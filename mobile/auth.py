"""Public configuration, login and logout for the mobile app."""
import logging

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac

from users.ldap_utils import connexion_ad2000
from users.models import CustomUser, MobileApiSession, Role, UserHistory

from .http import api_error, issue_token, mobile_endpoint, read_json, session_lifetime
from .serializers import serialize_me

logger = logging.getLogger('users')


@mobile_endpoint('GET', auth=False)
def config_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        'min_version': settings.MOBILE_MIN_APP_VERSION,
        'latest_version': settings.MOBILE_LATEST_APP_VERSION,
        'download_url': settings.MOBILE_APP_DOWNLOAD_URL,
        'contact': {
            'email': settings.MOBILE_CONTACT_EMAIL,
            'phone': settings.MOBILE_CONTACT_PHONE,
            'website': settings.MOBILE_WEBSITE_URL,
        },
        'notification_poll_seconds': settings.MOBILE_NOTIFICATION_POLL_SECONDS,
        'server_time': timezone.now().isoformat(),
    })


def resolve_directory_username(identifier: str) -> str | None:
    """Turn what the user typed into an AD sAMAccountName so we can always NTLM-bind.

    Accepts ``H0017549``, ``GSH\\H0017549``, ``H0017549@domain.local`` (UPN) and
    corporate e-mail addresses, which are looked up in the synced user directory.
    NTLM never sends the password to the directory in clear text, even on port 389.
    """
    value = identifier.strip()
    if '\\' in value:
        value = value.split('\\', 1)[1].strip()
    if '@' not in value:
        return value or None

    known = CustomUser.objects.filter(email__iexact=value).exclude(username='').first()
    if known is not None:
        return known.username
    local_part, _, domain = value.partition('@')
    domain = domain.casefold()
    if domain.endswith('.local') or domain.split('.', 1)[0] == settings.LDAP_DOMAIN.casefold():
        return local_part or None
    return None


def _throttle_keys(request: HttpRequest, identifier: str) -> dict[str, str]:
    ip_header = settings.MOBILE_LOGIN_CLIENT_IP_HEADER
    source_ip = (
        request.META.get(ip_header, '').split(',', 1)[0].strip() if ip_header
        else request.META.get('REMOTE_ADDR', '')
    ) or 'unknown'
    identities = {'ip': source_ip, 'user': identifier.strip().casefold()}
    return {
        scope: f'mobile-login:{scope}:' + salted_hmac('cbi-mobile-login-throttle', value).hexdigest()
        for scope, value in identities.items()
    }


def _consume(key: str, limit: int, window: int) -> bool:
    """Count one attempt; True when the bucket is over its limit."""
    if cache.add(key, 1, timeout=window):
        return limit < 1
    try:
        attempts = cache.incr(key)
    except ValueError:  # Expired between add() and incr().
        cache.add(key, 1, timeout=window)
        attempts = 1
    return attempts > limit


def _sync_directory_user(info: dict, fallback_username: str) -> CustomUser:
    """Create/update the local account exactly like users.views.login_view."""
    ldap_name = info.get('username') or fallback_username
    email = (info.get('email') or '').strip()
    ad2000 = (info.get('ad2000') or '').strip()
    if ad2000 in ('', '[]'):
        ad2000 = None

    user = CustomUser.objects.filter(username__iexact=ldap_name).first()
    if user is None and email:
        user = CustomUser.objects.filter(email__iexact=email).first()
    if user is None and ad2000:
        user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
    created = user is None
    if created:
        role, _ = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)
        user = CustomUser(username=ldap_name, role=role, can_view_direction=False)
        user.set_unusable_password()

    user.username = ldap_name
    user.first_name = info.get('first_name', '')
    user.last_name = info.get('last_name', '')
    user.email, user.ad2000 = email, ad2000
    if 'ad_groups' in info:
        # connexion_ad2000 does not return groups; keep the ones cached by the user sync.
        user.ad_groups = info['ad_groups']
    user.status = 'Active'
    user.save()
    if created:
        user.user_permissions.add(*user.role.permissions.all())
    return user


@mobile_endpoint('POST', auth=False)
def login_view(request: HttpRequest) -> JsonResponse:
    data = read_json(request)
    if data is None:
        return api_error(400, 'bad_request', 'Requête invalide.')
    raw_identifier, password = data.get('username'), data.get('password')
    if not isinstance(raw_identifier, str) or not isinstance(password, str) \
            or not raw_identifier.strip() or not password:
        return api_error(400, 'bad_request', 'Identifiant et mot de passe obligatoires.')
    identifier = raw_identifier.strip()

    keys = _throttle_keys(request, identifier)
    window = settings.MOBILE_LOGIN_RATE_LIMIT_WINDOW
    if _consume(keys['ip'], settings.MOBILE_LOGIN_RATE_LIMIT_PER_IP, window) or \
            _consume(keys['user'], settings.MOBILE_LOGIN_RATE_LIMIT_PER_USER, window):
        return api_error(429, 'throttled', 'Trop de tentatives. Réessayez plus tard.',
                         headers={'Retry-After': str(window)})

    username = resolve_directory_username(identifier)
    if username is None:
        return api_error(401, 'identifier_unknown',
                         'Adresse e-mail inconnue. Utilisez votre identifiant AD 2000.')

    info = connexion_ad2000(username, password)
    if info:
        user = _sync_directory_user(info, username)
    else:
        # Local accounts (admins/tests not in AD), same fallback as the web login.
        user = authenticate(request, username=username, password=password)
        if user is None:
            return api_error(401, 'invalid_credentials', 'Identifiant ou mot de passe invalide.')

    if not user.is_active:
        return api_error(403, 'account_inactive', 'Accès refusé. Veuillez contacter Helpdesk BI.')

    for key in keys.values():
        cache.delete(key)
    now = timezone.now()
    mobile_session = MobileApiSession.objects.create(
        user=user,
        expires_at=now + session_lifetime(),
        last_used_at=now,
        device=str(data.get('device') or '')[:120],
        app_version=str(data.get('app_version') or '')[:20],
    )
    UserHistory.objects.create(user=user, action='Utilisateur connecté (mobile)',
                               source=UserHistory.SOURCE_MOBILE)
    logger.info('Mobile login for user_id=%s (app %s)', user.pk, mobile_session.app_version or '?')
    return JsonResponse({
        'token': issue_token(mobile_session),
        'expires_at': mobile_session.expires_at.isoformat(),
        'user': serialize_me(user),
        # The app answers PBIRS NTLM challenges as DOMAIN\username with the password it holds.
        'credentials': {'domain': settings.LDAP_DOMAIN, 'username': user.username},
    })


@mobile_endpoint('POST')
def logout_view(request: HttpRequest) -> JsonResponse:
    request.mobile_session.revoked_at = timezone.now()
    request.mobile_session.save(update_fields=['revoked_at'])
    return JsonResponse({'ok': True})
