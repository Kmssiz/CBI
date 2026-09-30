"""Request/response plumbing shared by every mobile endpoint."""
import json
from datetime import timedelta
from functools import wraps
from typing import Any, Callable

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from users.models import MobileApiSession

TOKEN_SALT = 'cbi-mobile-auth-v1'
# Sliding expiry is only written back this often, to avoid one UPDATE per request.
SESSION_TOUCH_INTERVAL = timedelta(minutes=15)


def api_error(status: int, code: str, detail: str, headers: dict | None = None) -> JsonResponse:
    return JsonResponse({'detail': detail, 'code': code}, status=status, headers=headers)


def read_json(request: HttpRequest) -> dict[str, Any] | None:
    """Return the JSON object body, ``{}`` when empty, or None when malformed."""
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def int_param(request: HttpRequest, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(request.GET.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def issue_token(mobile_session: MobileApiSession) -> str:
    return signing.dumps({'user': mobile_session.user_id, 'sid': str(mobile_session.pk)}, salt=TOKEN_SALT)


def session_lifetime() -> timedelta:
    return timedelta(days=settings.MOBILE_SESSION_DAYS)


def _authenticate(request: HttpRequest) -> JsonResponse | None:
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return api_error(401, 'auth_required', 'Authentification requise.')
    try:
        # Expiry is enforced by the session row, which slides while the app is used.
        claims = signing.loads(header[7:].strip(), salt=TOKEN_SALT)
        mobile_session = MobileApiSession.objects.select_related('user', 'user__role').filter(
            pk=claims['sid'], user_id=claims['user'], revoked_at__isnull=True,
        ).first()
    except (signing.BadSignature, ValidationError, KeyError, TypeError, ValueError):
        return api_error(401, 'session_expired', 'Session invalide ou expirée.')

    now = timezone.now()
    if mobile_session is None or mobile_session.expires_at <= now:
        return api_error(401, 'session_expired', 'Session invalide ou expirée.')
    if not mobile_session.user.is_active:
        return api_error(401, 'account_inactive', 'Ce compte est désactivé.')

    if mobile_session.last_used_at is None or now - mobile_session.last_used_at > SESSION_TOUCH_INTERVAL:
        mobile_session.last_used_at = now
        mobile_session.expires_at = now + session_lifetime()
        mobile_session.save(update_fields=['last_used_at', 'expires_at'])

    request.mobile_session = mobile_session
    request.user = mobile_session.user
    return None


def mobile_endpoint(*methods: str, auth: bool = True) -> Callable:
    """Wrap a view as a JSON endpoint: method check, then Bearer authentication.

    Mobile requests carry no cookies, so CSRF protection does not apply.
    """
    allowed = {method.upper() for method in methods}

    def decorator(view: Callable) -> Callable:
        @csrf_exempt
        @wraps(view)
        def wrapped(request: HttpRequest, *args, **kwargs):
            if request.method not in allowed:
                return api_error(405, 'method_not_allowed', 'Méthode non autorisée.',
                                 headers={'Allow': ', '.join(sorted(allowed))})
            if auth:
                failure = _authenticate(request)
                if failure is not None:
                    return failure
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def dispatch(**handlers: Callable) -> Callable:
    """Route one URL to per-method handlers, e.g. ``dispatch(GET=list_view, POST=create_view)``."""
    def view(request: HttpRequest, *args, **kwargs):
        return handlers[request.method](request, *args, **kwargs)
    return mobile_endpoint(*handlers)(view)
