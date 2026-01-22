from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from .models import Task
from .forms import TaskForm  
from django.http import JsonResponse
from notifications.models import Notification 
from users.models import UserHistory
from django.utils.timezone import now
from users.utils import log_history, get_user_permissions


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