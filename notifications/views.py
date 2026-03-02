from django.shortcuts import render ,redirect,get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Notification
from django.http import JsonResponse
from users.models import UserHistory
from django.utils.timezone import now
from users.utils import get_user_permissions



def log_history(user, action):
    UserHistory.objects.create(user=user, action=action, timestamp=now())



@login_required
def notifications_list(request):
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread=Notification.objects.filter(user=request.user, is_read=False).count()
    # log_history(request.user, "Liste des notifications consultée")
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
        # log_history(request.user, f"Notification supprimée {notification.id}")
        notification.delete()
        
        if request.headers.get('Content-Type') == 'application/json':
            return JsonResponse({'success': True})
            
        return redirect('notifications') 

    return render(request, 'notifications/notifications.html', {'notifications': Notification.objects.filter(user=request.user)})

@login_required
def delete_all_notifications(request):
    notifications = Notification.objects.filter(user=request.user)
    count = notifications.count()  
    # log_history(request.user, f"Toutes les notifications supprimées ({count} au total)")

    if request.method == 'POST':
        notifications.delete()
        
        if request.headers.get('Content-Type') == 'application/json':
            return JsonResponse({'success': True})
            
        return redirect('notifications') 
    
    return render(request, 'notifications/notifications.html', {'notifications': notifications})


@login_required
def mark_notifications(request):
    notifications = Notification.objects.filter(user=request.user)

    if request.method == 'POST':
        count = notifications.count() 
        notifications.update(is_read=True)  
        
        # log_history(request.user, f"{count} notifications marquées comme lues")
        
        if request.headers.get('Content-Type') == 'application/json':
            return JsonResponse({'success': True})
            
        return redirect('notifications')
    
    return render(request, 'notifications/notifications.html', {'notifications': notifications})

@login_required
def mark_as_read(request, notification_id):
    if request.method == "POST":
        notification = get_object_or_404(Notification, id=notification_id)
        notification.is_read = True
        notification.save()
        # log_history(request.user, f"Notification {notification.id} marquée comme lue")

        return JsonResponse({"success": True})
    return JsonResponse({"success": False}, status=400)


