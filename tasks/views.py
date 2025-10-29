from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Task
from .forms import TaskForm  
from django.http import JsonResponse
from notifications.models import Notification 
from users.models import UserHistory
from django.utils.timezone import now

# Create your views here.

def log_history(user, action):
    UserHistory.objects.create(user=user, action=action, timestamp=now())

def get_user_permissions(user):
    all_permissions = [
      
        'add_permission', 'change_permission', 'delete_permission', 'view_permission',
       
        
        'add_anomalyprediction', 'change_anomalyprediction', 'delete_anomalyprediction', 'view_anomalyprediction',
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
def task_list(request):
    tasks = Task.objects.filter(assigned_to=request.user)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread=Notification.objects.filter(user=request.user, is_read=False).count()
    log_history(request.user, "Viewed task list")
    permissions = get_user_permissions(request.user)

    return render(request, 'tasks/task_list.html',
    { 'tasks': tasks ,
      'notifications':notifications,
      'unread':unread,
      'permissions': permissions,

      })

@login_required
def task_create(request):
    if request.method == 'POST':
        form = TaskForm(request.POST)
        if form.is_valid():
            task = form.save(commit=False)
            task.assigned_to = request.user
            task.save()
            log_history(request.user, f"Created task '{task.title}'") 

            return redirect('task_list') 
    else:
        form = TaskForm()
    
    tasks = Task.objects.filter(assigned_to=request.user)  
    return render(request, 'tasks/task_list.html', {'form': form, 'tasks': tasks})

@login_required
def task_edit(request, task_id):
    task = get_object_or_404(Task, id=task_id, assigned_to=request.user)
    if request.method == 'POST':
        form = TaskForm(request.POST, instance=task)
        if form.is_valid():
            task = form.save(commit=False)
            task.assigned_to = request.user
            task.save()
            log_history(request.user, f"Edited task '{task.title}'")
            return redirect('task_list')
    else:
        form = TaskForm(instance=task)
    
    tasks = Task.objects.filter(assigned_to=request.user)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)
    
    return render(request, 'tasks/task_list.html', {
        'form': form,
        'tasks': tasks,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })

@login_required
def task_delete(request, task_id):
    if request.method == 'POST':
        task = get_object_or_404(Task, id=task_id)
        log_history(request.user, f"Deleted task '{task.title}'")
        task.delete()
        return JsonResponse({'status': 'success', 'message': 'Task deleted successfully'})
    return JsonResponse({'status': 'error', 'message': 'Invalid request method'}, status=400)