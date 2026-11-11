"""Report slider: personal slideshows played as a presentation or a kiosk loop."""

import logging
from functools import wraps
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from powerbi_report.models import ReportRef, Slider, SliderItem, UserReportPermission
from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
from users.utils import get_user_permissions, log_history

logger = logging.getLogger('powerbi_report')

MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 3600
PLAY_MODES = ('kiosk',)  # one way to play a slider: the auto-looping one


def can_use_slider(user) -> bool:
    return user.is_authenticated and (user.is_admin or getattr(user, 'can_use_slider', False))


def slider_required(view_func):
    """Login required, then the admin-granted slider permission."""
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not can_use_slider(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)
    return _wrapped


def _page_context(request) -> dict:
    return {
        'permissions': get_user_permissions(request.user),
    }


def _accessible_reports(user) -> QuerySet:
    """Reports the user may open, from the local permission cache."""
    reports = ReportRef.objects.all()
    if not user.is_admin:
        reports = reports.filter(
            id__in=UserReportPermission.objects.filter(user=user).values('report_id')
        )
    return reports.prefetch_related('societes', 'directions', 'modules').order_by('name')


def _embed_url(report: ReportRef) -> str:
    if report.embed_url:
        return report.embed_url
    server_url = report.server_url or get_primary_pbirs_server_url()
    return f"{server_url}/Reports/powerbi/{quote((report.path or '').strip('/'), safe='/')}?rs:embed=true"


def _parse_interval(raw: str | None) -> int:
    try:
        value = int(raw or 30)
    except ValueError:
        value = 30
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, value))


@slider_required
def slider_list(request):
    sliders = (
        Slider.objects.filter(owner=request.user)
        .prefetch_related('items__report')
    )
    return render(request, 'powerbi_report/slider_list.html', {
        **_page_context(request),
        'sliders': sliders,
    })


@slider_required
def slider_edit(request, slider_id: int | None = None):
    slider = get_object_or_404(Slider, id=slider_id, owner=request.user) if slider_id else None
    accessible = _accessible_reports(request.user)

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        interval = _parse_interval(request.POST.get('interval_seconds'))
        accessible_ids = set(accessible.values_list('id', flat=True))
        # Keep the submitted order; drop duplicates and reports the user cannot open.
        report_ids = []
        for raw_id in request.POST.getlist('reports'):
            if raw_id.isdigit() and int(raw_id) in accessible_ids and int(raw_id) not in report_ids:
                report_ids.append(int(raw_id))

        if not name or not report_ids:
            messages.error(request, "Donnez un nom au slider et ajoutez au moins un rapport.")
        else:
            with transaction.atomic():
                if slider is None:
                    slider = Slider(owner=request.user)
                slider.name = name[:200]
                slider.interval_seconds = interval
                slider.save()
                slider.items.all().delete()
                SliderItem.objects.bulk_create([
                    SliderItem(slider=slider, report_id=report_id, position=position)
                    for position, report_id in enumerate(report_ids)
                ])
            messages.success(request, f"Slider « {slider.name} » enregistré.")
            return redirect('powerbi_report:slider_list')

    selected = list(slider.items.select_related('report')) if slider else []
    selected_ids = {item.report_id for item in selected}
    reports = list(accessible)
    by_id = {r.id: r for r in reports}
    # Filter choices: only the sociétés / directions that occur in the user's reports.
    societes = {s.id: s for r in reports for s in r.societes.all()}
    directions = {d.id: d for r in reports for d in r.directions.all()}
    return render(request, 'powerbi_report/slider_form.html', {
        **_page_context(request),
        'slider': slider,
        'selected_reports': [by_id.get(item.report_id, item.report) for item in selected],
        'available_reports': [r for r in reports if r.id not in selected_ids],
        'filter_societes': sorted(societes.values(), key=lambda o: o.name.lower()),
        'filter_directions': sorted(directions.values(), key=lambda o: o.name.lower()),
        'min_interval': MIN_INTERVAL_SECONDS,
        'max_interval': MAX_INTERVAL_SECONDS,
    })


@slider_required
@require_POST
def slider_delete(request, slider_id: int):
    slider = get_object_or_404(Slider, id=slider_id, owner=request.user)
    name = slider.name
    slider.delete()
    messages.success(request, f"Slider « {name} » supprimé.")
    return redirect('powerbi_report:slider_list')


@slider_required
def slider_play(request, slider_id: int):
    from powerbi_report.views import _user_can_access_report

    slider = get_object_or_404(Slider, id=slider_id, owner=request.user)
    # Permissions change with every sync: re-check each report at play time.
    items = list(slider.items.select_related('report'))
    slides = [
        {'name': item.report.name, 'url': _embed_url(item.report)}
        for item in items
        if _user_can_access_report(request.user, item.report)
    ]
    skipped = len(items) - len(slides)
    if skipped:
        logger.info("Slider %s: %s report(s) skipped, access lost for %s.", slider.id, skipped, request.user.username)
    if not slides:
        messages.error(request, "Ce slider ne contient plus aucun rapport accessible.")
        return redirect('powerbi_report:slider_list')

    mode = 'kiosk'
    log_history(request.user, f"Rapport consulté (slider) : {slider.name}")
    return render(request, 'powerbi_report/slider_play.html', {
        'slider': slider,
        'slides': slides,
        'skipped': skipped,
        'mode': mode,
    })


@login_required
@require_POST
def slider_keepalive(request):
    """Kiosk heartbeat: saving the session pushes its expiry forward."""
    request.session.modified = True
    return JsonResponse({'ok': True})
