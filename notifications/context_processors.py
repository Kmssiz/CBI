# notifications/context_processors.py

from .models import Notification  # Import the Notification model

def notifications_context(request):
    if request.user.is_authenticated:
        notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
        unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
        return {
            'notifications': notifications,
            'unread': unread_count,
        }
    return {}
