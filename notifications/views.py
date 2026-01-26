from django.shortcuts import render ,redirect,get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Notification
from django.http import JsonResponse
from users.models import UserHistory
from django.utils.timezone import now



def log_history(user, action):
    UserHistory.objects.create(user=user, action=action, timestamp=now())

def get_user_permissions(user):
    all_permissions = [
      
        'add_permission', 'change_permission', 'delete_permission', 'view_permission',
       
        

        'add_notification', 'change_notification', 'delete_notification', 'view_notification',
        'add_powerbireport', 'change_powerbireport', 'delete_powerbireport', 'view_powerbireport',
        'add_report', 'change_report', 'delete_report', 'view_report',
        'view_refresh',
        'add_reportaccess', 'change_reportaccess', 'delete_reportaccess', 'view_reportaccess',
        'add_task', 'change_task', 'delete_task', 'view_task',
        'view_dashboard',
         'add_customuser', 'change_customuser', 'delete_customuser', 'view_customuser',
        'add_role', 'change_role', 'delete_role', 'view_role',
        'add_userhistory', 'change_userhistory', 'delete_userhistory', 'view_userhistory',
    ]
    
    user_permissions = user.user_permissions.values_list('codename', flat=True)
    
    permissions = {perm: perm in user_permissions for perm in all_permissions}
    
    return permissions


@login_required
def notifications_list(request):
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread=Notification.objects.filter(user=request.user, is_read=False).count()
    log_history(request.user, "Viewed notifications list")
    permissions = get_user_permissions(request.user)

    return render(request, 'notifications/notifications.html',{
        'notifications':notifications,
        'unread':unread,
        'permissions': permissions,
        })
 
@login_required
def delete_notification(request, id):
    notification = get_object_or_404(Notification, pk=id)

    if request.method == 'POST':  
        log_history(request.user, f"Deleted notification {notification.id}")
        notification.delete()
        return redirect('notifications') 

    return render(request, 'notifications/notifications.html', {'notifications': Notification.objects.filter(user=request.user)})

@login_required
def delete_all_notifications(request):
    notifications = Notification.objects.filter(user=request.user)
    count = notifications.count()  
    log_history(request.user, f"Deleted all notifications ({count} total)")

    if request.method == 'POST':
        notifications.delete()
        return redirect('notifications') 
    
    return render(request, 'notifications/notifications.html', {'notifications': notifications})


@login_required
def mark_notifications(request):
    notifications = Notification.objects.filter(user=request.user)

    if request.method == 'POST':
        count = notifications.count() 
        notifications.update(is_read=True)  
        
        log_history(request.user, f"Marked {count} notifications as read")
        return redirect('notifications')
    
    return render(request, 'notifications/notifications.html', {'notifications': notifications})

@login_required
def mark_as_read(request, notification_id):
    if request.method == "POST":
        notification = get_object_or_404(Notification, id=notification_id)
        notification.is_read = True
        notification.save()
        log_history(request.user, f"Marked notification {notification.id} as read")

        return JsonResponse({"success": True})
    return JsonResponse({"success": False}, status=400)


