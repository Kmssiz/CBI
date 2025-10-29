from django.shortcuts import render, redirect
from django.http import JsonResponse
import uuid
import os
import tempfile
import time
from .adalog_inference import process_log_chunk
from users.models import UserHistory
from notifications.models import Notification

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


def upload_log(request):
    if request.method == 'POST':
        log_file = request.FILES.get('log_file')
        dataset_name = request.POST.get('dataset_name')
        if not log_file or not dataset_name:
            request.session['error'] = 'Missing log file or dataset name'
            return redirect('anomaly_detection:results')
        valid_datasets = ['RSPowerBI', 'RSPortal', 'RSHostingService']
        if dataset_name not in valid_datasets:
            request.session['error'] = f'Invalid dataset name. Choose from {valid_datasets}'
            return redirect('anomaly_detection:results')
        try:
            session_id = str(uuid.uuid4())
            with tempfile.NamedTemporaryFile(delete=False, suffix='.log') as temp_file:
                for chunk in log_file.chunks():
                    temp_file.write(chunk)
                temp_path = temp_file.name
            request.session['session_id'] = session_id
            request.session['temp_path'] = temp_path
            request.session['dataset_name'] = dataset_name
            request.session['current_chunk'] = 0
            request.session['results'] = []
            request.session['processing_done'] = False
            return redirect('anomaly_detection:results')
        except Exception as e:
            if 'temp_path' in locals() and os.path.exists(temp_path):
                try_delete_file(temp_path)
            request.session['error'] = f'Error processing file: {str(e)}'
            return redirect('anomaly_detection:results')
    permissions = get_user_permissions(request.user)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread=Notification.objects.filter(user=request.user, is_read=False).count()
    return render(request, 'anomaly_detection/upload.html', {'permissions': permissions, 'notifications':notifications,
        'unread':unread,})

def try_delete_file(file_path, retries=3, delay=1):
    """Attempt to delete a file with retries to handle access errors."""
    for attempt in range(retries):
        try:
            os.remove(file_path)
            return
        except OSError as e:
            if e.winerror == 32:  # File in use
                time.sleep(delay)
                continue
            raise
    raise OSError(f"Failed to delete {file_path} after {retries} attempts")

def process_next_chunk(request):
    if not request.session.get('session_id'):
        return JsonResponse({'error': 'No processing session found'})
    temp_path = request.session.get('temp_path')
    dataset_name = request.session.get('dataset_name')
    current_chunk = request.session.get('current_chunk', 0)
    if not temp_path or not dataset_name:
        return JsonResponse({'error': 'No file or dataset specified'})
    try:
        chunk_size = 1000
        lines = []
        with open(temp_path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(current_chunk * chunk_size * 100)  # Approximate line length
            for i, line in enumerate(f):
                if i >= chunk_size:
                    break
                lines.append(line)
        results = process_log_chunk(lines, dataset_name, batch_size=32)
        request.session['results'].extend(results)
        request.session['current_chunk'] = current_chunk + 1
        if len(lines) < chunk_size:
            request.session['processing_done'] = True
            try_delete_file(temp_path)
            del request.session['temp_path']
        request.session.modified = True
        return JsonResponse({
            'results': results,
            'done': request.session['processing_done']
        })
    except Exception as e:
        if temp_path and os.path.exists(temp_path):
            try_delete_file(temp_path)
        return JsonResponse({'error': str(e)})
def results(request):
    permissions = get_user_permissions(request.user)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread=Notification.objects.filter(user=request.user, is_read=False).count()
    context = {'error': request.session.get('error', None),'permissions': permissions, 'notifications':notifications,
        'unread':unread,}
    request.session.pop('error', None)
    return render(request, 'anomaly_detection/results.html', context)
