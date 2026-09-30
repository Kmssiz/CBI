"""Deployment checks for the mobile authentication path."""

from django.conf import settings
from django.core.checks import Warning, register, Tags


@register(Tags.security)
def check_mobile_login_security(app_configs, **kwargs):
    # Mobile login always binds to AD with NTLM (mobile/auth.py), so the
    # password never reaches the directory in clear text, LDAPS or not.
    warnings = []

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
