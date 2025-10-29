import os
import urllib.parse
import requests
import json
import pytz
from easyaudit.models import LoginEvent
from easyaudit.models import CRUDEvent, RequestEvent

from requests_ntlm import HttpNtlmAuth
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from notifications.models import Notification
from users.models import CustomUser,UserHistory,Role
from .models import Report , ReportAccess
from django.core.cache import cache
from django.db.models import Count
from requests_negotiate_sspi import HttpNegotiateAuth  
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime ,timedelta
from collections import Counter
from django.contrib import messages
from django.urls import reverse
from django.http import HttpResponseRedirect
from datetime import datetime

from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.db.models.functions import TruncDay


from django.contrib import admin
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.utils import timezone
from datetime import datetime, timedelta
from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncDay, TruncDate, TruncHour

REPORT_SERVER_URL = settings.POWERBI_REPORT_SERVER_URL


#################################################################################################################
#                    Logs user actions in the UserHistory model                                                 #
#################################################################################################################
def log_history(user, action):
    UserHistory.objects.create(user=user, action=action, timestamp=now())


#################################################################################################################
#                    Retrieves all permissions for a given user and returns them in a dictionary                #
#################################################################################################################
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

#################################################################################################################
#                    Retrieves NTLM authentication credentials for the current user                             #
#################################################################################################################

def get_current_user_auth(request):
    current_username = request.user.username
    current_password = request.session.get('ldap_password', None)
    if not current_password:
        print("LDAP password not found in session. Authentication might fail.")

    print(f"Authenticating as: {current_username}") 
    return HttpNtlmAuth(current_username, current_password)


#################################################################################################################
#                    Fetches all Power BI reports from the report server using API                              #
################################################################################################################# 

def get_powerbi_reports(request):
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache_key = f"powerbi_reports_cache_{user_id}"  
    cached_reports = cache.get(cache_key)

    if cached_reports:
        print(f"Returning cached Power BI reports for user {user_id}.")
        return cached_reports

    url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports"
    auth = get_current_user_auth(request)
    session = requests.Session()  
    session.auth = auth  

    try:
        response = session.get(url, auth=auth, timeout=10)  
        response.raise_for_status()

        reports = response.json().get('value', []) if response.status_code == 200 else []
        cache.set(cache_key, reports, timeout=200) 
        return reports
    except requests.exceptions.Timeout:
        print(f"Request timed out for user {user_id}.")
        return []
    except requests.exceptions.RequestException as err:
        print(f"Request Error for user {user_id}: {err}")
        return []

#################################################################################################################
#                    Retrieves permissions for a specific Power BI report                                       #
#################################################################################################################

def get_report_permissions(request, report_id):
    url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
    auth = get_current_user_auth(request)
    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        print(f"Policies API Response Code: {response.status_code}")
        print(f"Policies API Response Content: {response.text}")
        if response.status_code == 200:
            return response.json().get('Policies', [])
        else:
            return []
    except requests.exceptions.HTTPError as errh:
        print(f"HTTP Error (Permissions): {errh}")
        return []
    except requests.exceptions.RequestException as err:
        print(f"Request Error (Permissions): {err}")
        return []
#################################################################################################################
#                    Retrieves permissions for a specific folder on the report server                           #
#################################################################################################################

def get_folder_permissions(request, folder_id):
    url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/Folders({folder_id})/Policies"
    auth = get_current_user_auth(request)
    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        print(f"Folder Policies API Response Code: {response.status_code}")
        print(f"Folder Policies API Response Content: {response.text}")
        if response.status_code == 200:
            return response.json().get('Policies', [])
        else:
            return []
    except requests.exceptions.HTTPError as errh:
        print(f"HTTP Error (Folder Permissions): {errh}")
        return []
    except requests.exceptions.RequestException as err:
        print(f"Request Error (Folder Permissions): {err}")
        return []


#################################################################################################################
#                    Fetches folder structure from Power BI Report Server for jsTree                            #
#################################################################################################################
import requests
from django.http import JsonResponse
from django.views.decorators.http import require_GET

@require_GET
def get_folders(request):
    url = f"{settings.REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
    auth = get_current_user_auth(request)
    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        folders = response.json().get('value', [])
        # Extract folder paths
        folder_paths = [folder['Path'] for folder in folders if folder.get('Path')]
        return JsonResponse({'folders': folder_paths}, status=200)
    except requests.exceptions.RequestException as e:
        return JsonResponse({'error': f"Failed to fetch folders: {str(e)}"}, status=500)

from django.http import JsonResponse
import requests

def fetch_folders(request):
    """
    Fetch all folders from Power BI Report Server API.
    """
    try:
        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
        auth = get_current_user_auth(request)
        
        response = requests.get(url, auth=auth)
        response.raise_for_status()  # Raise an exception for bad status codes
        
        data = response.json()
        folders = data.get('value', [])  # Assuming the API returns folders in a 'value' key
        return JsonResponse({'folders': folders}, status=200)
    
    except requests.exceptions.RequestException as e:
        return JsonResponse({'error': f'Failed to fetch folders: {str(e)}'}, status=500)
        
def get_folder_list(request):
    """
    View to fetch all folders from Power BI Report Server.
    Returns a JSON response with the list of folder paths.
    """
    try:
        # Get authentication credentials
        auth = get_current_user_auth(request)
        if not auth:
            return JsonResponse({'error': 'Authentication failed'}, status=401)

        # Power BI Report Server API URL for folders
        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"

        # Make GET request to fetch folders
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()  # Raise exception for bad status codes

        # Parse the response JSON
        folders = response.json().get('value', [])
        
        # Extract folder paths
        folder_paths = [folder['Path'] for folder in folders if folder.get('Path')]
        print('*********************************************************************************************************************')
        print('**** start ************************************************************************************************************')

        return JsonResponse({'folders': folder_paths}, status=200)

    except requests.exceptions.RequestException as e:
        return JsonResponse({'error': f'Failed to fetch folders: {str(e)}'}, status=500)
    except Exception as e:
        return JsonResponse({'error': f'An unexpected error occurred: {str(e)}'}, status=500)
#################################################################################################################
#                    Displays a list of Power BI reports for admin users                                        #
#################################################################################################################
@login_required
def report_list(request):
   
    reports = get_powerbi_reports(request)
    base_embed_url = f"{REPORT_SERVER_URL}/Reports/powerbi/"

    for report in reports:
        path = report.get("Path", "")
        if path.startswith("/"):
            path = path[1:]  
        encoded_path = urllib.parse.quote(path, safe="/")
        report["embed_url"] = f"{base_embed_url}{encoded_path}?rs:embed=true"
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)
    log_history(request.user, "Viewed Power BI report management ")

    return render(request, 'powerbi_report/report_list.html', {'reports': reports,'notifications': notifications,'unread': unread,'permissions': permissions,})


#################################################################################################################
#                    Retrieves information for a specific Power BI report                                       #
#################################################################################################################

def get_powerbi_report_info(request, report_id):
    # Construct the API endpoint URL
    url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})"
    
    try:
        # Get current user's authentication
        auth = get_current_user_auth(request)

        # Send GET request
        response = requests.get(url, auth=auth, headers={"Accept": "application/json"})
        
        # Debug output
        print(f"API Response Code: {response.status_code}")
        print(f"API Response Content: {response.text}")
        
        # Parse response
        if response.status_code == 200:
            data = response.json()
            return {
                "name": data.get("Name"),
                "path": data.get("Path")
            }
        else:
            return None
        
    except requests.exceptions.RequestException as e:
        print(f"Failed to retrieve report info for ID '{report_id}': {e}")
        return None
#################################################################################################################
#                    Updates the name of a Power BI report and notifies admin users                             #
#################################################################################################################
@login_required
def edit_powerbi_report_name(request, report_id):
    if request.method == "POST":
        new_name = request.POST.get("name")
        if not new_name:
            messages.error(request, "Please provide a new report name.")
            return redirect('powerbi_report:report_list')

        update_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})"
        auth = get_current_user_auth(request)
        data = {"Name": new_name}
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.patch(update_url, json=data, auth=auth, headers=headers)
            response.raise_for_status()
            messages.success(request, "Report name updated successfully!")
            # Record log
            log_history(request.user, f"Updated report name to '{new_name}' for report ID '{report_id}'")

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Report with ID '{report_id}' has been renamed to '{new_name}' by {request.user.username}."
                )
            print(f"Sent notifications to {len(admin_users)} admin users about renaming report ID '{report_id}' to '{new_name}'.")
            # Clear the cache for the current user
            user_id = request.user.id
            cache_key = f"powerbi_reports_cache_{user_id}"
            cache.delete(cache_key)
            print(f"Cleared cache for user {user_id} after uploading report.")

        except requests.exceptions.RequestException as e:
            messages.error(request, f"Failed to update report name: {e}")
        return redirect('powerbi_report:report_list')

    return redirect('powerbi_report:report_list')

#################################################################################################################
#                    Updates the path of a Power BI report                                                      #
#################################################################################################################

def edit_powerbi_report_path(request, report_id):
    if request.method == "POST":
        new_path = request.POST.get("path")
        if not new_path:
            messages.error(request, "Please provide a new report path.")
            return redirect('powerbi_report:report_list')

        update_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})"
        auth = get_current_user_auth(request)
        data = {"Path": new_path}
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.patch(update_url, json=data, auth=auth, headers=headers)
            response.raise_for_status()
            user_id = request.user.id if request.user.is_authenticated else "anonymous"
            cache_key = f"powerbi_reports_cache_{user_id}"
            cache.delete(cache_key)
            messages.success(request, "Report path updated successfully!")
        except requests.exceptions.RequestException as e:
            messages.error(request, f"Failed to update report path: {e}")
        return redirect('powerbi_report:report_list')

    return redirect('powerbi_report:report_list')


#################################################################################################################
#                    Updates the description of a Power BI report and notifies admin users                      #
#################################################################################################################

def edit_powerbi_report_description(request, report_id):
    if request.method == "POST":
        new_description = request.POST.get("description")
        if not new_description:
            messages.error(request, "Please provide a new report description.")
            return redirect('powerbi_report:report_detail', report_id=report_id)

        update_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})"
        auth = get_current_user_auth(request)  
        data = {"Description": new_description}
        headers = {"Content-Type": "application/json"}
        info = get_powerbi_report_info(report_id)
        try:
            response = requests.patch(update_url, json=data, auth=auth, headers=headers)
            response.raise_for_status()
            user_id = request.user.id if request.user.is_authenticated else "anonymous"
            cache_key = f"powerbi_reports_cache_{user_id}"
            cache.delete(cache_key)
            
            messages.success(request, "Report description updated successfully!")
            # log_history(request.user, f"Updated description for report {info["name"]} ID: {report_id}  path {info["path"]} to '{new_description}'")
            
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Updated description for report: (ID: {report_id}) with description '{new_description}' by {request.user.username}."
                )
        except requests.exceptions.RequestException as e:
            messages.error(request, f"Failed to update report description: {e}")
        return redirect('powerbi_report:report_detail', report_id=report_id)

    return redirect('powerbi_report:report_detail', report_id=report_id)


#################################################################################################################
#                    Embeds a Power BI report for viewing in the browser                                        #
#################################################################################################################

@login_required
def embed_report(request, report_path):
    auth = get_current_user_auth(request)
    session = requests.Session()
    session.auth = auth  

    report_path = report_path.strip('/')
    encoded_path = urllib.parse.quote(report_path, safe="/")
    embed_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/powerbi/{encoded_path}?rs:embed=true"

    reports = get_powerbi_reports(request)

    # Find the report in the reports list
    report = next((r for r in reports if r["Path"].strip('/') == report_path), None)
    report_id = report["Id"] if report else None
    report_name = report_path.split('/')[-1] if report else report_path  

    breadcrumbs = []
    path_parts = report_path.split('/')
    current_path = ""

    for part in path_parts:
        current_path = f"{current_path}/{part}" if current_path else part
        breadcrumbs.append({
            "name": part,
            "url": urllib.parse.quote(current_path, safe="/")
        })

    try:
        response = session.get(embed_url)
        response.raise_for_status()

        notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
        unread = Notification.objects.filter(user=request.user, is_read=False).count()
        log_history(request.user, f"Viewed Power BI report: {report_path} (ID: {report_id})")
        permissions = get_user_permissions(request.user)

        return render(request, "powerbi_report/embed_report.html", {
            "embed_url": embed_url,
            "report_id": report_id,  
            "breadcrumbs": breadcrumbs,
            "notifications": notifications,
            "unread": unread,
            'permissions': permissions,
        })

    except requests.exceptions.RequestException as e:
        return render(request, "powerbi_report/embed_report.html", {
            "error": f"Erreur lors du chargement du rapport : {e}",
            "report_id": report_id, 
            "breadcrumbs": breadcrumbs,
        })

#########################################################################################
#                                  download report                                      #
#########################################################################################

from django.http import StreamingHttpResponse, HttpResponse
from django.conf import settings
from requests_ntlm import HttpNtlmAuth
import requests 

def download_report(request, report_id):
    url = f'{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Content/$value'
       
    auth = get_current_user_auth(request)
    info = get_powerbi_report_info(request, report_id)  # Pass request here

    if not info:
        messages.error(request, "Failed to retrieve report information.")
        return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

    try:
        response = requests.get(url, auth=auth, headers={'Accept': 'application/octet-stream'}, stream=True)
        
        if response.status_code == 200:
            file_response = StreamingHttpResponse(
                response.iter_content(chunk_size=8192),
                content_type='application/octet-stream'
            )
            file_response['Content-Disposition'] = f'attachment; filename="report_{info["name"]}.pbix"'
            log_history(request.user, f"Successfully download report {info['name']} from {info['path']}.")
            return file_response
        else:
            messages.error(request, f"Failed to retrieve report. Status code: {response.status_code}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
    
    except Exception as e:
        messages.error(request, f"Error downloading report: {str(e)}")
        return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
#################################################################################################################
#                    Displays a flat list of Power BI reports for authenticated users                           #
#################################################################################################################

@login_required
def report_list_flat(request):
    reports = get_powerbi_reports(request)
    base_embed_url = f"{REPORT_SERVER_URL}/Reports/powerbi/"
    for report in reports:
        path = report.get("Path", "")
        if path.startswith("/"):
            path = path[1:]
        encoded_path = urllib.parse.quote(path, safe="/")
        report["embed_url"] = f"{base_embed_url}{encoded_path}?rs:embed=true"
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    log_history(request.user, "Viewed Power BI report list (flat)")
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_list_flat.html', {
        'reports': reports,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })


#################################################################################################################
#                    Displays a hierarchical list of Power BI reports for authenticated users                   #
#################################################################################################################

@login_required
def report_list_hierarchy(request, folder_path=""):
    force_refresh = request.GET.get('force_refresh', 'false').lower() == 'true'
    reports = get_powerbi_reports(request)
    base_embed_url = f"{REPORT_SERVER_URL}/Reports/powerbi/"
    
    folder_dict = {}

    for report in reports:
        path = report.get("Path", "").strip("/")
        parts = path.split("/")
        
        current_level = folder_dict
        for part in parts[:-1]:  
            current_level = current_level.setdefault(part, {})

        report_name = parts[-1]
        encoded_path = urllib.parse.quote(path, safe="/")
        current_level[report_name] = f"{base_embed_url}{encoded_path}?rs:embed=true"

    current_folder = folder_dict
    breadcrumbs = []
    if folder_path:
        parts = folder_path.strip("/").split("/")
        for idx, part in enumerate(parts):
            breadcrumbs.append({
                "name": part,
                "url": "/".join(parts[: idx + 1])
            })
            current_folder = current_folder.get(part, {})

    folder_path = folder_path.rstrip('/')
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
     # Log only when folder_path is root (empty or "/")
    if not folder_path or folder_path == "/":
        log_history(request.user, "Viewed Power BI report list (hierarchy)")

    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_list_hierarchy.html', {
        'folder_structure': current_folder,
        'breadcrumbs': breadcrumbs,
        'current_path': folder_path,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })


#################################################################################################################
#                    Uploads a Power BI report to the report server and notifies admin users                    #
#################################################################################################################

import requests
from django.shortcuts import redirect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings

@login_required
def upload_powerbi_report(request):
    if not request.user.has_perm('powerbi_report.add_powerbireport'):
        messages.error(request, "You do not have permission to upload reports.")
        return redirect('powerbi_report:report_list_hierarchy')

    if request.method == 'POST':
        # Get form data
        pbix_file = request.FILES.get('pbix_file')
        folder_path = request.POST.get('parent_folder', '').strip('/')
        report_name = request.POST.get('report_name', '').strip()

        if not pbix_file or not report_name:
            messages.error(request, "Please provide a report name and a valid .pbix file.")
            return redirect('powerbi_report:report_list_hierarchy_folder', folder_path=folder_path)

        # Construct the report path
        report_path = f"/{folder_path}/{report_name}" if folder_path else f"/{report_name}"
        encoded_path = report_path.replace("'", "''").replace(" ", "%20")
        api_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports(path='{encoded_path}')/Model.Upload"

        # Setup authentication using get_current_user_auth
        auth = get_current_user_auth(request)
        session = requests.Session()
        session.auth = auth

        # Prepare the file for upload
        files = {
            'file': (pbix_file.name, pbix_file.read(), 'application/octet-stream')
        }

        headers = {
            'Accept': 'application/json'
        }

        try:
            # Perform the POST request to upload the PBIX file
            response = session.post(api_url, headers=headers, files=files)
            response.raise_for_status()
            messages.success(request, f"Report '{report_name}' uploaded successfully.")
            log_history(request.user, f"Uploaded Power BI report: {report_path}")
            # Clear the cache for the current user
            user_id = request.user.id
            cache_key = f"powerbi_reports_cache_{user_id}"
            cache.delete(cache_key)
            print(f"Cleared cache for user {user_id} after uploading report.")

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"A new report '{report_name}' has been uploaded to {report_path} by {request.user.username}."
                )
            print(f"Sent notifications to {len(admin_users)} admin users about new report '{report_name}'.")

        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to upload report. HTTP Error: {errh}")
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to upload report. Request Error: {err}")

        return redirect('powerbi_report:report_list_hierarchy_folder', folder_path=folder_path)

    # Redirect GET requests to the report list
    return redirect('powerbi_report:report_list_hierarchy')


#################################################################################################################
#                    Lists folders and reports in a specified folder for authenticated users                    #
#################################################################################################################
@login_required
def report_folders_list(request, folder_path=""):
    reports = get_powerbi_reports(request)
    base_embed_url = f"{REPORT_SERVER_URL}/Reports/powerbi/"
    folder_dict = {}
    report_dict = {}  # Separate dictionary for reports

    # Process reports
    for report in reports:
        path = report.get("Path", "").strip("/")
        if folder_path:
            # Only include reports directly in the specified folder
            if path.startswith(folder_path + "/") and path.count("/") == folder_path.count("/") + 1:
                report_name = path.split("/")[-1]
                encoded_path = urllib.parse.quote(path, safe="/")
                report_dict[report_name] = {
                    "url": f"{base_embed_url}{encoded_path}?rs:embed=true",
                    "type": "report"
                }
        else:
            # For root level, include reports without any folder prefix
            if "/" not in path:
                report_name = path
                encoded_path = urllib.parse.quote(path, safe="/")
                report_dict[report_name] = {
                    "url": f"{base_embed_url}{encoded_path}?rs:embed=true",
                    "type": "report"
                }

    # Fetch folders from the report server
    url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
    auth = get_current_user_auth(request)
    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        all_folders = response.json().get('value', [])
    except requests.RequestException as err:
        print(f"Error fetching folders: {err}")
        all_folders = []

    # Process folders
    for folder in all_folders:
        path = folder.get("Path", "").strip("/")
        if folder_path:
            # Only include folders directly under the specified folder_path
            if path.startswith(folder_path + "/") and path.count("/") == folder_path.count("/") + 1:
                folder_name = path.split("/")[-1]
                folder_id = folder.get("Id") or folder.get("id")
                folder_dict[folder_name] = {'id': folder_id, 'type': 'folder'}
        else:
            # For root level, include folders without any parent
            if "/" not in path:
                folder_name = path
                folder_id = folder.get("Id") or folder.get("id")
                folder_dict[folder_name] = {'id': folder_id, 'type': 'folder'}

    # Combine folders and reports into a single structure
    current_folder_structure = {**folder_dict, **report_dict}

    # Build breadcrumbs
    breadcrumbs = []
    if folder_path:
        parts = folder_path.strip("/").split("/")
        for idx, part in enumerate(parts):
            breadcrumbs.append({
                "name": part,
                "url": "/".join(parts[: idx + 1])
            })

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()

    log_history(request.user, "Viewed Power BI report folders list")
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_folders_list.html', {
        'folder_structure': current_folder_structure,
        'breadcrumbs': breadcrumbs,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
        'current_folder': folder_path,
    })


#################################################################################################################
#                    Creates a new folder in the Power BI Report Server and notifies admin users                 #
#################################################################################################################
@login_required
def add_powerbi_folder(request):
    if request.method == "POST":
        folder_name = request.POST.get("folder_name", "").strip()
        parent_folder = request.POST.get("parent_folder", "").strip()

        if not folder_name:
            messages.error(request, "Folder name is required.")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
        auth = get_current_user_auth(request)
        session = requests.Session()
        session.auth = auth

        payload = {
            "Name": folder_name,
            "Path": f"{parent_folder}/{folder_name}" if parent_folder else f"/{folder_name}"
        }

        try:
            response = session.post(url, json=payload)
            response.raise_for_status()
            # Record log
            log_history(request.user, f"Created folder '{folder_name}'")

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            folder_path = payload["Path"]
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"A new folder '{folder_name}' has been created in '{folder_path}' by {request.user.username}."
                )
            
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to create folder due to HTTP error: {str(errh)}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to create folder due to request error: {str(err)}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

    return render(request, "powerbi_report/report_folders_list.html")

#################################################################################################################
#                    Deletes a folder from the Power BI Report Server and notifies admin users                   #
#################################################################################################################
@login_required
def delete_powerbi_folder(request, folder_id):
    if request.method == "POST":
        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders({folder_id})"
        auth = get_current_user_auth(request)
        session = requests.Session()
        session.auth = auth

        try:
            response = session.delete(url)
            response.raise_for_status()
            # Record log
            log_history(request.user, f"Deleted folder with ID '{folder_id}'")

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Folder with ID '{folder_id}' has been deleted by {request.user.username}."
                )
            
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to delete folder due to HTTP error: {str(errh)}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to delete folder due to request error: {str(err)}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

    messages.error(request, "Invalid request method. Please use POST to delete a folder.")
    return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))


#################################################################################################################
#                    Fetches cache refresh plans for a specific Power BI reporte                               #
#################################################################################################################

def get_refresh_plans(report_id, request):
    url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/CacheRefreshPlans"
    auth = get_current_user_auth(request)

    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        data = response.json()
        print(f"data fetching CacheRefreshPlans: {data}")

        return data.get("value", []) 
    except requests.exceptions.RequestException as err:
        print(f"Error fetching CacheRefreshPlans: {err}")

        return []
from django.contrib import messages

#################################################################################################################
#                    Displays detailed information about a specific Power BI report                             #
#################################################################################################################


@login_required
def report_detail(request, report_id):
    reports = get_powerbi_reports(request)
    report = next((r for r in reports if r['Id'] == report_id), None)

    if not report:
        log_history(request.user, f"Attempted to view non-existent report ID: {report_id}")
        raise Http404("Report not found")
    log_history(request.user, f"Viewed Power BI report details: {report.get('Name', 'Unknown')} (ID: {report_id})")
    refresh_plans = get_refresh_plans(report_id, request)

    for plan in refresh_plans:
        if isinstance(plan['LastRunTime'], str):
            try:
                plan['LastRunTime'] = datetime.fromisoformat(plan['LastRunTime']).strftime('%Y-%m-%d %H:%M')
            except ValueError:
                plan['LastRunTime'] = "Invalid date"

    if request.method == "POST" and "refresh_plan_id" in request.POST:
        refresh_plan_id = request.POST["refresh_plan_id"]
        action = request.POST.get("action") 
        auth = get_current_user_auth(request)

        if not auth:
            messages.error(request, "Authentication failed.")
            log_history(request.user, f"Authentication failed for {action} action on refresh plan ID: {refresh_plan_id} for report ID: {report_id}")
        else:
            if action == "refresh":
                refresh_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/CacheRefreshPlans({refresh_plan_id})/Model.Execute"
                try:
                    response = requests.post(refresh_url, auth=auth, headers={"Content-Type": "application/json"})
                    response.raise_for_status()
                    messages.success(request, "Refresh started successfully!")
                    log_history(request.user, f"Initiated refresh for plan ID: {refresh_plan_id} on report ID: {report_id}")
                    
                    # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Initiated refresh for plan ID: {refresh_plan_id} on report ID: {report_id} by {request.user.username}."
                        )
                    return redirect('powerbi_report:report_detail', report_id=report_id)

                except requests.exceptions.RequestException as err:
                    print(f"Refresh Error: {err}")
                    messages.error(request, "Failed to refresh report.")
                    log_history(request.user, f"Failed to refresh plan ID: {refresh_plan_id} for report ID: {report_id}. Error: {str(err)}")
                    return redirect('powerbi_report:report_detail', report_id=report_id)

            elif action == "delete":
                delete_url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/CacheRefreshPlans({refresh_plan_id})"
                try:
                    response = requests.delete(delete_url, auth=auth)
                    response.raise_for_status()
                    messages.success(request, "Refresh plan deleted successfully!")
                    log_history(request.user, f"Deleted refresh plan ID: {refresh_plan_id} for report ID: {report_id}")
                     # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Deleted refresh plan ID: {refresh_plan_id} for report ID: {report_id} by {request.user.username}."
                        )
                    return redirect('powerbi_report:report_detail', report_id=report_id)

                except requests.exceptions.RequestException as err:
                    print(f"Delete Error: {err}")
                    messages.error(request, "Failed to delete refresh plan.")
                    log_history(request.user, f"Failed to delete refresh plan ID: {refresh_plan_id} for report ID: {report_id}. Error: {str(err)}")
                    return redirect('powerbi_report:report_detail', report_id=report_id)

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_detail.html', {
        'notifications': notifications,
        'report': report,
        'refresh_plans': refresh_plans,  
        'unread': unread,
        'permissions': permissions,
    })



#################################################################################################################
#                    Adds a new cache refresh plan for a Power BI report                                        #
#################################################################################################################


def add_refresh_plan(request, report_id):
    if request.method == 'POST':
        # Define server URL and credentials
        url = f"{settings.POWERBI_REPORT_SERVER_URL}/reports/api/v2.0/CacheRefreshPlans"
        
        auth = get_current_user_auth(request)

        # Get form data
        description = request.POST.get('description', 'Default Refresh Plan')
        start_datetime = request.POST.get('start_datetime', '2025-04-27T02:00')
        days_interval = request.POST.get('days_interval', 1)
        catalog_item_path = request.POST.get('catalog_item_path', '')

        # Convert start_datetime to Edm.DateTimeOffset format
        try:
            # Parse the input (e.g., '2025-04-27T02:00')
            dt = datetime.strptime(start_datetime, '%Y-%m-%dT%H:%M')
            # Format as ISO 8601 with seconds and timezone (e.g., '2025-04-27T02:00:00+02:00')
            start_datetime_formatted = dt.strftime('%Y-%m-%dT%H:%M:%S+02:00')
        except ValueError as e:
            messages.error(request, f"Invalid datetime format: {str(e)}")
            return HttpResponseRedirect(reverse('powerbi_report:report_detail', args=[report_id]))

        # Build the payload
        payload = {
            "Owner": None,
            "Description": description,
            "CatalogItemPath": catalog_item_path,
            "EventType": "DataModelRefresh",
            "Schedule": {
                "ScheduleID": None,
                "Definition": {
                    "StartDateTime": start_datetime_formatted,
                    "EndDateSpecified": False,
                    "EndDate": "1901-02-01T00:00:00+01:00",
                    "Recurrence": {
                        "DailyRecurrence": {
                            "@odata.type": "#Model.DailyRecurrence",
                            "DaysInterval": int(days_interval)
                        }
                    }
                }
            },
            "ScheduleDescription": "",
            "ParameterValues": []
        }

        try:
            # Send the POST request
            response = requests.post(
                url,
                auth=auth,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload)
            )
            info = get_powerbi_report_info(report_id)

            if response.status_code == 201:
                messages.success(request, "Refresh plan added successfully.")
                log_history(request.user, f"Added refresh plan for (ID: {report_id}) with description '{description}' and interval {days_interval} day(s)")
                admin_users = CustomUser.objects.filter(is_superuser=True)
                for admin in admin_users:
                    Notification.objects.create(
                        user=admin,
                        message=f"A new refresh plan for reportn '{description}' was added by {request.user.username}."
                    )
            else:
                messages.error(request, f"Failed to add refresh plan: {response.text}")
        except Exception as e:
            messages.error(request, f"Error adding refresh plan: {str(e)}")

        # Redirect back to the report details page
        return HttpResponseRedirect(reverse('powerbi_report:report_detail', args=[report_id]))

    return render(request, 'powerbi_report/report_details.html', {'report_id': report_id})   


#################################################################################################################
#                    Displays permissions for a specific Power BI report                                        #
################################################################************************************************#


from django.shortcuts import render
from django.http import Http404
import requests
import json
# Assuming these are defined elsewhere in your codebase
from decouple import config

'''
LDAP_GROUP_MEMBERS_URL = config('LDAP_GROUP_MEMBERS_URL')
LDAP_API_TOKEN = config('LDAP_API_TOKEN')

def get_group_members(group_name, visited_groups=None, depth=0, max_depth=10):
    """
    Recursively fetch members of a group using the LDAP API, handling sub-groups and avoiding circular references.
    
    Args:
        group_name (str): Name of the group to query.
        visited_groups (set): Set of groups already visited to avoid circular references.
        depth (int): Current recursion depth to prevent excessive recursion.
        max_depth (int): Maximum recursion depth to prevent stack overflow.
    
    Returns:
        set: Set of unique member usernames (individuals, not groups).
    """
    if visited_groups is None:
        visited_groups = set()
    
    # Prevent circular references
    if group_name in visited_groups:
        print(f"  Circular reference detected for group: {group_name}")
        return set()
    
    # Prevent excessive recursion
    if depth >= max_depth:
        print(f"  Max recursion depth reached for group: {group_name}")
        return set()
    
    visited_groups.add(group_name)
    
    # Construct API URL
    url = f"{LDAP_GROUP_MEMBERS_URL}/{group_name}?token={LDAP_API_TOKEN}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        members = data.get("members", [])
        if not members:
            return set()
        
        unique_members = set()
        
        for member in members:
            # Check if member is a group by attempting to fetch its members
            sub_url = f"{LDAP_GROUP_MEMBERS_URL}/{member}?token={LDAP_API_TOKEN}"
            try:
                sub_response = requests.get(sub_url)
                if sub_response.status_code == 200 and "members" in sub_response.json():
                    # Member is a group; recursively fetch its members
                    sub_members = get_group_members(member, visited_groups.copy(), depth + 1, max_depth)
                    unique_members.update(sub_members)
                else:
                    # Member is an individual
                    unique_members.add(member.replace("GROUPE-HASNAOUI\\", ""))
            except requests.RequestException:
                # Assume member is an individual
                unique_members.add(member.replace("GROUPE-HASNAOUI\\", ""))
        
        return unique_members
    
    except requests.RequestException as e:
        print(f"  Error fetching group {group_name}: {e}")
        return set()
    finally:
        visited_groups.discard(group_name)

'''



from django.core.cache import cache

LDAP_GROUP_MEMBERS_URL = config('LDAP_GROUP_MEMBERS_URL')
LDAP_API_TOKEN = config('LDAP_API_TOKEN')

def get_group_members(group_name, visited_groups=None, depth=0, max_depth=10):
   
    if visited_groups is None:
        visited_groups = set()
    
    # Check cache first
    cache_key = f"group_members:{group_name}"
    cached_members = cache.get(cache_key)
    if cached_members is not None:
        print(f"  Cache hit for group: {group_name}")
        return set(cached_members)
    
    # Prevent circular references
    if group_name in visited_groups:
        print(f"  Circular reference detected for group: {group_name}")
        return set()
    
    # Prevent excessive recursion
    if depth >= max_depth:
        print(f"  Max recursion depth reached for group: {group_name}")
        return set()
    
    visited_groups.add(group_name)
    
    # Construct API URL
    url = f"{LDAP_GROUP_MEMBERS_URL}/{group_name}?token={LDAP_API_TOKEN}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        members = data.get("members", [])
        if not members:
            cache.set(cache_key, [], timeout=86400)  # Cache empty result for 24 hours
            return set()
        
        unique_members = set()
        
        for member in members:
            # Clean the member name by removing the domain prefix
            cleaned_member = member.replace("GROUPE-HASNAOUI\\", "")
            
            # Check if the member is a user in the CustomUser model
            if CustomUser.objects.filter(ad2000=cleaned_member).exists():
                # Member is a user, add to unique_members
                unique_members.add(cleaned_member)
            else:
                # Check if the member is likely a group (all uppercase or contains hyphen)
                is_potential_group = cleaned_member.isupper() or '-' in cleaned_member
                if is_potential_group:
                    # Member might be a group; attempt to fetch its members
                    sub_url = f"{LDAP_GROUP_MEMBERS_URL}/{cleaned_member}?token={LDAP_API_TOKEN}"
                    try:
                        sub_response = requests.get(sub_url)
                        if sub_response.status_code == 200 and "members" in sub_response.json():
                            # Member is a group; recursively fetch its members
                            sub_members = get_group_members(cleaned_member, visited_groups.copy(), depth + 1, max_depth)
                            unique_members.update(sub_members)
                        else:
                            # Not a group, treat as individual
                            unique_members.add(cleaned_member)
                    except requests.RequestException:
                        # Failed to fetch as a group, treat as individual
                        unique_members.add(cleaned_member)
                        print(f"  Error checking if {cleaned_member} is a group, treating as individual")
                else:
                    # Not a group by naming convention, treat as individual
                    unique_members.add(cleaned_member)
        
        # Recursively resolve any remaining groups in unique_members
        def resolve_groups(members_set, visited_groups, depth, max_depth):
            final_members = set()
            groups_found = False
            
            for member in members_set:
                # Check if the member is a user in CustomUser
                if CustomUser.objects.filter(ad2000=member).exists():
                    final_members.add(member)
                else:
                    # Check if the member is likely a group
                    is_potential_group = member.isupper() or '-' in member
                    if is_potential_group:
                        sub_url = f"{LDAP_GROUP_MEMBERS_URL}/{member}?token={LDAP_API_TOKEN}"
                        try:
                            sub_response = requests.get(sub_url)
                            if sub_response.status_code == 200 and "members" in sub_response.json():
                                # Member is a group; fetch its members
                                groups_found = True
                                sub_members = get_group_members(member, visited_groups.copy(), depth + 1, max_depth)
                                final_members.update(sub_members)
                            else:
                                # Not a group, add as individual
                                final_members.add(member)
                        except requests.RequestException:
                            # Failed to fetch as a group, treat as individual
                            final_members.add(member)
                            print(f"  Error checking if {member} is a group in final check, treating as individual")
                    else:
                        # Not a group by naming convention, add as individual
                        final_members.add(member)
            
            # If groups were found, recursively resolve again
            if groups_found:
                return resolve_groups(final_members, visited_groups, depth, max_depth)
            return final_members
        
        # Call recursive resolution to ensure no groups remain
        final_members = resolve_groups(unique_members, visited_groups.copy(), depth, max_depth)
        
        # Cache the final result (list for JSON serialization)
        cache.set(cache_key, list(final_members), timeout=86400)  # Cache for 24 hours
        print(f"  Cached members for group: {group_name}")
        
        return final_members
    
    except requests.RequestException as e:
        print(f"  Error fetching group {group_name}: {e}")
        return set()
    finally:
        visited_groups.discard(group_name)


def report_permissions(request, report_id):
    reports = get_powerbi_reports(request)
    report = next((r for r in reports if r['Id'] == report_id), None)
    if not report:
        log_history(request.user, f"Attempted to view permissions for non-existent report ID: {report_id}")
        raise Http404("Report not found")
    
    report_name = report.get('Name', 'Unknown Report') 
    log_history(request.user, f"Viewed permissions for Power BI report: {report_name} (ID: {report_id})") 

    policies = get_report_permissions(request, report_id)
    if not policies:
        parent_folder_id = report.get("ParentFolderId")
        if parent_folder_id:
            policies = get_folder_permissions(request, parent_folder_id)

    # Process policies to resolve groups and map usernames to full names
    processed_policies = []
    for policy in policies:
        identifier = policy.get("GroupUserName", policy.get("UserName", ""))
        identifier = identifier.replace("GROUPE-HASNAOUI\\", "")
        
        # Check if the identifier is a group by attempting to fetch its members
        url = f"{LDAP_GROUP_MEMBERS_URL}/{identifier}?token={LDAP_API_TOKEN}"
        try:
            response = requests.get(url)
            if response.status_code == 200 and "members" in response.json():
                # Identifier is a group; fetch its members
                members = get_group_members(identifier)
                for member in members:
                    user = CustomUser.objects.filter(ad2000=member).first()
                    processed_policies.append({
                        **policy,
                        "UserName": member,
                        "FullName": user.get_full_name() if user else member,
                        "IsGroupMember": True
                    })
            else:
                # Identifier is an individual user
                user = CustomUser.objects.filter(ad2000=identifier).first()
                processed_policies.append({
                    **policy,
                    "UserName": identifier,
                    "FullName": user.get_full_name() if user else identifier,
                    "IsGroupMember": False
                })
        except requests.RequestException:
            # Assume identifier is an individual user
            user = CustomUser.objects.filter(ad2000=identifier).first()
            processed_policies.append({
                **policy,
                "UserName": identifier,
                "FullName": user.get_full_name() if user else identifier,
                "IsGroupMember": False
            })

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_permissions.html', {
        'report_id': report_id,
        'report_name': report_name,  
        'policies': processed_policies,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })





#################################################################################################################
#                    Adds a specific user to a Power BI report's permissions                                    #
#################################################################################################################

from django.core.exceptions import ObjectDoesNotExist

@login_required
def add_users_to_report(request, report_id, username):
    
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        allowed_account = username  
        
        current_policies = get_report_permissions(request, report_id)
        if not isinstance(current_policies, list):
            current_policies = []
        
        permission_exists = any(
            policy.get("GroupUserName", "").lower() == allowed_account.lower()
            for policy in current_policies
        )
        
        if not permission_exists:
            try:
                user = CustomUser.objects.get(ad2000=username)  
            except ObjectDoesNotExist:
                messages.error(request, f"User with username '{username}' does not exist.")
                return redirect('powerbi_report:missing_users', report_id=report_id)
            
            if user.role and user.role.name.lower() == "admin": 
                roles = [
                    {"Name": "Explorateur"},
                    {"Name": "Gestionnaire de contenu"},
                    {"Name": "Mes rapports"},
                    {"Name": "Report Builder"},
                    {"Name": "Serveur de publication"}
                ]
            else:
                roles = [{"Name": "Explorateur"}]
            
            new_policy = {
                "GroupUserName": allowed_account,
                "Roles": roles
            }
            current_policies.append(new_policy)
        
        payload = {
            "Id": report_id,
            "Policies": current_policies
        }
        headers = {"Content-Type": "application/json"}
        
        print("PUT URL:", url)
        print("Payload:", payload)
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            messages.success(request, f"Successfully added permissions for user {username} to report.")
            log_history(request.user, f"Added permission for user {username} to report (ID: {report_id}) with roles {', '.join(role['Name'] for role in roles)}")
            # Notify the affected user
            Notification.objects.create(
                user=user,
                message=f"You have been granted access to the report (ID:{report_id}) by {request.user.username}."
            )

            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"User {username} has been granted access to report (ID: {report_id}) by {request.user.username}."
                )
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to add permission due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to add permission due to request error: {str(err)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
    
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_users', report_id=report_id)



#################################################################################################################
#                    Adds multiple selected users to a Power BI report's permissions                            #
#################################################################################################################
@login_required
def add_selected_users_to_report(request, report_id):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        selected_users = request.POST.getlist('selected_users')
        
        if not selected_users:
            messages.error(request, "No users selected for addition.")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = get_report_permissions(request, report_id) or []
        
        if not isinstance(current_policies, list):
            current_policies = []
        
        new_users_added = []
        for username in selected_users:
            if not any(policy.get("GroupUserName", "").lower() == username.lower() for policy in current_policies):
                new_policy = {
                    "GroupUserName": username,
                    "Roles": [{"Name": "Explorateur"}]
                }
                current_policies.append(new_policy)
                new_users_added.append(username)

        if not new_users_added:
            log_history(request.user, f"No new users added to report (ID: {report_id}) as all selected users already have access")
            messages.warning(request, "All selected users already have access to the report.")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        
        payload = {"Id": report_id, "Policies": current_policies}
        headers = {"Content-Type": "application/json"}
        info = get_powerbi_report_info(request, report_id)  # Pass request here

        if not info:
            messages.error(request, "Failed to retrieve report information.")
            return redirect('powerbi_report:missing_users', report_id=report_id)

        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            messages.success(request, f"Successfully added {len(new_users_added)} user(s) to report permissions.")
            log_history(request.user, f"Added users {', '.join(new_users_added)} to report (ID: {report_id})")
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Users {', '.join(new_users_added)} have been granted access to report {info['name']} (ID: {report_id}) from {info['path']} by {request.user.username}."
                )
            for username in new_users_added:
                user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
                if user_obj:
                    Notification.objects.create(
                        user=user_obj,
                        message=f"You have been granted access to the report {info['name']} (ID: {report_id}) from {info['path']} by {request.user.username}."
                    )
                else:
                    print(f"User with ad2000={username} not found for notification.")

            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            print(f"HTTP Error while adding permissions: {errh}")
            messages.error(request, f"Failed to add permissions due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.RequestException as err:
            print(f"Request Error while adding permissions: {err}")
            messages.error(request, f"Failed to add permissions due to request error: {str(err)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
    
    log_history(request.user, f"Invalid request method (not POST) for adding users to report ID: {report_id}")
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_users', report_id=report_id)
#################################################################################################################
#                    Adds all users to a Power BI report's permissions                                          #
#################################################################################################################

@login_required
def add_all_users_to_report(request, report_id):
   
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        
        all_reports = get_powerbi_reports(request)
        report = next((r for r in all_reports if r['Id'] == report_id), None)
        if not report:
            return HttpResponse("Report not found", status=404)
        
        policies_url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        try:
            get_response = requests.get(policies_url, auth=auth)
            get_response.raise_for_status()
            full_policies = get_response.json()
        except Exception as e:
            print("Error fetching full policies:", e)
            full_policies = {
                "Id": "00000000-0000-0000-0000-000000000000",
                "InheritParentPolicy": False,
                "Policies": []
            }
        
        current_policies = full_policies.get("Policies", [])
        if not isinstance(current_policies, list):
            current_policies = []
        
        local_users = CustomUser.objects.all()
        
        for user in local_users:
            user_identifier = user.ad2000 if user.ad2000 else user.username
            exists = any(
                ((policy.get("GroupUserName") or policy.get("UserName")).split("\\")[-1]).lower() == user_identifier.lower()
                for policy in current_policies if policy.get("GroupUserName") or policy.get("UserName")
            )
            if not exists:
                new_policy = {
                    "GroupUserName": user_identifier,
                    "Roles": [{"Name": "Explorateur"}]
                }
                current_policies.append(new_policy)
        
        payload = {
            "Id": full_policies.get("Id", "00000000-0000-0000-0000-000000000000"),
            "InheritParentPolicy": full_policies.get("InheritParentPolicy", False),
            "Policies": current_policies
        }
        headers = {"Content-Type": "application/json"}
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        print("PUT URL:", url)
        print("Payload:", payload)
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            print(f"HTTP Error while adding all permissions: {errh}")
            return HttpResponse("Failed to add all permissions to server", status=500)
        except requests.exceptions.RequestException as err:
            print(f"Request Error while adding all permissions: {err}")
            return HttpResponse("Failed to add all permissions to server", status=500)
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Removes a specific user from a Power BI report's permissions                               #
#################################################################################################################



@login_required
def remove_users_from_report(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)

        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = get_report_permissions(request, report_id)
        if not isinstance(current_policies, list):
            current_policies = []

        updated_policies = [
            policy for policy in current_policies
            if policy.get("GroupUserName", "").lower() != username.lower()
        ]

        payload = {
            "Id": report_id,
            "Policies": updated_policies
        }
        headers = {"Content-Type": "application/json"}
        info = get_powerbi_report_info(request, report_id)  # Pass request here

        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            messages.success(request, f"Successfully removed permissions for user {username} from report.")
            log_history(request.user, f"Removed user {username} from report:(ID: {report_id})")
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"User {username} has had their access revoked from report {info['name']} (ID: {report_id}) from {info['path']} by {request.user.username}."
                )
            user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
            if user_obj:
                Notification.objects.create(
                    user=user_obj,
                    message=f"Your access to the report {info['name']} (ID: {report_id}) from {info['path']} has been revoked by {request.user.username}."
                )
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to remove permission due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to remove permission due to request error: {str(err)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)

    messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
    return redirect('powerbi_report:report_permissions', report_id=report_id)

#################################################################################################################
#                    Removes multiple selected users from a Power BI report's permissions                       #
#################################################################################################################

@login_required
def remove_selected_users_from_report(request, report_id):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        selected_users = request.POST.getlist('selected_users')
        
        if not selected_users:
            messages.error(request, "No users selected for removal.")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        
        current_policies = get_report_permissions(request, report_id) or []
        
        if not isinstance(current_policies, list):
            current_policies = []
        users_removed = []
        updated_policies = []
        for policy in current_policies:
            group_user_name = policy.get("GroupUserName", "")
            if group_user_name.lower() in [user.lower() for user in selected_users]:
                users_removed.append(group_user_name)
            else:
                updated_policies.append(policy)
        
        payload = {"Id": report_id, "Policies": updated_policies}
        headers = {"Content-Type": "application/json"}
        
        try:
            url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            info = get_powerbi_report_info(request, report_id)  # Pass request here

            if not info:
                messages.error(request, "Failed to retrieve report information.")
                return redirect('powerbi_report:report_permissions', report_id=report_id)

            messages.success(request, f"Successfully removed {len(users_removed)} user(s) from report permissions.")
            log_history(request.user, f"Removed users {', '.join(users_removed)} from report {info['name']} (ID: {report_id})")

            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Users {', '.join(users_removed)} have had their access revoked from report {info['name']} (ID: {report_id}) from {info['path']} by {request.user.username}."
                )
            # Notify users who lost access
            for username in users_removed:
                user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
                if user_obj:
                    Notification.objects.create(
                        user=user_obj,
                        message=f"Your access to the report {info['name']} (ID: {report_id}) from {info['path']} has been revoked by {request.user.username}."
                    )
                else:
                    print(f"User with ad2000={username} not found for notification.")

            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            print(f"HTTP Error while removing permissions: {errh}")
            messages.error(request, f"Failed to remove permissions due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.RequestException as err:
            print(f"Request Error while removing permissions: {err}")
            messages.error(request, f"Failed to remove permissions due to request error: {str(err)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
    
    messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
    return redirect('powerbi_report:report_permissions', report_id=report_id)



#################################################################################################################
#                    Displays users who do not have access to a specific Power BI report                        #
#################################################################################################################

@login_required
def missing_users(request, report_id):
  
    policies = get_report_permissions(request, report_id)
    
    allowed_accounts = set()
    if policies:
        for policy in policies:
            account = policy.get("GroupUserName") or policy.get("UserName")
            if account:
                allowed_accounts.add(account.split("\\")[-1].lower())
    
    all_users = CustomUser.objects.all()
    missing_users = []
    for user in all_users:
        identifier = (user.ad2000 or user.username).lower()
        if identifier not in allowed_accounts:
            missing_users.append(user)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)
    log_history(request.user, f"Viewed no access permissions for report: (ID: {report_id})")
    context = {
        'report_id': report_id,
        'missing_users': missing_users,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,

    }
    return render(request, 'powerbi_report/missing_users.html', context)




#################################################################################################################
#                    Preloads cache with Power BI reports and their permissions                                 #
#################################################################################################################

def load_cache(request):
    cache_key = "powerbi_reports_cache_all_users"
    all_reports = get_powerbi_reports(request)
    cache.set(cache_key, all_reports, timeout=300)
    
    for report in all_reports:
        cache.get_or_set(
            f"report_permissions_{report['Id']}",
            lambda: get_report_permissions(request, report['Id']),
            timeout=86400
        )
        parent_folder_id = report.get("ParentFolderId")
        if parent_folder_id:
            cache.get_or_set(
                f"folder_permissions_{parent_folder_id}",
                lambda: get_folder_permissions(request, parent_folder_id),
                timeout=86400
            )
    
    return JsonResponse({
        "status": "Cache preloaded",
        "num_reports": len(all_reports)
    })

#################################################################################################################
#                    Displays Power BI reports a specific user has access to                                    #
#################################################################################################################


import logging
logger = logging.getLogger(__name__)

def user_permission(request, username):
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        selected_user = None
        full_name = username
    
    current_username = username.lower()
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache_key = "powerbi_reports_cache_all_users"  
    all_reports = cache.get(cache_key)
    
    if all_reports is None:
        all_reports = get_powerbi_reports(request)
        cache.set(cache_key, all_reports, timeout=300) 
    allowed_reports = []
    
    for report in all_reports:
        policies = cache.get_or_set(
            f"report_permissions_{report['Id']}", 
            lambda: get_report_permissions(request, report['Id']), 
            timeout=300
        )
        
        if not policies:
            parent_folder_id = report.get("ParentFolderId")
            if parent_folder_id:
                policies = cache.get_or_set(
                    f"folder_permissions_{parent_folder_id}",
                    lambda: get_folder_permissions(request, parent_folder_id),
                    timeout=300
                )
        
        is_allowed = any(
            (policy.get("GroupUserName") or policy.get("UserName", "")).split("\\")[-1].lower() == current_username
            for policy in policies
        )
        
        if is_allowed:
            allowed_reports.append(report)
    
    
    
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)
    current_username_1 = username

    return render(request, 'powerbi_report/user_permission.html', {
        'selected_user': current_username_1,
        'full_name': full_name,
        'reports': allowed_reports,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })


#################################################################################################################
#                    Displays row-level security policies for a specific user and report                        #
#################################################################################################################

def report_security_view(request, username, report_id):
    # Try to retrieve the user by username.
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        selected_user = None
        full_name = username

    # Attempt to get the report instance (assumes Report model has report_id field)
    try:
        report_instance = Report.objects.get(report_id=report_id)
    except Report.DoesNotExist:
        messages.error(request, "Report not found.")
        return redirect("dashboard")  # Adjust redirection as needed.

    # Retrieve the row-level security policies for the specified report.
    policies = cache.get_or_set(
        f"report_permissions_{report_id}",
        lambda: get_report_permissions(request, report_id),
        timeout=300
    )

    # Filter policies matching the given username (case insensitive).
    current_username = username.lower()
    user_policies = [
        policy for policy in policies
        if (policy.get("GroupUserName") or policy.get("UserName", "")).split("\\")[-1].lower() == current_username
    ]

    if not user_policies:
        messages.error(request, "User does not have permissions for this report's row-level security.")
        return redirect("dashboard")

    # Determine the row-level security roles (or level) for the user.
    # Here, we assume each policy contains a "RoleName" key indicating the RLS role.
    user_roles = [policy.get("RoleName") for policy in user_policies if policy.get("RoleName")]
    row_security_level = ", ".join(user_roles) if user_roles else "No roles assigned"

    # Retrieve additional context data.
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_security.html', {
        'selected_user': username,
        'full_name': full_name,
        'report': report_instance,
        'user_policies': user_policies,
        'row_security_level': row_security_level,  # New context variable for RLS levels.
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })



#################################################################################################################
#                    Displays reports a specific user does not have access to                                   #
#################################################################################################################

@login_required
def missing_permissions(request, username):
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        selected_user = None
        full_name = username  
    
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache_key = "powerbi_reports_cache_all_users"  
    all_reports = cache.get(cache_key)
    
    if all_reports is None:
        all_reports = get_powerbi_reports(request)
        cache.set(cache_key, all_reports, timeout=300)  
    
    missing_reports = []
    
    for report in all_reports:
        policies = cache.get_or_set(
            f"report_permissions_{report['Id']}",
            lambda: get_report_permissions(request, report['Id']),
            timeout=300
        )
        
        if not policies:
            parent_folder_id = report.get("ParentFolderId")
            if parent_folder_id:
                policies = cache.get_or_set(
                    f"folder_permissions_{parent_folder_id}",
                    lambda: get_folder_permissions(request, parent_folder_id),
                    timeout=300
                )
        
        user_has_permission = any(
            (policy.get("GroupUserName") or policy.get("UserName", "")).split("\\")[-1].lower() == username.lower()
            for policy in policies
        )
        
        if not user_has_permission:
            missing_reports.append(report)

    log_history(request.user, f"Viewed no access report permissions for user :{username})")
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)
    
    return render(request, 'powerbi_report/missing_permissions.html', {
        'selected_username': username,
        'full_name': full_name,
        'missing_reports': missing_reports,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })



#################################################################################################################
#                    Lists users with or without report permissions based on filter                             #
#################################################################################################################


@login_required
def users_no_reports_view(request):  
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()

    users = CustomUser.objects.exclude(last_login__isnull=True)

    filter_no_permission = request.GET.get('no_permission', 'false') == 'true'
    permissions = get_user_permissions(request.user)

    if filter_no_permission:
        users_with_permissions = set()
        all_reports = get_powerbi_reports(request)

        for report in all_reports:
            policies = get_report_permissions(request, report['Id']) or []
            parent_folder_id = report.get("ParentFolderId")
            if parent_folder_id and not policies:
                policies = get_folder_permissions(request, parent_folder_id) or []

            for policy in policies:
                allowed_user = policy.get("GroupUserName") or policy.get("UserName")
                if allowed_user:
                    extracted = allowed_user.split("\\")[-1].lower()
                    users_with_permissions.add(extracted)

        users = [user for user in users if user.ad2000.lower() not in users_with_permissions]

    return render(request, 'users/user_management.html', {
        'notifications': notifications,
        'unread': unread,
        'users': users,
        'permissions': permissions,


    })


#################################################################################################################
#                    Adds permissions for a specific user to a Power BI report                                  #
#################################################################################################################
@login_required
def add_permission_to_server(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        allowed_account = username  
        current_policies = cache.get_or_set(
            f"report_permissions_{report_id}",
            lambda: get_report_permissions(request, report_id),
            timeout=300
        )
        
        if not isinstance(current_policies, list):
            current_policies = []
        
        permission_exists = any(
            policy.get("GroupUserName", "").lower() == allowed_account.lower()
            for policy in current_policies
        )
        
        if not permission_exists:
            try:
                user = CustomUser.objects.get(ad2000=username)  
            except ObjectDoesNotExist:
                messages.error(request, f"User with username '{username}' does not exist.")
                return redirect('powerbi_report:missing_permissions', username=username)
            
            roles = [{"Name": "Explorateur"}]
            if user.role and user.role.name.lower() == "admin": 
                roles.extend([
                    {"Name": "Gestionnaire de contenu"},
                    {"Name": "Mes rapports"},
                    {"Name": "Report Builder"},
                    {"Name": "Serveur de publication"}
                ])
            
            new_policy = {
                "GroupUserName": allowed_account,
                "Roles": roles
            }
            current_policies.append(new_policy)
        
        payload = {
            "Id": report_id,
            "Policies": current_policies
        }
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            
            cache.set(f"report_permissions_{report_id}", current_policies, timeout=300)
            # Notify the affected user
            info = get_powerbi_report_info(request, report_id)  # Pass request here

            if not info:
                messages.error(request, "Failed to retrieve report information.")
                return redirect('powerbi_report:missing_permissions', username=username)

            Notification.objects.create(
                user=user,
                message=f"You have been granted access to the report {info['name']} (ID: {report_id}) in {info['path']} by {request.user.username}."
            )
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"User {username} has been granted access to report {info['name']} (ID: {report_id}) in {info['path']} by {request.user.username}."
                )
            messages.success(request, f"Successfully added permissions for user {username} to report.")
            log_history(request.user, f"Added permission for user {username} to report {info['name']} (ID: {report_id}) in {info['path']} with roles {', '.join(role['Name'] for role in roles)}")

            return redirect('powerbi_report:missing_permissions', username=username)
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to add permission due to request error: {str(err)}")
            return redirect('powerbi_report:missing_permissions', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_permissions', username=username)
#################################################################################################################
#                    Grants a user access to all Power BI reports                                               #
#################################################################################################################

@login_required
def add_all_permissions(request, username):
   
    if request.method == 'POST':
        try:
            user_obj = CustomUser.objects.get(ad2000__iexact=username)
        except CustomUser.DoesNotExist:
            return HttpResponse("User not found", status=404)

        all_reports = get_powerbi_reports(request)
        auth = get_current_user_auth(request)
        headers = {"Content-Type": "application/json"}
         
        granted_reports = []

        for report in all_reports:
            policies = get_report_permissions(request, report['Id'])
            if not policies:
                parent_folder_id = report.get("ParentFolderId")
                if parent_folder_id:
                    policies = get_folder_permissions(request, parent_folder_id)
            if not isinstance(policies, list):
                policies = []
            has_permission = False
            for policy in policies:
                allowed_user = policy.get("GroupUserName") or policy.get("UserName")
                if allowed_user:
                    extracted = allowed_user.split("\\")[-1]
                    if extracted.lower() == username.lower():
                        has_permission = True
                        break
            if not has_permission:
                new_policy = {
                    "GroupUserName": username,  
                    "Roles": [
                        {"Name": "Explorateur"}  
                    ]
                }
                policies.append(new_policy)
                payload = {
                    "Id": report['Id'],
                    "Policies": policies
                }
                url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report['Id']})/Policies"
                try:
                    response = requests.put(url, json=payload, auth=auth, headers=headers)
                    response.raise_for_status()
                    granted_reports.append(report['Id'])

                except Exception as e:
                    print(f"Error adding permission for report {report['Id']}: {e}")
        if granted_reports:
            user_obj=CustomUser.objects.get(ad2000__iexact=username)

            Notification.objects.create(
                user=user_obj,
                message=f"You have been granted access to {len(granted_reports)} reports."
            )
        return redirect('powerbi_report:missing_permissions', username=username)
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Grants a user access to selected Power BI reports                                          #
#################################################################################################################

@login_required
def add_selected_permissions(request, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        report_ids = request.POST.getlist('report_ids')
        granted_reports = []
        
        try:
            user_obj = CustomUser.objects.get(ad2000__iexact=username)
        except ObjectDoesNotExist:
            messages.error(request, f"User with username '{username}' does not exist.")
            return redirect('powerbi_report:missing_permissions', username=username)
        
        roles = [{"Name": "Explorateur"}]
        if user_obj.role and user_obj.role.name.lower() == "admin":
            roles.extend([
                {"Name": "Gestionnaire de contenu"},
                {"Name": "Mes rapports"},
                {"Name": "Report Builder"},
                {"Name": "Serveur de publication"}
            ])
        
        for report_id in report_ids:
            cache_key = f"report_permissions_{report_id}"
            policies = cache.get_or_set(
                cache_key,
                lambda: get_report_permissions(request, report_id),
                timeout=300
            )
            
            if not isinstance(policies, list):
                policies = []
            
            permission_exists = any(
                ((policy.get("GroupUserName") or policy.get("UserName")).split("\\")[-1]).lower() == username.lower()
                for policy in policies if (policy.get("GroupUserName") or policy.get("UserName"))
            )
            
            if not permission_exists:
                new_policy = {
                    "GroupUserName": username,  
                    "Roles": roles  
                }
                policies.append(new_policy)
                url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
                payload = {"Policies": policies}
                headers = {"Content-Type": "application/json"}
                
                try:
                    response = requests.put(url, json=payload, auth=auth, headers=headers)
                    response.raise_for_status()
                    info = get_powerbi_report_info(request, report_id)  # Pass request here
                    if info:
                        granted_reports.append((report_id, info['name'], info['path']))
                        cache.set(cache_key, policies, timeout=300)
                    else:
                        messages.error(request, f"Failed to retrieve report information for report ID '{report_id}'.")
                except requests.exceptions.RequestException as e:
                    messages.error(request, f"Failed to add permission for report ID '{report_id}': {str(e)}")
        
        if granted_reports:
            # Notify the affected user
            report_details = ", ".join([f"{name} (ID: {rid}) in {path}" for rid, name, path in granted_reports])
            Notification.objects.create(
                user=user_obj,
                message=f"You have been granted access to {len(granted_reports)} report(s): {report_details} by {request.user.username}."
            )
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"User {username} has been granted access to {len(granted_reports)} report(s): {report_details} by {request.user.username}."
                )
            messages.success(request, f"Successfully added permissions for user {username} to {len(granted_reports)} report(s).")
            log_history(request.user, f"Added permissions for user {username} to reports: {report_details} with roles {', '.join(role['Name'] for role in roles)}")
        
        if not granted_reports:
            messages.warning(request, "No new permissions were added as the user already has access to all selected reports or report info could not be retrieved.")
        
        return redirect('powerbi_report:missing_permissions', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_permissions', username=username)


#################################################################################################################
#                    Removes a specific user's permissions from a Power BI report                               #
#################################################################################################################

@login_required
def remove_permission_from_server(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = cache.get_or_set(
            f"report_permissions_{report_id}",
            lambda: get_report_permissions(request, report_id),
            timeout=300
        )
        if not isinstance(current_policies, list):
            current_policies = []
        
        updated_policies = [
            policy for policy in current_policies 
            if (policy.get("GroupUserName") or policy.get("UserName")) and policy.get("GroupUserName", "").split("\\")[-1].lower() != username.lower()
        ]
        
        try:
            response = requests.put(url, json={"Policies": updated_policies}, auth=auth, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = get_powerbi_report_info(request, report_id)  # Pass request here

            if not info:
                messages.error(request, "Failed to retrieve report information.")
                return redirect('powerbi_report:user_permission', username=username)

            messages.success(request, f"Successfully removed permissions for user {username} to report.")
            log_history(request.user, f"Removed permission for user {username} to report {info['name']} (ID: {report_id}) in {info['path']}")
            try:
                user_obj = CustomUser.objects.get(ad2000__iexact=username)
                Notification.objects.create(
                    user=user_obj,
                    message=f"Your access to report {info['name']} (ID: {report_id}) in {info['path']} has been removed by {request.user.username}."
                )
            except ObjectDoesNotExist:
                print(f"User with ad2000={username} not found for notification.")
            
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"User {username} has had access removed from report {info['name']} (ID: {report_id}) in {info['path']} by {request.user.username}."
                )
            return redirect('powerbi_report:user_permission', username=username)
        except requests.exceptions.RequestException as e:
            print(f"Failed to remove permission for report ID '{report_id}': {e}")
            messages.error(request, f"Failed to remove permission due to request error: {str(e)}")
            return redirect('powerbi_report:user_permission', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
    return redirect('powerbi_report:user_permission', username=username)

#################################################################################################################
#                    Removes a specific user's permissions from all Power BI reports                            #
#################################################################################################################


@login_required
def remove_all_permissions(request, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        all_reports = get_powerbi_reports(request)
        
        for report in all_reports:
            report_id = report['Id']
            cache_key = f"report_permissions_{report_id}"
            
            policies = cache.get_or_set(
                cache_key,
                lambda: get_report_permissions(request, report_id) or get_folder_permissions(request, report.get("ParentFolderId")),
                timeout=300
            )
            
            if not policies:
                continue  
            
            updated_policies = [
                policy for policy in policies
                if not (
                    (policy.get("GroupUserName") or policy.get("UserName")) and
                    (policy.get("GroupUserName") or policy.get("UserName")).split("\\")[-1].lower() == username.lower()
                )
            ]
            
            if len(updated_policies) == len(policies):
                continue
            
            url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
            payload = {"Policies": updated_policies}
            headers = {"Content-Type": "application/json"}
            
            try:
                response = requests.put(url, json=payload, auth=auth, headers=headers)
                response.raise_for_status()
                
                cache.set(cache_key, updated_policies, timeout=300)
                print(f"Permissions updated for report {report_id}")
            except requests.exceptions.HTTPError as errh:
                print(f"HTTP Error for report {report_id}: {errh}")
            except requests.exceptions.RequestException as err:
                print(f"Request Error for report {report_id}: {err}")
        
        user_obj = CustomUser.objects.get(ad2000__iexact=username)
        message = "Your access to all reports has been removed."
        Notification.objects.create(user=user_obj, message=message)
        
        return redirect('powerbi_report:user_permission', username=username )
    
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Removes a specific user's permissions from selected Power BI reports                       #
#################################################################################################################

@login_required
def remove_selected_permissions(request, username):
    if request.method != 'POST':
        messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
        return redirect('powerbi_report:user_permission', username=username)

    auth = get_current_user_auth(request)
    report_ids = request.POST.getlist('report_ids')
    error_messages = []
    removed_reports = []

    try:
        user_obj = get_object_or_404(CustomUser, ad2000__iexact=username)
    except Http404:
        messages.error(request, f"User with username '{username}' does not exist.")
        return redirect('powerbi_report:user_permission', username=username)

    for report_id in report_ids:
        policies = cache.get_or_set(
            f"report_permissions_{report_id}",
            lambda: get_report_permissions(request, report_id),
            timeout=300
        )
        if not isinstance(policies, list):
            policies = []

        updated_policies = [
            policy for policy in policies 
            if (policy.get("GroupUserName") or policy.get("UserName")) and 
               policy.get("GroupUserName", "").split("\\")[-1].lower() != username.lower()
        ]
        
        url = f"{REPORT_SERVER_URL}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        payload = {"Policies": updated_policies}
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = get_powerbi_report_info(request, report_id)  # Pass request here
            if info:
                removed_reports.append((report_id, info['name'], info['path']))
            else:
                print(f"Failed to retrieve report info for ID '{report_id}'")
                error_messages.append(f"Report {report_id}: Failed to retrieve report information")
        except requests.exceptions.RequestException as e:
            print(f"Failed to remove permission for report ID '{report_id}': {e}")
            error_messages.append(f"Report {report_id}: Failed to remove permission due to request error: {str(e)}")
    
    if removed_reports:
        # Notify the affected user
        report_details = ", ".join([f"{name} (ID: {rid}) in {path}" for rid, name, path in removed_reports])
        Notification.objects.create(
            user=user_obj,
            message=f"Your access to {len(removed_reports)} report(s) has been removed: {report_details} by {request.user.username}."
        )
        # Notify admin users
        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=f"User {username} has had access removed from {len(removed_reports)} report(s): {report_details} by {request.user.username}."
            )
        messages.success(request, f"Successfully removed permissions for user {username} from {len(removed_reports)} report(s).")
        log_history(request.user, f"Removed permissions for user {username} from reports: {report_details}")
    
    if error_messages:
        messages.error(request, "Some permissions could not be removed: " + "; ".join(error_messages))
    
    if not removed_reports and not error_messages:
        messages.warning(request, "No permissions were removed as the user does not have access to the selected reports.")
    
    return redirect('powerbi_report:user_permission', username=username) 

############################################################################
#                              Dashboard                                   #
############################################################################

@login_required
def dashboard(request):
    log_history(request.user, "Accessed the dashboard")

    user_id = request.user.id
    cache_key = f"dashboard_data_{user_id}"
    cached_data = cache.get(cache_key)

    refresh_cache_key = f"report_refresh_list_{user_id}"
    refresh_data = cache.get(refresh_cache_key)

    if cached_data:
        if refresh_data:
            cached_data.update({
                'report_refresh_list': refresh_data.get('Report_Refresh_List', []),
                'completed_refreshes': refresh_data.get('completed_refreshes', 0),
                'failed_refreshes': refresh_data.get('failed_refreshes', 0),
            })
        return render(request, 'home.html', cached_data)

    # Default dashboard data
    total_users = CustomUser.objects.count()
    active_users = CustomUser.objects.filter(last_login__isnull=False).count()

    now = timezone.now()
    tz = pytz.timezone("Africa/Algiers")
    current_time = now.astimezone(tz)
    current_year = current_time.year
    current_month = current_time.month
    current_day = current_time.day
    users_this_month = CustomUser.objects.filter(
        last_login__year=current_year,
        last_login__month=current_month
    ).count()

    powerbi_reports = get_powerbi_reports(request)
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    # Monthly login counts 
    monthly_logins = (
        LoginEvent.objects
        .filter(
            login_type=LoginEvent.LOGIN,
            datetime__year=current_year
        )
        .annotate(month=TruncMonth('datetime'))
        .values('month')
        .annotate(count=Count('id'))
        .order_by('month')
    )

    # Daily login counts for current month
    daily_logins = (
        LoginEvent.objects
        .filter(
            login_type=LoginEvent.LOGIN,
            datetime__year=current_year,
            datetime__month=current_month
        )
        .annotate(day=TruncDay('datetime'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('day')
    )

    # Weekly login counts for the last 7 days (Friday to today)
    end_date = current_time
    days_to_last_friday = (current_time.weekday() - 4) % 7  
    start_date = current_time - timedelta(days=days_to_last_friday)
    start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    
    weekly_logins = (
        LoginEvent.objects
        .filter(
            login_type=LoginEvent.LOGIN,  
            datetime__date__gte=start_date,
            datetime__date__lte=end_date
        )
        .annotate(day=TruncDate('datetime'))
        .values('day')
        .annotate(count=Count('id'))
        .order_by('day')
    )

    # Hourly login counts for today
    start_of_day = current_time.replace(hour=0, minute=0, second=0, microsecond=0)  
    end_of_day = start_of_day + timedelta(days=1)
    hourly_logins = (
        LoginEvent.objects
        .filter(
            login_type=LoginEvent.LOGIN,  
            datetime__gte=start_of_day,
            datetime__lt=end_of_day
        )
        .annotate(hours=TruncHour('datetime'))
        .values('hours')
        .annotate(count=Count('id'))
        .order_by('hours')
    )


    context = {
        'total_users': total_users,
        'active_users': active_users,
        'users_this_month': users_this_month,
        'total_reports': len(powerbi_reports),
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
        'monthly_logins': list(monthly_logins),
        'daily_logins': list(daily_logins),
        'weekly_logins': list(weekly_logins),
        'hourly_logins': list(hourly_logins),
    }

    if refresh_data:
        context.update({
            'report_refresh_list': refresh_data.get('Report_Refresh_List', []),
            'completed_refreshes': refresh_data.get('completed_refreshes', 0),
            'failed_refreshes': refresh_data.get('failed_refreshes', 0),
        })

    cache.set(cache_key, context, timeout=500)
    return render(request, 'home.html', context)

from django.http import JsonResponse

@login_required
def get_report_refresh_list(request):
    user_id = request.user.id
    cache_key = f"report_refresh_list_{user_id}"
    cached_data = cache.get(cache_key)

    if cached_data:
        return JsonResponse({
            'report_refresh_list': cached_data.get('Report_Refresh_List', []),
            'completed_refreshes': cached_data.get('completed_refreshes', 0),
            'failed_refreshes': cached_data.get('failed_refreshes', 0),
        })




    now = datetime.now(pytz.timezone("Africa/Algiers"))
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = now.replace(hour=23, minute=59, second=59)

    powerbi_reports = get_powerbi_reports(request)
    completed_refreshes = 0
    failed_refreshes = 0
    report_refresh_list = []

    auth = get_current_user_auth(request)

    for report in powerbi_reports:
        report_id = report.get("Id")
        report_name = report.get("Name", "Unknown Report")

        if not report_id:
            continue

        refresh_url = f"{settings.POWERBI_REPORT_SERVER_URL}/reports/api/v2.0/PowerBIReports({report_id})/CacheRefreshPlans"

        try:
            refresh_response = requests.get(refresh_url, auth=auth, timeout=10)
            refresh_response.raise_for_status()
            refresh_data = refresh_response.json().get("value", [])

            for refresh in refresh_data:
                last_status = refresh.get("LastStatus", "Unknown")
                last_run_time = refresh.get("LastRunTime")
                schedule_data = refresh.get("Schedule", {})
                schedule_definition = schedule_data.get("Definition")

                if last_run_time and isinstance(last_run_time, str) and len(last_run_time) >= 19:
                    try:
                        dt_object = datetime.strptime(last_run_time[:19], "%Y-%m-%dT%H:%M:%S")
                        dt_object = dt_object.replace(tzinfo=pytz.UTC).astimezone(pytz.timezone("Africa/Algiers"))

                        if start_of_day <= dt_object <= end_of_day:
                            if "completed" in last_status.lower():
                                completed_refreshes += 1
                            elif "failed" in last_status.lower():
                                failed_refreshes += 1

                            formatted_last_refresh = dt_object.strftime("%d/%m/%Y %H:%M")

                            report_refresh_list.append({
                                "report_id": report_id,
                                "report_name": report_name,
                                "status": last_status,
                                "last_refresh": formatted_last_refresh,
                                "next_refresh": "N/A" if schedule_definition is None else schedule_definition
                            })
                    except ValueError:
                        pass

        except requests.exceptions.RequestException as err:
            print(f"Error fetching refresh details for report {report_id}: {err}")

    context = {
        'completed_refreshes': completed_refreshes,
        'failed_refreshes': failed_refreshes,
        'Report_Refresh_List': report_refresh_list,
    }

    cache.set(cache_key, context, timeout=500)

    return JsonResponse({
        'report_refresh_list': report_refresh_list,
        'completed_refreshes': completed_refreshes,
        'failed_refreshes': failed_refreshes,
    })



