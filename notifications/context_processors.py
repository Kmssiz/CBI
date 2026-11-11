"""Template context for the notification bell in the page header."""
from django.utils.functional import SimpleLazyObject

from .models import Notification

BELL_LIMIT = 20


def notifications_context(request):
    """
    ``bell_notifications`` (latest ones) and ``bell_unread`` for base.html.

    Both are lazy: pages that do not render the header (JSON, redirects,
    standalone pages) run no query.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}
    return {
        'bell_notifications': SimpleLazyObject(
            lambda: list(Notification.objects.filter(user=user).order_by('-created_at')[:BELL_LIMIT])
        ),
        'bell_unread': SimpleLazyObject(
            lambda: Notification.objects.filter(user=user, is_read=False).count()
        ),
    }
