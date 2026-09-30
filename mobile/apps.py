from django.apps import AppConfig


class MobileConfig(AppConfig):
    """JSON API for the CBI mobile app. Models live in ``users``."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mobile'
    verbose_name = 'CBI Mobile API'
