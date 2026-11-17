"""Copy one user's access (reports, RLS, view rights) to another: a preview first, then the copy."""

import logging
import re

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from notifications.models import Notification
from powerbi_report.services import access_copy
from powerbi_report.services.pbirs_client import PBIRSClient
from users.models import CustomUser
from users.session_secrets import get_ldap_password
from users.utils import admin_required, get_user_permissions, person_label

logger = logging.getLogger('powerbi_report')

PREVIEW_ROWS = 200      # rows listed in the preview (the counts always cover everything)
SEARCH_LIMIT = 15       # people proposed while typing
SEARCH_MIN_CHARS = 2


def _name(user) -> str:
    return user.get_full_name() or user.username


def find_user(text):
    """A user typed or picked as 'Nom Prénom (ad2000)', a bare ad2000, or a username. Active users only."""
    text = (text or '').strip()
    if not text:
        return None
    match = re.search(r'\(([^()]+)\)\s*$', text)
    ident = (match.group(1) if match else text).strip()
    return CustomUser.objects.filter(is_active=True).filter(Q(ad2000__iexact=ident) | Q(username__iexact=ident)).first()


@admin_required
@require_GET
def copy_user_search(request):
    """People matching what is typed in the dialog ("Nom Prénom (ad2000)"). Thousands of users: never listed in full."""
    text = (request.GET.get('q') or '').strip()
    if len(text) < SEARCH_MIN_CHARS:
        return JsonResponse({'results': []})
    people = CustomUser.objects.filter(is_active=True, ad2000__isnull=False).exclude(ad2000='')
    people = people.exclude(ad2000__startswith='$').exclude(ad2000__icontains='duplicate')   # directory conflict records
    for term in text.split()[:4]:                       # every word must match somewhere: "bob dur" finds Bob Durand
        people = people.filter(Q(first_name__icontains=term) | Q(last_name__icontains=term)
                               | Q(username__icontains=term) | Q(ad2000__icontains=term))
    exclude = (request.GET.get('exclude') or '').strip()
    if exclude:
        people = people.exclude(ad2000__iexact=exclude)
    people = people.order_by('first_name', 'last_name', 'username')[:SEARCH_LIMIT]
    return JsonResponse({'results': [' '.join(f"{_name(u)} ({u.ad2000})".split()) for u in people]})   # one line each


def _pks(request, name):
    """Report ids the admin unticked on the preview (posted as repeated fields)."""
    return [int(v) for v in request.POST.getlist(name) if v.isdigit()]


def _role_picks(request):
    """{report id: [role ids]} for each RLS line shown on the preview (rls_pick lists them)."""
    picks = {}
    for pk in _pks(request, 'rls_pick'):
        picks[pk] = request.POST.getlist(f'rls_roles_{pk}')
    return picks


def _options(post):
    return {
        'do_reports': post.get('do_reports') == 'on',
        'do_rls': post.get('do_rls') == 'on',
        'do_sections': post.get('do_sections') == 'on',
    }


def _problem(request, source, target, options):
    """A reason the copy cannot start, or ''."""
    if source is None or target is None:
        return "Utilisateur introuvable (choisissez une personne dans la liste)."
    if source.pk == target.pk:
        return "La source et la destination sont la même personne."
    if not (source.ad2000 or '').strip() or not (target.ad2000 or '').strip():
        return "Les deux personnes doivent avoir un identifiant AD (ad2000)."
    if not any(options.values()):
        return "Cochez au moins une chose à copier."
    if not get_ldap_password(request):
        return "Session expirée : reconnectez-vous pour agir sur PBIRS."
    return ''


def _factory(request):
    return lambda: PBIRSClient(request)


@admin_required
@require_POST
def copy_access_preview(request, username):
    """Step 1, read-only: show exactly what copying would do. Nothing is written."""
    current = CustomUser.objects.filter(ad2000__iexact=username).first()
    if current is None:
        messages.error(request, f"Utilisateur '{username}' introuvable.")
        return redirect('users_view')
    other = find_user(request.POST.get('other'))
    from_me = request.POST.get('direction') != 'to_me'
    source, target = (current, other) if from_me else (other, current)
    options = _options(request.POST)

    problem = _problem(request, source, target, options)
    if problem:
        messages.error(request, problem)
        return redirect('powerbi_report:user_permission', username=current.ad2000)

    plan = access_copy.build_plan(_factory(request), source, target, **options)
    return render(request, 'powerbi_report/copy_access_preview.html', {
        'plan': plan, 'source': source, 'target': target, 'current': current, 'options': options,
        'to_grant_shown': plan.to_grant[:PREVIEW_ROWS], 'to_grant_more': max(0, len(plan.to_grant) - PREVIEW_ROWS),
        'rls_shown': plan.rls[:PREVIEW_ROWS], 'rls_more': max(0, len(plan.rls) - PREVIEW_ROWS),
        'permissions': get_user_permissions(request.user),
    })


@admin_required
@require_POST
def copy_access_apply(request, username):
    """Step 2: read everything again (never trust the preview) and write it."""
    current = CustomUser.objects.filter(ad2000__iexact=username).first()
    source, target = find_user(request.POST.get('source_user')), find_user(request.POST.get('target_user'))
    options = _options(request.POST)
    back = current.ad2000 if current else (target.ad2000 if target else '')

    problem = _problem(request, source, target, options)
    if problem:
        messages.error(request, problem)
        return redirect('powerbi_report:user_permission', username=back) if back else redirect('users_view')

    factory = _factory(request)
    plan = access_copy.build_plan(factory, source, target, **options)
    plan = access_copy.restrict_plan(plan, skip_grant=_pks(request, 'skip_grant'), skip_rls=_pks(request, 'skip_rls'),
                                     role_picks=_role_picks(request))
    result = access_copy.apply_plan(factory, plan)
    _notify(request, plan, result)
    _report(request, plan, result)
    logger.info("Access copied %s -> %s by %s: %d granted, %d RLS, sections %s, %d failed",
                source.ad2000, target.ad2000, request.user.username, len(result.granted),
                len(result.rls_done), result.sections_set, len(result.grant_failed) + len(result.rls_failed))
    return redirect('powerbi_report:user_permission', username=target.ad2000)


def _report(request, plan, result):
    """What happened, in the page's own message area."""
    done = []
    if result.granted:
        done.append(f"{len(result.granted)} rapport(s) ajouté(s)")
    if result.already_present:
        done.append(f"{len(result.already_present)} déjà accessible(s)")
    if result.rls_done:
        done.append(f"RLS appliqué sur {len(result.rls_done)} rapport(s)")
    if result.sections_set:
        done.append("sections visibles : " + ", ".join(result.sections_set))
    if done:
        messages.success(request, f"Copie terminée pour {_name(plan.target)} : " + " · ".join(done) + ".")
    elif not (result.grant_failed or result.rls_failed or result.not_processed):
        messages.info(request, "Rien à copier : tout est déjà en place.")

    def listing(failures):
        shown = ", ".join(f"« {r.name or r.pbirs_id} »" for r, _ in failures[:5])
        return shown + (f" et {len(failures) - 5} autre(s)" if len(failures) > 5 else "")

    if result.grant_failed:
        messages.error(request, f"{len(result.grant_failed)} rapport(s) n'ont pas pu être ajoutés : {listing(result.grant_failed)}.")
    if result.rls_failed:
        messages.error(request, f"RLS non appliqué sur {len(result.rls_failed)} rapport(s) : {listing(result.rls_failed)}.")
    if result.not_processed:
        messages.warning(request, f"{result.not_processed} élément(s) restent à traiter (temps limite atteint) : "
                                  "relancez la copie, ce qui est déjà fait est ignoré.")


def _notify(request, plan, result):
    """Tell the person who received access, and the other admins (one message each)."""
    source, target = plan.source, plan.target
    if result.granted or result.rls_done:
        names = [r.name for r in result.granted if r.name][:8]
        more = f" et {len(result.granted) - 8} autre(s)" if len(result.granted) > 8 else ""
        text = f"Vous avez obtenu l'accès à {len(result.granted)} rapport(s)"
        if names:
            text += " : " + ", ".join(names) + more
        if result.rls_done:
            text += f" (RLS appliqué sur {len(result.rls_done)} rapport(s))"
        Notification.objects.create(user=target, message=text + ".")
    if result.granted or result.rls_done or result.sections_set:
        summary = (f"{_name(target)} a reçu une copie des accès de {_name(source)} : "
                   f"{len(result.granted)} rapport(s), RLS sur {len(result.rls_done)}, par {person_label(request.user)}.")
        for admin in CustomUser.objects.filter(is_superuser=True, is_active=True).exclude(pk=target.pk):
            Notification.objects.create(user=admin, message=summary)
