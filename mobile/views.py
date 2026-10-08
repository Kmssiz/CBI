"""Authenticated mobile endpoints (see docs/MOBILE_API.md)."""
import hashlib
import json
import logging
import mimetypes
from datetime import timedelta

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Max, OuterRef, Q, Subquery
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone

from notifications.models import Notification
from powerbi_report.models import LOGO_OPTION_TYPES, MetadataOption, ReportRef
from users.models import CustomUser, MobileFavorite, UserHistory

from .catalog import (
    ReportAccess, ServerRegistry, build_catalog, favorite_ids_for, is_mobile_admin,
    serialize_report, user_can_open, with_metadata,
)
from .http import api_error, dispatch, int_param, mobile_endpoint, read_json
from .serializers import (
    REPORT_VIEW_PREFIX, serialize_history, serialize_me, serialize_notification,
    serialize_user_summary,
)

logger = logging.getLogger('users')


def _not_found() -> JsonResponse:
    return api_error(404, 'not_found', 'Élément introuvable.')


# --- profile ---------------------------------------------------------------

@mobile_endpoint('GET')
def me_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(serialize_me(request.user))


def _photo_response(user: CustomUser) -> HttpResponse:
    if not user.profile_image:
        return _not_found()
    try:
        handle = user.profile_image.open('rb')
    except (FileNotFoundError, OSError):
        return _not_found()
    content_type = mimetypes.guess_type(user.profile_image.name)[0] or 'application/octet-stream'
    response = FileResponse(handle, content_type=content_type)
    response['Cache-Control'] = 'private, max-age=3600'
    return response


@mobile_endpoint('GET')
def me_photo_view(request: HttpRequest) -> HttpResponse:
    return _photo_response(request.user)


@mobile_endpoint('GET')
def user_photo_view(request: HttpRequest, user_id: int) -> HttpResponse:
    # Photos of other users only appear on the admin history screens.
    if user_id != request.user.pk and not is_mobile_admin(request.user):
        return api_error(403, 'forbidden', 'Accès refusé.')
    user = CustomUser.objects.filter(pk=user_id).first()
    return _photo_response(user) if user else _not_found()


@mobile_endpoint('GET')
def metadata_logo_view(request: HttpRequest, option_id: int) -> HttpResponse:
    option = MetadataOption.objects.filter(pk=option_id, option_type__in=LOGO_OPTION_TYPES).first()
    if option is None or not option.logo:
        return _not_found()
    try:
        handle = option.logo.open('rb')
    except (FileNotFoundError, OSError):
        return _not_found()
    content_type = mimetypes.guess_type(option.logo.name)[0] or 'application/octet-stream'
    response = FileResponse(handle, content_type=content_type)
    # The catalogue URL carries ?v=<file name>, so a cached copy is never stale.
    response['Cache-Control'] = 'private, max-age=31536000, immutable'
    return response


# --- catalogue & reports ---------------------------------------------------

def _unread_count(user: CustomUser) -> int:
    return Notification.objects.filter(user=user, is_read=False).count()


@mobile_endpoint('GET')
def catalog_view(request: HttpRequest) -> HttpResponse:
    catalog = build_catalog(request)
    catalog['unread_notification_count'] = _unread_count(request.user)
    body = json.dumps(catalog, cls=DjangoJSONEncoder, sort_keys=True)
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'
    if request.headers.get('If-None-Match') == etag:
        response = HttpResponse(status=304)
    else:
        catalog['generated_at'] = timezone.now().isoformat()
        response = JsonResponse(catalog)
    response['ETag'] = etag
    response['Cache-Control'] = 'private, no-cache'
    return response


def _openable_report(request: HttpRequest, report_id: int) -> ReportRef | None:
    report = with_metadata(ReportRef.objects.filter(pk=report_id)).first()
    if report is None or not user_can_open(request.user, report):
        return None
    return report


def _report_payload(request: HttpRequest, report: ReportRef, servers: ServerRegistry | None = None) -> dict:
    return serialize_report(report, servers or ServerRegistry(), favorite_ids_for(request.user))


@mobile_endpoint('GET')
def report_detail_view(request: HttpRequest, report_id: int) -> JsonResponse:
    report = _openable_report(request, report_id)
    return JsonResponse(_report_payload(request, report)) if report else _not_found()


@mobile_endpoint('POST')
def report_open_view(request: HttpRequest, report_id: int) -> JsonResponse:
    report = _openable_report(request, report_id)
    if report is None:
        return _not_found()
    servers = ServerRegistry()
    server = servers.for_report(report)
    if server is None:
        return api_error(503, 'server_unavailable', "Aucun serveur de rapports n'est configuré.")
    entry = UserHistory.objects.create(
        user=request.user, report=report, source=UserHistory.SOURCE_MOBILE,
        action=f'{REPORT_VIEW_PREFIX} : {report.name} (mobile)'[:255],
    )
    payload = _report_payload(request, report, servers)
    return JsonResponse({'view_id': entry.pk, 'embed_url': payload['embed_url'], 'server': server, 'report': payload})


@mobile_endpoint('GET')
def report_mobile_layout_view(request: HttpRequest, report_id: int) -> JsonResponse:
    """Phone layout of the report's pages; the app keeps the desktop view when unavailable."""
    from .mobile_layout import MobileLayoutUnavailable, mobile_layout_for

    report = _openable_report(request, report_id)
    if report is None:
        return _not_found()
    try:
        data = mobile_layout_for(report)
    except MobileLayoutUnavailable as exc:
        logger.warning('Mobile layout unavailable for report %s: %s', report.pk, exc)
        return JsonResponse({'available': False, 'pages': {}})
    return JsonResponse({'available': bool(data.get('pages')), **data})


@mobile_endpoint('POST')
def report_close_view(request: HttpRequest, report_id: int) -> JsonResponse:
    data = read_json(request)
    if data is None:
        return api_error(400, 'bad_request', 'Requête invalide.')
    try:
        view_id = int(data.get('view_id'))
        duration = int(data.get('duration_seconds'))
    except (TypeError, ValueError):
        return api_error(400, 'bad_request', 'view_id et duration_seconds sont obligatoires.')
    updated = UserHistory.objects.filter(
        pk=view_id, user=request.user, report_id=report_id, source=UserHistory.SOURCE_MOBILE,
    ).update(duration_seconds=max(0, min(duration, 24 * 3600)))
    return JsonResponse({'ok': True}) if updated else _not_found()


# --- favorites -------------------------------------------------------------

@mobile_endpoint('GET')
def favorites_view(request: HttpRequest) -> JsonResponse:
    servers, favorite_ids = ServerRegistry(), favorite_ids_for(request.user)
    access = ReportAccess(request.user)
    reports = with_metadata(ReportRef.objects.filter(pk__in=favorite_ids))
    items = [
        serialize_report(report, servers, favorite_ids)
        for report in reports if access.can_open(report.pk)
    ]
    items.sort(key=lambda item: (item['location'].casefold(), item['name'].casefold()))
    return JsonResponse({'favorites': items})


def _favorite_put(request: HttpRequest, report_id: int) -> JsonResponse:
    report = ReportRef.objects.filter(pk=report_id).first()
    if report is None or not user_can_open(request.user, report):
        return _not_found()
    MobileFavorite.objects.get_or_create(user=request.user, report=report)
    return JsonResponse({'id': report_id, 'favorite': True})


def _favorite_delete(request: HttpRequest, report_id: int) -> JsonResponse:
    MobileFavorite.objects.filter(user=request.user, report_id=report_id).delete()
    return JsonResponse({'id': report_id, 'favorite': False})


favorite_view = dispatch(PUT=_favorite_put, DELETE=_favorite_delete)


# --- notifications ---------------------------------------------------------

def _latest_notification_id(user: CustomUser) -> int:
    return Notification.objects.filter(user=user).aggregate(latest=Max('id'))['latest'] or 0


@mobile_endpoint('GET')
def notifications_view(request: HttpRequest) -> JsonResponse:
    user = request.user
    queryset = Notification.objects.filter(user=user)
    after = int_param(request, 'after', 0, 0, 2**31 - 1)
    if after:
        queryset = queryset.filter(pk__gt=after)
    limit = int_param(request, 'limit', 50, 1, 200)
    return JsonResponse({
        'notifications': [serialize_notification(n) for n in queryset.order_by('-created_at', '-pk')[:limit]],
        'unread_count': _unread_count(user),
        'latest_id': _latest_notification_id(user),
    })


@mobile_endpoint('GET')
def notifications_unread_count_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse({'unread_count': _unread_count(request.user), 'latest_id': _latest_notification_id(request.user)})


@mobile_endpoint('POST')
def notification_read_view(request: HttpRequest, notification_id: int) -> JsonResponse:
    notifications = Notification.objects.filter(pk=notification_id, user=request.user)
    if not notifications.exists():
        return _not_found()
    notifications.update(is_read=True)
    return JsonResponse({'ok': True, 'unread_count': _unread_count(request.user)})


@mobile_endpoint('POST')
def notifications_read_all_view(request: HttpRequest) -> JsonResponse:
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'ok': True, 'unread_count': 0})


@mobile_endpoint('DELETE')
def notification_delete_view(request: HttpRequest, notification_id: int) -> JsonResponse:
    deleted, _ = Notification.objects.filter(pk=notification_id, user=request.user).delete()
    if not deleted:
        return _not_found()
    return JsonResponse({'ok': True, 'unread_count': _unread_count(request.user)})


# --- history ---------------------------------------------------------------

def _consultations(user: CustomUser, days: int):
    since = timezone.now() - timedelta(days=days)
    return UserHistory.objects.filter(
        user=user, action__startswith=REPORT_VIEW_PREFIX, timestamp__gte=since,
    ).select_related('report').prefetch_related(
        'report__poles', 'report__directions', 'report__societes', 'report__modules',
    ).order_by('-timestamp')


@mobile_endpoint('GET')
def history_view(request: HttpRequest) -> JsonResponse:
    days = int_param(request, 'days', 30, 1, 365)
    return JsonResponse({'history': [serialize_history(e) for e in _consultations(request.user, days)[:500]]})


@mobile_endpoint('GET')
def history_users_view(request: HttpRequest) -> JsonResponse:
    if not is_mobile_admin(request.user):
        return api_error(403, 'forbidden', 'Réservé aux administrateurs.')
    users = CustomUser.objects.annotate(
        last_view=Max('history__timestamp', filter=Q(history__action__startswith=REPORT_VIEW_PREFIX)),
    ).filter(last_view__isnull=False)
    if query := request.GET.get('q', '').strip():
        for term in query.split():
            users = users.filter(
                Q(first_name__icontains=term) | Q(last_name__icontains=term)
                | Q(username__icontains=term) | Q(ad2000__icontains=term)
            )
    if company := request.GET.get('company', '').strip():
        users = users.filter(societe__icontains=company)

    limit = int_param(request, 'limit', 50, 1, 100)
    offset = int_param(request, 'offset', 0, 0, 10**6)
    count = users.count()
    last_entry = UserHistory.objects.filter(
        user=OuterRef('pk'), action__startswith=REPORT_VIEW_PREFIX,
    ).order_by('-timestamp', '-pk').values('pk')[:1]
    page = list(
        users.annotate(last_entry_id=Subquery(last_entry))
        .select_related('role').order_by('-last_view', 'pk')[offset:offset + limit]
    )
    entries = UserHistory.objects.filter(pk__in=[user.last_entry_id for user in page]).select_related('report') \
        .prefetch_related('report__poles', 'report__directions', 'report__societes', 'report__modules')
    entries_by_id = {entry.pk: entry for entry in entries}
    results = [
        {'user': serialize_user_summary(user),
         'last': serialize_history(entries_by_id[user.last_entry_id]) if user.last_entry_id in entries_by_id else None}
        for user in page
    ]
    return JsonResponse({'count': count, 'results': results})


@mobile_endpoint('GET')
def history_user_detail_view(request: HttpRequest, user_id: int) -> JsonResponse:
    if user_id != request.user.pk and not is_mobile_admin(request.user):
        return api_error(403, 'forbidden', 'Accès refusé.')
    user = CustomUser.objects.select_related('role').filter(pk=user_id).first()
    if user is None:
        return _not_found()
    days = int_param(request, 'days', 30, 1, 365)
    return JsonResponse({
        'user': serialize_user_summary(user),
        'history': [serialize_history(e) for e in _consultations(user, days)[:500]],
    })
