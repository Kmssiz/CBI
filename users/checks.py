"""Deployment checks for the mobile authentication path."""

from django.conf import settings
from django.core.checks import Warning, register, Tags


@register(Tags.security)
def check_mobile_login_security(app_configs, **kwargs):
    warnings = []

    if not settings.LDAP_USE_SSL or settings.LDAP_ENABLE_PORT_FALLBACK:
        warnings.append(Warning(
            'Mobile login is disabled because LDAP is not locked to LDAPS.',
            hint=(
                'Set LDAP_USE_SSL=True and LDAP_ENABLE_PORT_FALLBACK=False, '
                'then verify that the directory certificate is trusted.'
            ),
            id='users.W001',
        ))

    cache_backend = settings.CACHES['default']['BACKEND']
    if cache_backend.endswith('LocMemCache'):
        warnings.append(Warning(
            'Mobile login throttles use a process-local cache.',
            hint=(
                'Enforce login limits at the ingress or configure an atomic '
                'shared cache for every web worker and replica.'
            ),
            id='users.W002',
        ))

    return warnings
