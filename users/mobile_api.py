"""Authenticated JSON endpoints for the Flutter client."""
import json
from functools import wraps
from urllib.parse import urlsplit

from datetime import timedelta

from django.contrib.auth import authenticate
from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from notifications.models import Notification
from powerbi_report.models import ReportRef, UserReportPermission
from users.ldap_utils import connexion_ad2000
from users.models import CustomUser, MobileApiSession, MobileFavorite, Role, UserHistory


def _payload(request):
    try:
        payload = json.loads(request.body or '{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mobile_login_throttle_keys(request, identifier):
    trusted_ip_header = settings.MOBILE_LOGIN_CLIENT_IP_HEADER
    source_ip = (
        request.META.get(trusted_ip_header, '').split(',', 1)[0].strip()
        if trusted_ip_header
        else request.META.get('REMOTE_ADDR', '')
    ) or 'unknown'
    identities = {
        'ip': source_ip,
        'user': identifier.strip().casefold(),
    }
    return {
        scope: 'mobile-login:' + scope + ':' + salted_hmac(
            'cbi-mobile-login-throttle', value,
        ).hexdigest()
        for scope, value in identities.items()
    }


def _consume_login_bucket(key, limit, window):
    if cache.add(key, 1, timeout=window):
        return 1 > limit
    try:
        attempts = cache.incr(key)
    except ValueError:  # The entry may expire between add() and incr().
        if cache.add(key, 1, timeout=window):
            attempts = 1
        else:
            attempts = cache.incr(key)
    return attempts > limit


def _mobile_auth(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return JsonResponse({'detail': 'Authentication required'}, status=401)
        try:
            claims = signing.loads(header[7:], salt='cbi-mobile-auth', max_age=60 * 60 * 24 * 14)
            mobile_session = MobileApiSession.objects.select_related('user', 'user__role').filter(
                pk=claims['sid'], user_id=claims['user'], revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).first()
            if mobile_session is None:
                return JsonResponse({'detail': 'Invalid or expired session'}, status=401)
            request.mobile_session = mobile_session
            request.mobile_user = mobile_session.user
            request.user = mobile_session.user
        except (signing.BadSignature, KeyError, ValueError):
            return JsonResponse({'detail': 'Invalid or expired session'}, status=401)
        if not request.mobile_user.is_active:
            return JsonResponse({'detail': 'Account is inactive'}, status=401)
        return view(request, *args, **kwargs)
    return wrapped


@csrf_exempt
@require_POST
def mobile_login(request):
    # Mobile users enter their directory password directly into the app. Never
    # let this API bind to Active Directory over plain LDAP or fall back to it.
    if not settings.LDAP_USE_SSL or settings.LDAP_ENABLE_PORT_FALLBACK:
        return JsonResponse(
            {'detail': 'Mobile login requires LDAPS with plaintext fallback disabled'},
            status=503,
        )

    data = _payload(request)
    raw_identifier, password = data.get('username'), data.get('password')
    if not isinstance(raw_identifier, str) or not isinstance(password, str):
        return JsonResponse({'detail': 'Username and password are required'}, status=400)
    identifier = raw_identifier.strip()
    if not identifier or not password:
        return JsonResponse({'detail': 'Username and password are required'}, status=400)

    throttle_keys = _mobile_login_throttle_keys(request, identifier)
    throttle_window = settings.MOBILE_LOGIN_RATE_LIMIT_WINDOW
    blocked = (
        _consume_login_bucket(
            throttle_keys['ip'], settings.MOBILE_LOGIN_RATE_LIMIT_PER_IP,
            throttle_window,
        )
        or _consume_login_bucket(
            throttle_keys['user'], settings.MOBILE_LOGIN_RATE_LIMIT_PER_USER,
            throttle_window,
        )
    )
    if blocked:
        return JsonResponse(
            {'detail': 'Too many login attempts. Try again later.'},
            status=429,
            headers={'Retry-After': str(throttle_window)},
        )

    # Match users.views.login_view so LDAP identities resolve exactly as on web.
    info = connexion_ad2000(identifier, password)
    user = None
    if info:
        ldap_name = info.get('username', identifier)
        email, ad2000 = (info.get('email') or '').strip(), (info.get('ad2000') or '').strip()
        if ad2000 in ('', '[]'):
            ad2000 = None
        user = CustomUser.objects.filter(username__iexact=ldap_name).first()
        if user is None and email:
            user = CustomUser.objects.filter(email__iexact=email).first()
        if user is None and ad2000:
            user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
        if user is None:
            role, _ = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)
            user = CustomUser(username=ldap_name, role=role)
            user.set_unusable_password()
            user.save()
            user.user_permissions.add(*role.permissions.all())
        user.username = ldap_name
        user.first_name = info.get('first_name', '')
        user.last_name = info.get('last_name', '')
        user.email, user.ad2000 = email, ad2000
        user.ad_groups = info.get('ad_groups', [])
        user.status = 'Active'
        user.save()
    else:
        user = authenticate(request, username=identifier, password=password)
        if user is None:
            return JsonResponse({'detail': 'Invalid credentials'}, status=401)

    if not user.is_active:
        return JsonResponse({'detail': 'Account is inactive'}, status=403)
    for key in throttle_keys.values():
        cache.delete(key)
    mobile_session = MobileApiSession.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(days=14),
    )
    token = signing.dumps({'user': user.pk, 'sid': str(mobile_session.pk)}, salt='cbi-mobile-auth')
    return JsonResponse({'token': token, 'user': {
        'name': user.get_full_name() or user.username,
        'role': user.role.name if user.role else '',
        'username': user.username,
        'domain': settings.LDAP_DOMAIN,
    }})


@csrf_exempt
@_mobile_auth
@require_POST
def mobile_logout(request):
    request.mobile_session.revoked_at = timezone.now()
    request.mobile_session.save(update_fields=['revoked_at'])
    return JsonResponse({'ok': True})


@_mobile_auth
@require_GET
def mobile_bootstrap(request):
    user = request.mobile_user
    permitted = ReportRef.objects.all() if user.is_superuser else ReportRef.objects.filter(user_permissions__user=user)
    permitted = permitted.distinct().prefetch_related('poles', 'societes', 'directions', 'modules')
    favorite_ids = set(MobileFavorite.objects.filter(user=user).values_list('report_id', flat=True))

    def serialize(item, category=None):
        mobile_token = signing.dumps({
            'user': user.pk, 'report': item.pk,
            'sid': str(request.mobile_session.pk),
        }, salt='mobile-pbirs')
        from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
        pbirs_server = item.server_url or get_primary_pbirs_server_url()
        pbirs_url = urlsplit(pbirs_server)
        if category is None:
            category = item.pole or item.societe or item.direction or item.name
        return {'id': str(item.pk), 'name': item.name, 'category': category,
                'embed_url': request.build_absolute_uri(f'/mobile/embed/{item.pk}/?token={mobile_token}'),
                'auth_host': pbirs_url.hostname or '',
                'auth_https': pbirs_url.scheme.lower() == 'https' and bool(pbirs_url.hostname),
                'tabs': [option.name for option in item.directions.all()],
                'favorite': item.pk in favorite_ids}

    def first_option_name(options, fallback):
        first_option = next(iter(options.all()), None)
        return first_option.name if first_option else fallback

    is_admin = user.is_superuser or user.is_admin
    from powerbi_report.models import CustomFolder, FolderReportItem
    from powerbi_report.views import get_visible_report_ids

    consolidated_ids = get_visible_report_ids(request, view_type='consolide') if (is_admin or user.can_view_consolide) else set()
    pole_ids = get_visible_report_ids(request, view_type='pole') if (is_admin or user.can_view_pole) else set()
    direction_ids = get_visible_report_ids(request, view_type='direction') if (is_admin or user.can_view_direction) else set()
    module_ids = get_visible_report_ids(request, view_type='module') if (is_admin or user.can_view_module) else set()
    anomaly_ids = get_visible_report_ids(request, view_type='anomalie') if (is_admin or user.can_view_anomalie) else set()
    biblio_ids = get_visible_report_ids(request, view_type='biblio')
    consolidated = [r for r in permitted if r.pbirs_id in consolidated_ids]
    poles = [r for r in permitted if r.pbirs_id in pole_ids]
    directions = [r for r in permitted if r.pbirs_id in direction_ids]
    modules = [r for r in permitted if r.pbirs_id in module_ids]
    anomalies = [r for r in permitted if r.pbirs_id in anomaly_ids]
    library = [r for r in permitted if r.pbirs_id in biblio_ids]
    companies = [r for r in permitted if r.pbirs_id in pole_ids.union(direction_ids) and (r.societes.exists() or r.societe)]

    view_access = {
        'consolide': is_admin or user.can_view_consolide,
        'direction': is_admin or user.can_view_direction,
        'pole': is_admin or user.can_view_pole,
        'module': is_admin or user.can_view_module,
        'anomalie': is_admin or user.can_view_anomalie,
        'biblio': True,
    }
    visible_ids_by_view = {
        'consolide': consolidated_ids,
        'direction': direction_ids,
        'pole': pole_ids,
        'module': module_ids,
        'anomalie': anomaly_ids,
        'biblio': biblio_ids,
    }
    mobile_folders = {}
    for view_type, allowed in view_access.items():
        if not allowed:
            continue
        visible_pbirs_ids = visible_ids_by_view[view_type]
        visible_reports = ReportRef.objects.filter(pbirs_id__in=visible_pbirs_ids)
        permitted_ids = set(visible_reports.values_list('pk', flat=True))
        all_folders = list(CustomFolder.objects.filter(view_type=view_type).order_by('order', 'name'))
        children = {folder.pk: [] for folder in all_folders}
        for folder in all_folders:
            if folder.parent_id in children:
                children[folder.parent_id].append(folder)
        reports_by_folder = {folder.pk: [] for folder in all_folders}
        assignments = FolderReportItem.objects.filter(
            folder__view_type=view_type,
            report_id__in=permitted_ids,
        ).select_related('report').order_by('order', 'report__name')
        for assignment in assignments:
            reports_by_folder[assignment.folder_id].append(serialize(assignment.report))

        def folder_tree(folder):
            nested = [node for child in children[folder.pk] if (node := folder_tree(child))]
            report_items = reports_by_folder[folder.pk]
            if not nested and not report_items:
                return None
            return {
                'id': folder.pk,
                'name': folder.name,
                'children': nested,
                'reports': report_items,
            }

        mobile_folders[view_type] = [
            node for folder in all_folders if folder.parent_id is None
            if (node := folder_tree(folder))
        ]
    user_notifications = Notification.objects.filter(user=user)
    unread_notification_count = user_notifications.filter(is_read=False).count()
    notifications = user_notifications.order_by('-created_at')[:30]
    return JsonResponse({
        'consolidated': [serialize(r, r.name) for r in consolidated],
        'poles': [serialize(r, first_option_name(r.poles, r.pole or 'Pôle')) for r in poles],
        'directions': [serialize(r, first_option_name(r.directions, r.direction or 'Direction')) for r in directions],
        'modules': [serialize(r, first_option_name(r.modules, 'Module')) for r in modules],
        'anomalies': [serialize(r, r.name) for r in anomalies],
        'library': [serialize(r, r.name) for r in library],
        'companies': [serialize(r, first_option_name(r.societes, r.societe or 'Société')) for r in companies],
        'favorites': [serialize(r) for r in permitted if r.pk in favorite_ids],
        'folders': mobile_folders,
        'notifications': [{'id': n.pk, 'title': 'Notification', 'message': n.message,
                           'date': n.created_at.strftime('%d/%m/%Y'),
                           'is_read': n.is_read} for n in notifications],
        'unread_notification_count': unread_notification_count,
        'permissions': {'direction': user.can_view_direction, 'pole': user.can_view_pole,
                        'consolidated': user.can_view_consolide, 'anomalie': user.can_view_anomalie,
                        'module': user.can_view_module},
    })


@_mobile_auth
@require_GET
def mobile_history(request):
    """Return only the authenticated user's recent portal activity."""
    records = UserHistory.objects.filter(
        user=request.mobile_user,
    ).order_by('-timestamp')[:100]
    return JsonResponse({
        'history': [
            {'id': record.pk, 'action': record.action, 'timestamp': record.timestamp.isoformat()}
            for record in records
        ],
    })


@csrf_exempt
@_mobile_auth
@require_http_methods(['PUT'])
def mobile_favorite(request, report_id):
    user = request.mobile_user
    if not user.is_superuser and not UserReportPermission.objects.filter(user=user, report_id=report_id).exists():
        return JsonResponse({'detail': 'Report not found'}, status=404)
    if not ReportRef.objects.filter(pk=report_id).exists():
        return JsonResponse({'detail': 'Report not found'}, status=404)
    should_favorite = bool(_payload(request).get('favorite'))
    favorite, _ = MobileFavorite.objects.get_or_create(user=user, report_id=report_id)
    if not should_favorite:
        favorite.delete()
    return JsonResponse({'favorite': should_favorite})


@csrf_exempt
@_mobile_auth
@require_http_methods(['PUT'])
def mobile_notification_read(request, notification_id):
    updated = Notification.objects.filter(
        pk=notification_id,
        user=request.mobile_user,
        is_read=False,
    ).update(is_read=True)
    if not updated and not Notification.objects.filter(
        pk=notification_id,
        user=request.mobile_user,
    ).exists():
        return JsonResponse({'detail': 'Notification not found'}, status=404)
    return JsonResponse({'is_read': True})


@csrf_exempt
@_mobile_auth
@require_http_methods(['PUT'])
def mobile_notifications_read_all(request):
    updated = Notification.objects.filter(
        user=request.mobile_user,
        is_read=False,
    ).update(is_read=True)
    return JsonResponse({'updated': updated})


@require_GET
def mobile_embed(request, report_id):
    try:
        claims = signing.loads(request.GET.get('token', ''), salt='mobile-pbirs', max_age=60 * 60 * 8)
    except signing.BadSignature:
        return HttpResponseForbidden('Expired or invalid report link.')
    if claims.get('report') != report_id:
        return HttpResponseForbidden('Invalid report link.')
    account = CustomUser.objects.filter(pk=claims.get('user'), is_active=True).first()
    if account is None:
        return HttpResponseForbidden('This account is inactive.')
    if not MobileApiSession.objects.filter(
        pk=claims.get('sid'), user=account, revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).exists():
        return HttpResponseForbidden('This mobile session has expired.')
    report = ReportRef.objects.filter(pk=report_id).first()
    if report and not account.is_superuser:
        if not UserReportPermission.objects.filter(report=report, user=account).exists():
            report = None
    if report is None:
        return HttpResponseForbidden('You do not have access to this report.')
    from users.utils import log_history
    log_history(account, f"Rapport consulté : {report.name} (mobile)")
    # Pass a short-lived signature to PBIRS URL resolution; the target remains
    # the report's configured server URL and embedded report path.
    from urllib.parse import quote
    from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
    server = report.server_url or get_primary_pbirs_server_url()
    url = f"{server}/Reports/powerbi/{quote(report.path.strip('/'), safe='/')}?rs:embed=true"
    return redirect(url)
