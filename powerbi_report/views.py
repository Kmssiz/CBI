"""
PowerBI Report Views.

This module handles all view logic for Power BI Report management,
including report listing, embedding, permissions, and folder management.
"""

import logging
import os
import json
import time
import urllib.parse
import re
from collections import Counter
from datetime import datetime, timedelta

import pytz
import requests
from requests_ntlm import HttpNtlmAuth
from concurrent.futures import ThreadPoolExecutor, as_completed

# Windows-only authentication module
import sys
if sys.platform == 'win32':
    from requests_negotiate_sspi import HttpNegotiateAuth
else:
    HttpNegotiateAuth = None  # Not available on Linux

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import models
from django.db.models import Count
from django.db.models.functions import TruncMonth, TruncDay, TruncDate, TruncHour, TruncQuarter, TruncYear
from django.http import Http404, HttpResponse, JsonResponse, HttpResponseRedirect
from django.core.exceptions import PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt

from easyaudit.models import LoginEvent, CRUDEvent, RequestEvent
from notifications.models import Notification
from users.models import CustomUser, UserHistory, Role
from users.utils import log_history, get_user_permissions, admin_required

from .models import ReportRef, CustomFolder, FolderReportItem, UserReportPermission, PermissionSyncLog, PBIRSServer, MetadataOption
from .services import (
    PBIRSClient,
    get_active_pbirs_server_urls,
    get_group_members,
    get_pbirs_server_name_map,
    get_primary_pbirs_server_url,
    sync_all_user_permissions,
    sync_report_permissions,
    sync_report_refs,
)
from .services.ldap_group_members import LDAP_API_TOKEN, LDAP_GROUP_MEMBERS_URL
from .views_modules.folder_api import get_folder_list_response, get_folders_response
from .views_modules.embed import embed_report_view
from .views_modules.pbirs_folders import (
    add_powerbi_folder_view,
    delete_powerbi_folder_view,
    report_folders_list_view,
)
from .views_modules.report_file_ops import (
    download_report_view,
    replace_powerbi_report_view,
)
from .views_modules.report_listing import report_list_flat_view, report_list_view
from .views_modules.report_metadata import (
    edit_powerbi_report_description_view,
    edit_powerbi_report_name_view,
    edit_powerbi_report_path_view,
    get_powerbi_report_info_data,
)
from .views_modules.refresh_api import (
    get_refresh_plan_history_response,
    get_refresh_plans_data,
    get_shared_schedules_data,
)

logger = logging.getLogger('powerbi_report')

#################################################################################################################
#                    Retrieves NTLM authentication credentials for the current user                             #
#################################################################################################################

def get_current_user_auth(request):
    current_username = request.user.username
    current_password = request.session.get('ldap_password', None)
    
    if not current_password:
        logger.warning(
            "LDAP password not found in session for user %s. PBIRS auth unavailable.",
            current_username,
        )
        return None  # Return None so caller can handle gracefully
    
    # NTLM requires DOMAIN\username format
    ntlm_username = f"{settings.LDAP_DOMAIN}\\{current_username}"
    logger.debug("Authenticating PBIRS as %s", ntlm_username)
    return HttpNtlmAuth(ntlm_username, current_password)


#################################################################################################################
#                    Fetches all Power BI reports from the report server using API                              #
################################################################################################################# 

def _normalize_pbirs_item_type(item: dict) -> str:
    """
    Normalize PBIRS item type values to canonical names.

    Handles values from either `Type` or `TypeName` fields, including
    string and numeric representations used by CatalogItems.
    """
    raw_type = item.get("TypeName", item.get("Type"))

    if isinstance(raw_type, str):
        normalized = raw_type.strip().lower()
        if normalized == "folder":
            return "Folder"
        if normalized in {"powerbireport", "report"}:
            return "PowerBIReport"
        if normalized.isdigit():
            raw_type = int(normalized)
        else:
            return raw_type

    if isinstance(raw_type, int):
        # PBIRS catalog item type IDs (commonly observed):
        # 1 = Folder, 13 = Power BI Report, 2 = Report
        if raw_type == 1:
            return "Folder"
        if raw_type in (2, 13):
            return "PowerBIReport"

    return ""


def _normalize_pbirs_path(path: str) -> str:
    """Normalize PBIRS paths for reliable folder/report comparisons."""
    if not path:
        return ""
    normalized = path.strip()
    if not normalized:
        return ""
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized.casefold()


def _get_pbirs_folder_paths(request) -> set[str]:
    """
    Fetch folder paths from all configured PBIRS servers for the current user.
    Used as source-of-truth to prevent folders being treated as reports.
    """
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache_key = f"pbirs_folder_paths_{user_id}"
    cached_paths = cache.get(cache_key)
    if cached_paths is not None:
        return set(cached_paths)

    server_urls = get_active_pbirs_server_urls()
    if not server_urls:
        return set()

    auth_candidates = []
    user_auth = get_current_user_auth(request)
    if user_auth:
        auth_candidates.append(user_auth)

    # Fallback to service account to keep classification stable even when
    # the current user session does not have ldap_password.
    service_username = getattr(settings, "LDAP_SERVICE_USERNAME", "")
    service_password = getattr(settings, "LDAP_SERVICE_PASSWORD", "")
    service_domain = getattr(settings, "LDAP_DOMAIN", "")
    if service_username and service_password and service_domain:
        auth_candidates.append(
            HttpNtlmAuth(f"{service_domain}\\{service_username}", service_password)
        )

    for auth in auth_candidates:
        all_paths = set()
        for server_url in server_urls:
            try:
                url = f"{server_url}/Reports/api/v2.0/Folders"
                response = requests.get(url, auth=auth, timeout=10)
                response.raise_for_status()
                items = response.json().get("value", [])
                paths = {
                    _normalize_pbirs_path(item.get("Path", ""))
                    for item in items if item.get("Path")
                }
                all_paths.update(paths)
            except requests.exceptions.RequestException as err:
                logger.warning(f"Failed to fetch PBIRS folder paths from {server_url}: {err}")
        if all_paths:
            cache.set(cache_key, list(all_paths), timeout=200)
            return all_paths

    return set()


def get_powerbi_reports(request, endpoint="PowerBIReports"):
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache_key = f"powerbi_reports_cache_{user_id}_{endpoint}"  
    cached_reports = cache.get(cache_key)

    if cached_reports is not None:
        logger.debug(
            "Returning cached Power BI reports for user %s (endpoint=%s).",
            user_id,
            endpoint,
        )
        return cached_reports

    server_urls = get_active_pbirs_server_urls()
    if not server_urls:
        logger.warning("No active PBIRS servers configured; returning empty report list.")
        return []
    auth = get_current_user_auth(request)
    
    # Handle missing auth (session expired)
    if not auth:
        logger.warning("No PBIRS auth available for user %s; returning empty report list.", user_id)
        return []
    
    session = requests.Session()  
    session.auth = auth  

    all_filtered_items = []
    for server_url in server_urls:
        try:
            url = f"{server_url}/Reports/api/v2.0/{endpoint}"
            response = session.get(url=url, auth=auth, timeout=10)  
            response.raise_for_status()

            items = response.json().get('value', []) if response.status_code == 200 else []
            
            if endpoint == "CatalogItems":
                for item in items:
                    item_type = _normalize_pbirs_item_type(item)
                    if item_type == "PowerBIReport":
                        item['_server_url'] = server_url
                        all_filtered_items.append({**item, "Type": "PowerBIReport"})
            else:
                for item in items:
                    item_path = item.get("Path", "")
                    if not item_path:
                        continue

                    item_type = _normalize_pbirs_item_type(item)
                    if item_type == "Folder":
                        continue
                    if item_type and item_type != "PowerBIReport":
                        continue

                    item['_server_url'] = server_url
                    all_filtered_items.append({**item, "Type": "PowerBIReport"})

        except requests.exceptions.Timeout:
            logger.warning("PBIRS request timed out for user %s (endpoint=%s) on server %s.", user_id, endpoint, server_url)
        except requests.exceptions.RequestException as err:
            logger.error("PBIRS request error for user %s (endpoint=%s) on server %s: %s", user_id, endpoint, server_url, err)

    cache.set(cache_key, all_filtered_items, timeout=200) 
    return all_filtered_items

#################################################################################################################
#                    Local DB report listing (uses UserReportPermission instead of PBIRS API)                   #
#################################################################################################################

def get_local_reports_for_user(user, request=None):
    """
    Get accessible reports from the local DB instead of calling PBIRS API.
    Uses UserReportPermission + ReportRef tables populated at login.
    Returns list of dicts matching the PBIRS API format for template compatibility.
    """
    from powerbi_report.models import ReportRef, UserReportPermission

    if user.is_superuser:
        report_refs = ReportRef.objects.all().prefetch_related('poles', 'directions', 'societes', 'modules')
    else:
        permitted_report_ids = UserReportPermission.objects.filter(
            user=user
        ).values_list('report_id', flat=True)
        report_refs = ReportRef.objects.filter(id__in=permitted_report_ids).prefetch_related('poles', 'directions', 'societes', 'modules')

    # Defensive fallback for historical cache pollution.
    report_refs = _filter_reportref_to_leaf_items(report_refs)

    reports = []
    for ref in report_refs:
        path = ref.path or ""
        clean_path = path.lstrip("/")
        encoded_path = urllib.parse.quote(clean_path, safe="/")
        server_url = ref.server_url or get_primary_pbirs_server_url()
        embed_url = ref.embed_url or f"{server_url}/Reports/powerbi/{encoded_path}?rs:embed=true"

        # Derive ParentFolderId-like info from path for hierarchy views
        path_parts = path.strip("/").split("/") if path else []
        parent_folder_path = "/" + "/".join(path_parts[:-1]) if len(path_parts) > 1 else "/"

        ref_poles = [p.name for p in ref.poles.all()]
        ref_dirs = [d.name for d in ref.directions.all()]
        ref_socs = [s.name for s in ref.societes.all()]
        ref_mods = [m.name for m in ref.modules.all()]

        reports.append({
            'Id': ref.pbirs_id,
            'Name': ref.name,
            'Path': path,
            'Description': getattr(ref, 'description', ""),
            'Type': 'PowerBIReport',
            'embed_url': embed_url,
            'server_url': server_url,
            'ParentFolderPath': parent_folder_path,
            'modified_at': ref.modified_at,
            'modified_by': ref.modified_by,
            'pole': ", ".join(ref_poles),
            'direction': ", ".join(ref_dirs),
            'societe': ", ".join(ref_socs),
            'poles': ref_poles,
            'directions': ref_dirs,
            'societes': ref_socs,
            'modules': ref_mods,
            'is_consolide': ref.is_consolide,
            'report_type': ref.report_type,
        })

    return reports


def _user_can_access_report(user, report_ref):
    """Check report access from local DB only."""
    if not user.is_authenticated or not report_ref:
        return False
    if user.is_superuser or user.is_admin:
        return True
    return UserReportPermission.objects.filter(user=user, report=report_ref).exists()


def _sync_local_permissions_for_report(report_id, user):
    """Refresh one report's local permissions after an admin PBIRS policy change."""
    try:
        synced_count = sync_report_permissions(report_id, triggered_by=user)
        logger.info(
            "Synced %s local permissions for report %s after admin change.",
            synced_count,
            report_id,
        )
    except Exception as exc:
        logger.error(
            "Failed to sync local permissions for report %s after admin change: %s",
            report_id,
            exc,
        )


def get_local_folders_from_reports(reports):
    """
    Derive folder structure from report paths.
    Returns list of folder dicts with Id, Name, Path, Type keys.
    """
    folder_paths = set()
    for report in reports:
        path = report.get('Path', '')
        parts = path.strip('/').split('/')
        # Build all parent folder paths
        for i in range(1, len(parts)):  # Skip the last part (report name)
            folder_path = '/' + '/'.join(parts[:i])
            folder_paths.add(folder_path)

    folders = []
    for fp in sorted(folder_paths):
        folder_name = fp.split('/')[-1]
        parent_path = '/' + '/'.join(fp.strip('/').split('/')[:-1]) if '/' in fp.strip('/') else '/'
        folders.append({
            'Id': f'folder_{fp}',  # Synthetic ID
            'Name': folder_name,
            'Path': fp,
            'Type': 'Folder',
            'ParentFolderPath': parent_path,
        })

    return folders


def _filter_reportref_to_leaf_items(report_refs):
    """
    Defensive filter for assignment lists.

    Keeps only leaf paths so folder-like cached rows do not appear as reports.
    """
    refs = list(report_refs)
    normalized_paths = {}
    for ref in refs:
        path = (ref.path or "").strip()
        normalized_paths[ref.id] = path.rstrip("/") if path != "/" else "/"

    leaf_refs = []
    for ref in refs:
        path = normalized_paths.get(ref.id, "")
        if not path or path == "/":
            continue

        path_prefix = f"{path}/"
        has_children = any(
            other_path.startswith(path_prefix)
            for other_id, other_path in normalized_paths.items()
            if other_id != ref.id and other_path
        )

        if not has_children:
            leaf_refs.append(ref)

    return leaf_refs


def _format_granted_reports_message(report_names: list[str]) -> str:
    """
    Build a concise user-facing granted-access message in French.
    Example: "Vous avez obtenu l'accÃ¨s Ã  1 rapport(s) : Sales Report"
    """
    cleaned_names = [
        (name or "").strip()
        for name in report_names
        if (name or "").strip()
    ]
    details = ", ".join(cleaned_names) if cleaned_names else "N/A"
    return f"Vous avez obtenu l'accÃ¨s Ã  {len(cleaned_names)} rapport(s) : {details}"


def _get_assignable_reports_from_pbirs(request):
    """
    Fetch assignable reports from PBIRS with strict type filtering.

    Returns local ReportRef rows linked to the live PBIRS report IDs only.
    """
    pbirs_reports = get_powerbi_reports(request, endpoint="CatalogItems")
    if not pbirs_reports:
        return []

    valid_pbirs_ids = []
    for report in pbirs_reports:
        pbirs_id = report.get('Id')
        if not pbirs_id:
            continue

        valid_pbirs_ids.append(pbirs_id)
        name = report.get('Name', 'Unnamed')
        path = report.get('Path', '')
        server_url = report.get('_server_url') or get_primary_pbirs_server_url()
        clean_path = path.strip('/')
        encoded_path = urllib.parse.quote(clean_path, safe='/')
        embed_url = f"{server_url}/Reports/powerbi/{encoded_path}?rs:embed=true"

        ReportRef.objects.update_or_create(
            pbirs_id=pbirs_id,
            defaults={
                'name': name,
                'path': path,
                'server_url': server_url,
                'embed_url': embed_url,
            }
        )

    return list(ReportRef.objects.filter(pbirs_id__in=valid_pbirs_ids).order_by('name'))




#################################################################################################################
#                    Retrieves permissions for a specific Power BI report                                       #
#################################################################################################################

@admin_required
def get_report_permissions(request, report_id, server_url=None, timeout=10):
    base_url = server_url or ReportRef.get_server_url(report_id)
    if not base_url:
        logger.warning("No PBIRS server configured while fetching permissions for report %s.", report_id)
        return []
    url = f"{base_url}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
    auth = get_current_user_auth(request)
    if not auth:
        return []
    try:
        response = requests.get(url, auth=auth, timeout=timeout)
        response.raise_for_status()
        if response.status_code == 200:
            return response.json().get('Policies', [])
        else:
            return []
    except requests.exceptions.HTTPError as errh:
        # 403 Forbidden is expected when user lacks admin rights to view policies - not an error
        if hasattr(errh, 'response') and errh.response is not None and errh.response.status_code == 403:
            return []
        logger.error(f"HTTP Error (Permissions) for report {report_id} on {base_url}: {errh}")
        return []
    except requests.exceptions.RequestException as err:
        logger.error(f"Request Error (Permissions) for report {report_id} on {base_url}: {err}")
        return []
#################################################################################################################
#                    Retrieves permissions for a specific folder on the report server                           #
#################################################################################################################

@admin_required
def get_folder_permissions(request, folder_id, server_url=None, timeout=10):
    base_url = server_url or get_primary_pbirs_server_url()
    if not base_url:
        logger.warning("No PBIRS server configured while fetching permissions for folder %s.", folder_id)
        return []
    url = f"{base_url}/Reports/api/v2.0/Folders({folder_id})/Policies"
    auth = get_current_user_auth(request)
    if not auth:
        return []
    try:
        response = requests.get(url, auth=auth, timeout=timeout)
        response.raise_for_status()
        if response.status_code == 200:
            return response.json().get('Policies', [])
        else:
            return []
    except requests.exceptions.HTTPError as errh:
        # 403 Forbidden is expected when user lacks admin rights to view policies - not an error
        if hasattr(errh, 'response') and errh.response is not None and errh.response.status_code == 403:
            return []
        logger.error(f"HTTP Error (Folder Permissions) for folder {folder_id} on {base_url}: {errh}")
        return []
    except requests.exceptions.RequestException as err:
        logger.error(f"Request Error (Folder Permissions) for folder {folder_id} on {base_url}: {err}")
        return []
        logger.error(f"Request Error (Folder Permissions): {err}")
        return []


#################################################################################################################
#                    Fetches folder structure from Power BI Report Server for jsTree                            #
#################################################################################################################
from django.views.decorators.http import require_GET

@admin_required
@require_GET
def get_folders(request):
    return get_folders_response(request, get_current_user_auth)

def _update_report_metadata(report_id, user, name=None, path=None, description=None):
    """Update modification metadata for a report in ReportRef."""
    try:
        from powerbi_report.models import ReportRef
        from django.utils import timezone
        
        defaults = {
            'modified_at': timezone.now(),
            'modified_by': user,
        }
        if name:
            defaults['name'] = name
        if path:
            defaults['path'] = path
        if description:
            defaults['description'] = description

        ReportRef.objects.update_or_create(
            pbirs_id=report_id,
            defaults=defaults
        )
    except Exception as e:
        logger.error(f"Failed to update report metadata for {report_id}: {e}")

@admin_required
def get_folder_list(request):
    return get_folder_list_response(request, get_current_user_auth)
#################################################################################################################
#                    Displays a list of Power BI reports for admin users                                        #
#################################################################################################################
@admin_required
def report_list(request):
    return report_list_view(
        request=request,
        local_reports_getter=get_local_reports_for_user,
        permissions_getter=get_user_permissions,
    )


#################################################################################################################
#                    Retrieves information for a specific Power BI report                                       #
#################################################################################################################

@login_required
def get_powerbi_report_info(request, report_id):
    return get_powerbi_report_info_data(request, report_id, get_current_user_auth)
#################################################################################################################
#                    Updates the name of a Power BI report and notifies admin users                             #
#################################################################################################################
@admin_required
def edit_powerbi_report_name(request, report_id):
    return edit_powerbi_report_name_view(
        request=request,
        report_id=report_id,
        auth_getter=get_current_user_auth,
        report_permissions_getter=get_report_permissions,
        metadata_updater=_update_report_metadata,
        history_logger=log_history,
    )
#################################################################################################################
#                    Updates the path of a Power BI report                                                      #
#################################################################################################################

@admin_required
def edit_powerbi_report_path(request, report_id):
    return edit_powerbi_report_path_view(
        request=request,
        report_id=report_id,
        auth_getter=get_current_user_auth,
        metadata_updater=_update_report_metadata,
    )
#################################################################################################################
#                    Replaces an existing Power BI report with a new PBIX file                                  #
#################################################################################################################

@admin_required
def replace_powerbi_report(request, report_id):
    return replace_powerbi_report_view(
        request=request,
        report_id=report_id,
        auth_getter=get_current_user_auth,
        report_permissions_getter=get_report_permissions,
        metadata_updater=_update_report_metadata,
        history_logger=log_history,
        permissions_syncer=sync_all_user_permissions,
    )
#################################################################################################################
#                    Updates the description of a Power BI report and notifies admin users                      #
#################################################################################################################

@admin_required
def edit_powerbi_report_description(request, report_id):
    return edit_powerbi_report_description_view(
        request=request,
        report_id=report_id,
        auth_getter=get_current_user_auth,
        report_info_getter=get_powerbi_report_info,
        metadata_updater=_update_report_metadata,
        history_logger=log_history,
    )
#################################################################################################################
#                    Embeds a Power BI report for viewing in the browser                                        #
#################################################################################################################

@login_required
def embed_report(request, report_path):
    return embed_report_view(
        request=request,
        report_path=report_path,
        auth_getter=get_current_user_auth,
        permissions_getter=get_user_permissions,
    )

#########################################################################################
#                                  download report                                      #
#########################################################################################

@admin_required
def download_report(request, report_id):
    return download_report_view(
        request=request,
        report_id=report_id,
        auth_getter=get_current_user_auth,
        report_info_getter=get_powerbi_report_info,
        report_permissions_getter=get_report_permissions,
        report_server_url=get_primary_pbirs_server_url(),
    )
#################################################################################################################
#                    Displays a flat list of Power BI reports for authenticated users                           #
#################################################################################################################

# Mapping of context values to their PBIRS root folder paths.
CONTEXT_ROOT_FOLDERS = {
    'direction': '/CBI',
    'pole': '/CBI',
    'biblio': '/BI',
    'anomalie': '/Anomalie',
    'consolide': '/CBI',
}

@login_required
def report_list_flat(request):
    return report_list_flat_view(
        request=request,
        report_server_url=get_primary_pbirs_server_url(),
        context_root_folders=CONTEXT_ROOT_FOLDERS,
        reports_getter=get_powerbi_reports,
        permissions_getter=get_user_permissions,
        local_reports_getter=get_local_reports_for_user,
    )


#################################################################################################################
#                    Displays a hierarchical list of Power BI reports for authenticated users                   #
#################################################################################################################

@admin_required
def report_list_hierarchy(request, folder_path=""):
    force_refresh = request.GET.get('force_refresh', 'false').lower() == 'true'

    # Use local DB instead of PBIRS API for listing
    items = get_local_reports_for_user(request.user, request=request)

    # Also include derived folders so the hierarchy builds correctly
    items += get_local_folders_from_reports(items)
    
    folder_dict = {}

    for item in items:
        path = item.get("Path", "").strip("/")
        if not path:
            continue
            
        parts = path.split("/")
        item_type = item.get("Type")
        item_name = parts[-1]
        
        # Traverse to the parent container
        current_level = folder_dict
        for part in parts[:-1]:
            # Ensure parent exists and is always a folder node with children.
            existing = current_level.get(part)
            if not isinstance(existing, dict) or existing.get('type') != 'Folder':
                current_level[part] = {'type': 'Folder', 'children': {}}
            elif 'children' not in existing or not isinstance(existing['children'], dict):
                existing['children'] = {}
            current_level = current_level[part]['children']

        if item_type == 'Folder':
            # Ensure the folder entry exists and has children.
            existing = current_level.get(item_name)
            if not isinstance(existing, dict) or existing.get('type') != 'Folder':
                current_level[item_name] = {'type': 'Folder', 'children': {}}
            elif 'children' not in existing or not isinstance(existing['children'], dict):
                existing['children'] = {}
            
        elif item_type == 'PowerBIReport':
            # For reports, assign the embed URL if a folder with the same name does not already exist.
            if (
                item_name in current_level
                and isinstance(current_level[item_name], dict)
                and current_level[item_name].get('type') == 'Folder'
            ):
                continue
            current_level[item_name] = {
                "type": "PowerBIReport",
                "url": item.get("embed_url"),
                "path": item.get("Path", "")
            }

    current_folder = folder_dict
    breadcrumbs = []
    if folder_path:
        parts = folder_path.strip("/").split("/")
        for idx, part in enumerate(parts):
            breadcrumbs.append({
                "name": part,
                "url": "/".join(parts[: idx + 1])
            })
            # Navigate into 'children' if it exists
            node = current_folder.get(part)
            if node and node.get('type') == 'Folder':
                current_folder = node.get('children', {})
            else:
                current_folder = {}
                break

    folder_path = folder_path.rstrip('/')
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
     # Log only when folder_path is root (empty or "/")
    # if not folder_path or folder_path == "/":
        # log_history(request.user, "Liste des rapports Power BI consultÃ©e (hiÃ©rarchie)")

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

@admin_required
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
        primary_server_url = get_primary_pbirs_server_url()
        if not primary_server_url:
            messages.error(request, "No active PBIRS server is configured.")
            return redirect('powerbi_report:report_list_hierarchy_folder', folder_path=folder_path)
        api_url = f"{primary_server_url}/Reports/api/v2.0/PowerBIReports(path='{encoded_path}')/Model.Upload"

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
            log_history(request.user, f"Rapport Power BI tÃ©lÃ©versÃ© : {report_path}")
            # Clear the cache for the current user
            user_id = request.user.id
            cache_key = f"powerbi_reports_cache_{user_id}"
            cache.delete(cache_key)
            logger.debug("Cleared cache for user %s after uploading report.", user_id)

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Un nouveau rapport '{report_name}' a Ã©tÃ© tÃ©lÃ©versÃ© dans {report_path} par {request.user.username}."
                )
            logger.info(
                "Sent upload notification for report '%s' to %s admins.",
                report_name,
                len(admin_users),
            )

        except requests.exceptions.HTTPError as errh:
            messages.error(request, f"Failed to upload report. HTTP Error: {errh}")
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to upload report. Request Error: {err}")

        return redirect('powerbi_report:report_list_hierarchy_folder', folder_path=folder_path)

    # Redirect GET requests to the report list
    return redirect('powerbi_report:report_list_hierarchy')


#################################################################################################################
#                   Adds a new report to PBIRS and stores local metadata in ReportRef                         #
#################################################################################################################

@admin_required
def add_report_local(request):
    """Upload a .pbix to PBIRS and save local metadata (pole, direction, type).
    Returns a JSON response so the frontend can display inline feedback.
    """
    from powerbi_report.models import ReportRef, REPORT_TYPE_CHOICES, MetadataOption
    from django.http import JsonResponse

    def _err(msg, code=400):
        return JsonResponse({'success': False, 'error': msg}, status=code)

    permissions = get_user_permissions(request.user)
    if not permissions.get('add_powerbireport'):
        return _err("Vous n'avez pas la permission d'ajouter des rapports.", 403)

    if request.method != 'POST':
        return _err("Méthode non autorisée.", 405)

    pbix_file = request.FILES.get('pbix_file')
    pbirs_path = request.POST.get('pbirs_path', '').strip()
    server_url = request.POST.get('server_url', '').strip()
    report_type = request.POST.get('report_type', '').strip()
    is_consolide = request.POST.get('is_consolide') == 'on'

    selected_directions = request.POST.getlist('directions')
    selected_societes = request.POST.getlist('societes')
    selected_modules = request.POST.getlist('modules')


    # ── Input validation ──────────────────────────────────────────────────────
    if not pbix_file:
        return _err("Aucun fichier sélectionné. Veuillez choisir un fichier .pbix.")
    if not pbix_file.name.lower().endswith('.pbix'):
        return _err("Le fichier doit être au format .pbix.")
    if not pbirs_path:
        return _err("Le chemin PBIRS est obligatoire.")
    if not server_url:
        return _err("Le serveur PBIRS est obligatoire.")

    valid_servers = get_active_pbirs_server_urls()
    if server_url not in valid_servers:
        return _err("Serveur PBIRS non valide ou non configuré.")

    # Normalise path
    if not pbirs_path.startswith('/'):
        pbirs_path = '/' + pbirs_path

    # ── Auth ──────────────────────────────────────────────────────────────────
    auth = get_current_user_auth(request)
    if not auth:
        return _err(
            "Authentification échouée. Votre session a peut-être expiré — "
            "veuillez vous déconnecter puis vous reconnecter.",
            401
        )

    # ── Call PBIRS Model.Upload ───────────────────────────────────────────────
    encoded_path = pbirs_path.replace("'", "''").replace(" ", "%20")
    api_url = (
        f"{server_url}/Reports/api/v2.0"
        f"/PowerBIReports(path='{encoded_path}')/Model.Upload"
    )

    session = requests.Session()
    session.auth = auth

    try:
        response = session.post(
            api_url,
            headers={'Accept': 'application/json'},
            files={'file': (pbix_file.name, pbix_file.read(), 'application/octet-stream')},
            timeout=120,  # allow large uploads
        )

        # ── HTTP-level errors with specific messages ───────────────────────
        if response.status_code == 400:
            detail = ""
            try:
                detail = response.json().get('Message') or response.json().get('error', {}).get('message', '')
            except Exception:
                pass
            return _err(
                f"Chemin PBIRS invalide ou paramètre incorrect. "
                f"Vérifiez que le chemin '{pbirs_path}' est bien formé."
                + (f" Détail serveur : {detail}" if detail else ""),
                400
            )

        if response.status_code == 401:
            return _err(
                "Vos identifiants PBIRS ont été refusés. "
                "Déconnectez-vous et reconnectez-vous puis réessayez.",
                401
            )

        if response.status_code == 403:
            return _err(
                "Vous n'avez pas les droits pour déposer un rapport dans ce dossier PBIRS sur ce serveur. "
                "Contactez l'administrateur PBIRS.",
                403
            )

        if response.status_code == 404:
            return _err(
                f"Le dossier parent du chemin '{pbirs_path}' n'existe pas sur le serveur PBIRS. "
                "Créez le dossier sur le serveur ou corrigez le chemin.",
                404
            )

        if response.status_code == 409:
            return _err(
                f"Un rapport existe déjà à l'emplacement '{pbirs_path}' sur ce serveur. "
                "Choisissez un chemin différent ou supprimez l'existant depuis PBIRS.",
                409
            )

        if response.status_code >= 500:
            return _err(
                f"Le serveur PBIRS a retourné une erreur interne ({response.status_code}). "
                "Vérifiez que le serveur est opérationnel et réessayez.",
                502
            )

        response.raise_for_status()  # catch any remaining 4xx/5xx
        data = response.json()

        pbirs_id = data.get('Id', '')
        report_name = data.get('Name', pbix_file.name.replace('.pbix', ''))

        # ── Persist local metadata ────────────────────────────────────────────
        report_ref, _ = ReportRef.objects.update_or_create(
            pbirs_id=pbirs_id,
            defaults={
                'name': report_name,
                'path': pbirs_path,
                'server_url': server_url,
                'is_consolide': is_consolide,
                'report_type': report_type or None,
                'modified_by': request.user,
            }
        )

        # Consolidated reports may still carry Société/Module metadata for
        # organizational navigation and filtering.
        report_ref.societes.set(selected_societes)
        parent_pole_ids = MetadataOption.objects.filter(
            id__in=selected_societes,
            option_type='societe',
            parent__isnull=False
        ).values_list('parent_id', flat=True).distinct()
        report_ref.poles.set(parent_pole_ids)
        report_ref.directions.set(selected_directions)
        report_ref.modules.set(selected_modules)

        # Sync legacy CharFields for backward compatibility
        report_ref.societe = ", ".join([s.name for s in report_ref.societes.all()]) or None
        report_ref.pole = ", ".join([p.name for p in report_ref.poles.all()]) or None
        report_ref.direction = ", ".join([d.name for d in report_ref.directions.all()]) or None
        report_ref.save()


        cache.delete(f"powerbi_reports_cache_{request.user.id}")
        log_history(request.user, f"Rapport ajouté : {pbirs_path}")
        logger.info(
            "Report '%s' uploaded by %s to PBIRS path '%s'.",
            report_name, request.user.username, pbirs_path
        )
        return JsonResponse({
            'success': True,
            'message': f"Rapport « {report_name} » publié avec succès sur PBIRS.",
        })

    except requests.exceptions.ConnectionError:
        logger.error("Cannot reach PBIRS server for upload.")
        return _err(
            "Impossible de joindre le serveur PBIRS. "
            "Vérifiez que le serveur est démarré et accessible depuis ce réseau."
        )
    except requests.exceptions.Timeout:
        logger.error("PBIRS upload timed out for path '%s'.", pbirs_path)
        return _err(
            "La requête a expiré (timeout). Le fichier est peut-être trop volumineux "
            "ou le serveur PBIRS est lent. Réessayez ou contactez l'administrateur."
        )
    except requests.exceptions.HTTPError as errh:
        logger.error("Upload HTTP error: %s", errh)
        return _err(f"Erreur HTTP inattendue : {errh}")
    except requests.exceptions.RequestException as err:
        logger.error("Upload request error: %s", err)
        return _err(f"Erreur réseau : {err}")
    except Exception as exc:
        logger.exception("Unexpected error during report upload: %s", exc)
        return _err("Une erreur inattendue s'est produite. Consultez les logs serveur.")


#################################################################################################################
#                   Updates local metadata of an existing ReportRef (no PBIRS API call)                       #
#################################################################################################################

@admin_required
def update_report_metadata_local(request, report_id: str):
    """Update pole, direction, report_type fields stored locally for a given report."""
    from powerbi_report.models import ReportRef, MetadataOption

    permissions = get_user_permissions(request.user)
    if not permissions.get('change_powerbireport'):
        messages.error(request, "Vous n'avez pas la permission de modifier les métadonnées.")
        return redirect('powerbi_report:report_detail', report_id=report_id)

    if request.method != 'POST':
        return redirect('powerbi_report:report_detail', report_id=report_id)

    report_ref = ReportRef.objects.filter(pbirs_id=report_id).first()
    if not report_ref:
        messages.error(request, "Rapport introuvable dans la base de données locale.")
        return redirect('powerbi_report:report_list')

    is_consolide = request.POST.get('is_consolide') == 'on'
    report_ref.is_consolide = is_consolide

    report_ref.report_type = request.POST.get('report_type', '').strip() or None
    report_ref.modified_by = request.user
    report_ref.save()

    # Consolidation changes navigation context, but must not destroy the
    # report's organizational metadata when the toggle is enabled.
    selected_societes = request.POST.getlist('societes')
    report_ref.societes.set(selected_societes)
    # Derive poles from Société parent relationships.
    parent_pole_ids = MetadataOption.objects.filter(
        id__in=selected_societes,
        option_type='societe',
        parent__isnull=False
    ).values_list('parent_id', flat=True).distinct()
    report_ref.poles.set(parent_pole_ids)

    selected_directions = request.POST.getlist('directions')
    report_ref.directions.set(selected_directions)

    selected_modules = request.POST.getlist('modules')
    report_ref.modules.set(selected_modules)

    # Sync legacy CharFields for backward compatibility
    report_ref.societe = ", ".join([s.name for s in report_ref.societes.all()]) or None
    report_ref.pole = ", ".join([p.name for p in report_ref.poles.all()]) or None
    report_ref.direction = ", ".join([d.name for d in report_ref.directions.all()]) or None
    report_ref.save()

    log_history(request.user, f"Métadonnées mises à jour pour le rapport : {report_ref.name}")
    messages.success(request, "Métadonnées mises à jour avec succès.")
    return redirect('powerbi_report:report_detail', report_id=report_id)


#################################################################################################################
#                    Lists folders and reports in a specified folder for authenticated users                    #
#################################################################################################################
@admin_required
def report_folders_list(request, folder_path=""):
    return report_folders_list_view(
        request=request,
        folder_path=folder_path,
        local_reports_getter=get_local_reports_for_user,
        local_folders_getter=get_local_folders_from_reports,
        permissions_getter=get_user_permissions,
    )


#################################################################################################################
#                    Creates a new folder in the Power BI Report Server and notifies admin users                 #
#################################################################################################################
@admin_required
def add_powerbi_folder(request):
    return add_powerbi_folder_view(
        request=request,
        auth_getter=get_current_user_auth,
        history_logger=log_history,
    )

#################################################################################################################
#                    Deletes a folder from the Power BI Report Server and notifies admin users                   #
#################################################################################################################
@admin_required
def delete_powerbi_folder(request, folder_id):
    return delete_powerbi_folder_view(
        request=request,
        folder_id=folder_id,
        auth_getter=get_current_user_auth,
        history_logger=log_history,
    )


#################################################################################################################
#                    Fetches cache refresh plans for a specific Power BI reporte                               #
#################################################################################################################

def get_refresh_plans(report_id, request):
    return get_refresh_plans_data(report_id, request, get_current_user_auth)

#################################################################################################################
#                    Fetches shared schedules from Power BI Report Server                                       #
#################################################################################################################

def get_shared_schedules(request):
    return get_shared_schedules_data(request, get_current_user_auth)

#################################################################################################################
#                    Displays detailed information about a specific Power BI report                             #
#################################################################################################################


@admin_required
def report_detail(request, report_id):
    # Use local DB instead of PBIRS API for lookup
    from powerbi_report.models import ReportRef
    report_ref = ReportRef.objects.filter(pbirs_id=report_id).first()
    
    if not report_ref:
        log_history(request.user, f"Tentative d'accÃ¨s Ã  un rapport inexistant ID : {report_id}")
        raise Http404("Report not found")

    if not _user_can_access_report(request.user, report_ref):
        log_history(request.user, f"Tentative d'accÃƒÂ¨s non autorisÃƒÂ©e au rapport ID : {report_id}")
        return HttpResponse("You do not have permission to view this report.", status=403)

    # Build report dict to match expected format
    report = {
        'Id': report_ref.pbirs_id,
        'Name': report_ref.name,
        'Path': report_ref.path,
        'Description': getattr(report_ref, 'description', ""),
    }
    # log_history(request.user, f"DÃ©tails du rapport Power BI consultÃ©s : {report.get('Name', 'Inconnu')} (ID : {report_id})")
    refresh_plans = get_refresh_plans(report_id, request)
    shared_schedules = get_shared_schedules(request)

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
            log_history(request.user, f"Ã‰chec d'authentification pour l'action {action} sur le plan d'actualisation ID : {refresh_plan_id} pour le rapport ID : {report_id}")
        else:
            if action == "refresh":
                refresh_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/CacheRefreshPlans({refresh_plan_id})/Model.Execute"
                try:
                    response = requests.post(refresh_url, auth=auth, headers={"Content-Type": "application/json"})
                    response.raise_for_status()
                    messages.success(request, "Refresh started successfully!")
                    log_history(request.user, f"Actualisation initiée pour le plan ID : {refresh_plan_id} sur le rapport '{report['Name']}'")
                    
                    # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Actualisation lancée pour le plan (ID : {refresh_plan_id}) sur le rapport '{report['Name']}' par {request.user.username}."
                        )
                    return redirect('powerbi_report:report_detail', report_id=report_id)

                except requests.exceptions.RequestException as err:
                    logger.error("Refresh execution failed for plan %s: %s", refresh_plan_id, err)
                    messages.error(request, "Failed to refresh report.")
                    log_history(request.user, f"Échec de l'actualisation du plan ID : {refresh_plan_id} pour le rapport '{report['Name']}'. Erreur : {str(err)}")
                    return redirect('powerbi_report:report_detail', report_id=report_id)

            elif action == "delete":
                delete_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/CacheRefreshPlans({refresh_plan_id})"
                try:
                    response = requests.delete(delete_url, auth=auth)
                    response.raise_for_status()
                    messages.success(request, "Refresh plan deleted successfully!")
                    log_history(request.user, f"Plan d'actualisation ID : {refresh_plan_id} supprimé pour le rapport '{report['Name']}'")
                     # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Plan d'actualisation (ID : {refresh_plan_id}) supprimé pour le rapport '{report['Name']}' par {request.user.username}."
                        )
                    return redirect('powerbi_report:report_detail', report_id=report_id)

                except requests.exceptions.RequestException as err:
                    logger.error("Refresh plan delete failed for plan %s: %s", refresh_plan_id, err)
                    messages.error(request, "Failed to delete refresh plan.")
                    log_history(request.user, f"Échec de la suppression du plan ID : {refresh_plan_id} pour le rapport '{report['Name']}'. Erreur : {str(err)}")
                    return redirect('powerbi_report:report_detail', report_id=report_id)

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)
    
    # Context data for schedule creation
    week_days = [
        ('Sun', 'Dimanche'), ('Mon', 'Lundi'), ('Tue', 'Mardi'), 
        ('Wed', 'Mercredi'), ('Thu', 'Jeudi'), ('Fri', 'Vendredi'), ('Sat', 'Samedi')
    ]
    months = [
        'January', 'February', 'March', 'April', 'May', 'June', 
        'July', 'August', 'September', 'October', 'November', 'December'
    ]

    from powerbi_report.models import REPORT_TYPE_CHOICES, MetadataOption
    import json as _json

    metadata_options = MetadataOption.objects.all().select_related('parent')
    all_metadata_poles = [opt for opt in metadata_options if opt.option_type == 'pole']
    all_metadata_directions = [opt for opt in metadata_options if opt.option_type == 'direction']
    all_metadata_societes = [opt for opt in metadata_options if opt.option_type == 'societe']
    all_metadata_modules = [opt for opt in metadata_options if opt.option_type == 'module']

    # Map Société ID → Pôle ID
    societe_pole_map = {}
    for opt in metadata_options:
        if opt.option_type == 'societe' and opt.parent_id:
            societe_pole_map[opt.id] = opt.parent_id

    # Current report metadata IDs
    report_pole_ids = list(report_ref.poles.values_list('id', flat=True))
    report_direction_ids = list(report_ref.directions.values_list('id', flat=True))
    report_societe_ids = list(report_ref.societes.values_list('id', flat=True))
    report_module_ids = list(report_ref.modules.values_list('id', flat=True))

    return render(request, 'powerbi_report/report_detail.html', {
        'notifications': notifications,
        'report': report,
        'report_ref': report_ref,
        'report_type_choices': REPORT_TYPE_CHOICES,
        'refresh_plans': refresh_plans,
        'shared_schedules': shared_schedules,
        'unread': unread,
        'permissions': permissions,
        'week_days': week_days,
        'months': months,
        'all_metadata_poles': all_metadata_poles,
        'all_metadata_directions': all_metadata_directions,
        'all_metadata_societes': all_metadata_societes,
        'all_metadata_modules': all_metadata_modules,
        'report_pole_ids': report_pole_ids,
        'report_direction_ids': report_direction_ids,
        'report_societe_ids': report_societe_ids,
        'report_module_ids': report_module_ids,
        'societe_pole_map_json': _json.dumps(societe_pole_map),
    })



#################################################################################################################
#                    Adds a new cache refresh plan for a Power BI report                                        #
#################################################################################################################


@admin_required
def add_refresh_plan(request, report_id):
    if request.method == 'POST':
        # Define server URL and credentials
        url = f"{ReportRef.get_server_url(report_id)}/reports/api/v2.0/CacheRefreshPlans"
        
        auth = get_current_user_auth(request)

        # Get form data
        description = request.POST.get('description', 'Default Refresh Plan')
        start_datetime = request.POST.get('start_datetime')
        catalog_item_path = request.POST.get('catalog_item_path', '')
        
        # New parameters for shared plan
        plan_type = request.POST.get('plan_type', 'specific')
        shared_schedule_id = request.POST.get('shared_schedule_id')
        
        # Recurrence parameters
        recurrence_type = request.POST.get('recurrence_type', 'Day')

        payload = {
            "Owner": None,
            "Description": description,
            "CatalogItemPath": catalog_item_path,
            "EventType": "DataModelRefresh",
            "ParameterValues": []
        }

        if plan_type == 'shared' and shared_schedule_id:
             payload["Schedule"] = {
                "ScheduleID": shared_schedule_id
            }
        else:
            # Existing specific plan logic
            # Convert start_datetime to Edm.DateTimeOffset format
            if not start_datetime:
                 start_datetime = "2025-04-27T02:00" # fallback if missing

            try:
                # Parse the input (e.g., '2025-04-27T02:00')
                dt = datetime.strptime(start_datetime, '%Y-%m-%dT%H:%M')
                # Format as ISO 8601 with seconds and timezone (e.g., '2025-04-27T02:00:00+01:00')
                start_datetime_formatted = dt.strftime('%Y-%m-%dT%H:%M:%S+01:00')
            except ValueError as e:
                messages.error(request, f"Invalid datetime format: {str(e)}")
                return HttpResponseRedirect(reverse('powerbi_report:report_detail', args=[report_id]))
            
            schedule_definition = {
                "StartDateTime": start_datetime_formatted,
                "EndDateSpecified": False,
                "EndDate": "1901-02-01T00:00:00+01:00",
            }

            if recurrence_type == 'Hour':
                minutes = request.POST.get('minutes_interval', 60)
                schedule_definition["Recurrence"] = {
                    "MinuteRecurrence": {
                         "@odata.type": "#Model.MinuteRecurrence",
                        "MinutesInterval": int(minutes)
                    }
                }
            elif recurrence_type == 'Day':
                days = request.POST.get('days_interval', 1)
                schedule_definition["Recurrence"] = {
                    "DailyRecurrence": {
                        "@odata.type": "#Model.DailyRecurrence",
                        "DaysInterval": int(days)
                    }
                }
            elif recurrence_type == 'Week':
                selected_days = request.POST.getlist('week_days')
                days_of_week = {
                    "Sunday": "Sun" in selected_days,
                    "Monday": "Mon" in selected_days,
                    "Tuesday": "Tue" in selected_days,
                    "Wednesday": "Wed" in selected_days,
                    "Thursday": "Thu" in selected_days,
                    "Friday": "Fri" in selected_days,
                    "Saturday": "Sat" in selected_days
                }
                
                # Check current PBIRS API expectation. Often it is "DaysOfWeek": { "Sunday": true, ... }
                # But sometimes it's a WeekDays string enum. The Model.WeeklyRecurrence normally takes DaysOfWeek selector.
                schedule_definition["Recurrence"] = {
                    "WeeklyRecurrence": {
                        "@odata.type": "#Model.WeeklyRecurrence",
                        "WeeksInterval": 1, 
                        "DaysOfWeek": days_of_week
                    }
                }
            elif recurrence_type == 'Month':
                raw_month_days = request.POST.get('month_days', '1')
                
                # Simply remove spaces to ensure "1, 15" becomes "1,15"
                # And "1-25" remains "1-25"
                month_days_str = str(raw_month_days).replace(' ', '')
                
                logger.debug(
                    "Monthly recurrence day input normalized from '%s' to '%s'.",
                    raw_month_days,
                    month_days_str,
                )

                selected_months = request.POST.getlist('months') # e.g. ['January', 'February']
                
                all_months = [
                    'January', 'February', 'March', 'April', 'May', 'June', 
                    'July', 'August', 'September', 'October', 'November', 'December'
                ]
                
                months_of_year = {}
                for m in all_months:
                    months_of_year[m] = m in selected_months

                schedule_definition["Recurrence"] = {
                    "MonthlyRecurrence": {
                        "@odata.type": "#Model.MonthlyRecurrence",
                        "Days": month_days_str, 
                        "MonthsOfYear": months_of_year
                    }
                }
            elif recurrence_type == 'Once':
                # No recurrence, just start date
                 schedule_definition["Recurrence"] = None 

            payload["Schedule"] = {
                "ScheduleID": None,
                "Definition": schedule_definition
            }
            if not description: # Ensure description is set if not provided
                 payload["Description"] = "Specific Refresh Plan" # Fallback or keep existing default

        try:
            # Send the POST request
            logger.debug("Adding refresh plan payload for report %s.", report_id)
            response = requests.post(
                url,
                auth=auth,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=30 
            )
            info = get_powerbi_report_info(request, report_id)

            if response.status_code == 201:
                messages.success(request, "Refresh plan added successfully.")
                report_name = (info or {}).get("name") or report_id
                log_history(request.user, f"Plan d'actualisation ajouté pour le rapport '{report_name}' avec la description '{description}'")
                admin_users = CustomUser.objects.filter(is_superuser=True)
                for admin in admin_users:
                    Notification.objects.create(
                        user=admin,
                        message=f"Un nouveau plan d'actualisation pour le rapport '{report_name}' (Plan: {description}) a été ajouté par {request.user.username}."
                    )
            else:
                messages.error(request, f"Failed to add refresh plan: {response.text}")
                logger.error("PBIRS rejected refresh plan for report %s: %s", report_id, response.text)
        except Exception as e:
            messages.error(request, f"Error adding refresh plan: {str(e)}")

        # Redirect back to the report details page
        return HttpResponseRedirect(reverse('powerbi_report:report_detail', args=[report_id]))

    return render(request, 'powerbi_report/report_details.html', {'report_id': report_id})   


#################################################################################################################
#                    Displays permissions for a specific Power BI report                                        #
################################################################************************************************#

@admin_required
def report_permissions(request, report_id):
    reports = get_powerbi_reports(request)
    lookup_id = str(report_id).casefold()
    report = next(
        (r for r in reports if str(r.get('Id', '')).casefold() == lookup_id),
        None
    )

    # Fallback to local cache source used by report_list to avoid false 404s
    # when PBIRS listing is temporarily unavailable or ID casing differs.
    if not report:
        report_ref = ReportRef.objects.filter(pbirs_id__iexact=report_id).first()
        if report_ref:
            report = {
                "Id": report_ref.pbirs_id,
                "Name": report_ref.name,
                "Path": report_ref.path,
                "Type": "PowerBIReport",
            }
        else:
            log_history(request.user, f"Tentative d'accÃ¨s aux permissions d'un rapport inexistant ID : {report_id}")
            raise Http404("Report not found")

    canonical_report_id = report.get("Id", report_id)
    report_name = report.get('Name', 'Unknown Report')
    # log_history(request.user, f"Permissions du rapport Power BI consultÃ©es : {report_name} (ID : {report_id})")

    policies = get_report_permissions(request, canonical_report_id)
    if not policies:
        parent_folder_id = report.get("ParentFolderId")
        if parent_folder_id:
            policies = get_folder_permissions(request, parent_folder_id)

    # Process policies to identify groups and individual users
    processed_policies = []
    for policy in policies:
        identifier = policy.get("GroupUserName", policy.get("UserName", ""))
        clean_identifier = identifier.replace("GROUPE-HASNAOUI\\", "").split("\\")[-1]
        
        # Check if the identifier is a group by attempting to fetch its members
        url = f"{LDAP_GROUP_MEMBERS_URL}/{clean_identifier}?token={LDAP_API_TOKEN}"
        try:
            response = requests.get(url)
            if response.status_code == 200 and "members" in response.json():
                # Identifier is a group; fetch its members for display
                members = get_group_members(clean_identifier)
                members_with_names = []
                for member in members:
                    user = CustomUser.objects.filter(ad2000=member).first()
                    members_with_names.append({
                        "UserName": member,
                        "FullName": user.get_full_name() if user else member
                    })
                processed_policies.append({
                    **policy,
                    "GroupUserName": clean_identifier,
                    "UserName": clean_identifier,  # For template compatibility
                    "Identifier": clean_identifier,
                    "DisplayName": clean_identifier,
                    "IsGroup": True,
                    "Members": members_with_names,
                    "MemberCount": len(members_with_names)
                })
            else:
                # Identifier is an individual user
                user = CustomUser.objects.filter(ad2000=clean_identifier).first()
                processed_policies.append({
                    **policy,
                    "UserName": clean_identifier,
                    "GroupUserName": clean_identifier,  # For template compatibility
                    "Identifier": clean_identifier,
                    "DisplayName": user.get_full_name() if user else clean_identifier,
                    "FullName": user.get_full_name() if user else clean_identifier,
                    "IsGroup": False,
                    "Members": [],
                    "MemberCount": 0
                })
        except requests.RequestException:
            # Assume identifier is an individual user
            user = CustomUser.objects.filter(ad2000=clean_identifier).first()
            processed_policies.append({
                **policy,
                "UserName": clean_identifier,
                "GroupUserName": clean_identifier,  # For template compatibility
                "Identifier": clean_identifier,
                "DisplayName": user.get_full_name() if user else clean_identifier,
                "FullName": user.get_full_name() if user else clean_identifier,
                "IsGroup": False,
                "Members": [],
                "MemberCount": 0
            })

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_permissions.html', {
        'report_id': canonical_report_id,
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

@admin_required
def add_users_to_report(request, report_id, username):
    
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        allowed_account = username  
        try:
            user = CustomUser.objects.get(ad2000=username)
        except ObjectDoesNotExist:
            messages.error(request, f"User with username '{username}' does not exist.")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        
        current_policies = get_report_permissions(request, report_id)
        if not isinstance(current_policies, list):
            current_policies = []
        
        permission_exists = any(
            policy.get("GroupUserName", "").lower() == allowed_account.lower()
            for policy in current_policies
        )

        roles = [{"Name": "Explorateur"}]
        if user.role and user.role.name.lower() == "admin":
            roles.extend([
                {"Name": "Gestionnaire de contenu"},
                {"Name": "Mes rapports"},
                {"Name": "Report Builder"},
                {"Name": "Serveur de publication"}
            ])
        
        if not permission_exists:
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
        
        logger.debug("Updating report policies via PUT %s", url)
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            messages.success(request, f"Successfully added permissions for user {username} to report.")
            report_name = (info or {}).get("name") or report_id
            log_history(request.user, f"Permission ajoutée pour l'utilisateur {username} au rapport '{report_name}' avec les rôles {', '.join(role['Name'] for role in roles)}")

            info = get_powerbi_report_info(request, report_id)
            report_name = (info or {}).get("name") or report_id

            # Notify the affected user
            Notification.objects.create(
                user=user,
                message=_format_granted_reports_message([report_name])
            )

            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                if admin.id == user.id:
                    continue
                Notification.objects.create(
                    user=admin,
                    message=f"L'utilisateur {username} a obtenu l'accès au rapport '{report_name}' par {request.user.username}."
                )
            
            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
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
@admin_required
def add_selected_users_to_report(request, report_id):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        selected_users = request.POST.getlist('selected_users')
        
        if not selected_users:
            messages.error(request, "No users selected for addition.")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
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
            log_history(request.user, f"Aucun nouvel utilisateur ajoutÃ© au rapport (ID : {report_id}) car tous les utilisateurs sÃ©lectionnÃ©s y ont dÃ©jÃ  accÃ¨s")
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
            log_history(request.user, f"Utilisateurs ajoutÃ©s : {', '.join(new_users_added)} au rapport (ID : {report_id})")

            selected_users_lower = {u.lower() for u in new_users_added}
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                admin_identifier = (admin.ad2000 or admin.username or "").lower()
                if admin_identifier in selected_users_lower:
                    continue
                Notification.objects.create(
                    user=admin,
                    message=f"Les utilisateurs {', '.join(new_users_added)} ont obtenu l'accÃ¨s au rapport {info['name']} (ID : {report_id}) dans {info['path']} par {request.user.username}."
                )
            for added_username in new_users_added:
                user_obj = CustomUser.objects.filter(ad2000__iexact=added_username).first()
                if user_obj:
                    Notification.objects.create(
                        user=user_obj,
                        message=_format_granted_reports_message([info['name']])
                    )
                else:
                    logger.warning("User with ad2000=%s not found for notification.", added_username)

            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            logger.error("HTTP Error while adding report permissions: %s", errh)
            messages.error(request, f"Failed to add permissions due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.RequestException as err:
            logger.error("Request Error while adding report permissions: %s", err)
            messages.error(request, f"Failed to add permissions due to request error: {str(err)}")
            return redirect('powerbi_report:missing_users', report_id=report_id)
    
    log_history(request.user, f"MÃ©thode de requÃªte invalide (non POST) pour l'ajout d'utilisateurs au rapport ID : {report_id}")
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_users', report_id=report_id)
#################################################################################################################
#                    Adds all users to a Power BI report's permissions                                          #
#################################################################################################################

@admin_required
def add_all_users_to_report(request, report_id):
   
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        
        all_reports = get_powerbi_reports(request)
        report = next((r for r in all_reports if r['Id'] == report_id), None)
        if not report:
            return HttpResponse("Report not found", status=404)
        
        policies_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        try:
            get_response = requests.get(policies_url, auth=auth)
            get_response.raise_for_status()
            full_policies = get_response.json()
        except Exception as e:
            logger.error("Error fetching full policies for report %s: %s", report_id, e)
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
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        logger.debug("Updating all-user report policies via PUT %s", url)
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            return redirect('powerbi_report:missing_users', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            logger.error("HTTP Error while adding all permissions: %s", errh)
            return HttpResponse("Failed to add all permissions to server", status=500)
        except requests.exceptions.RequestException as err:
            logger.error("Request Error while adding all permissions: %s", err)
            return HttpResponse("Failed to add all permissions to server", status=500)
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Removes a specific user from a Power BI report's permissions                               #
#################################################################################################################



@admin_required
def remove_users_from_report(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)

        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = get_report_permissions(request, report_id)
        if not isinstance(current_policies, list):
            current_policies = []

        updated_policies = [
            policy for policy in current_policies
            if (policy.get("GroupUserName") or policy.get("UserName", "")).split("\\")[-1].lower() != username.lower()
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
            log_history(request.user, f"Utilisateur {username} retiré du rapport '{info['name']}'")
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès de l'utilisateur {username} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                )
            user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
            if user_obj:
                Notification.objects.create(
                    user=user_obj,
                    message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                )
            
            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
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

@admin_required
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
        selected_users_lower = [user.lower() for user in selected_users]
        for policy in current_policies:
            group_user_name = policy.get("GroupUserName") or policy.get("UserName") or ""
            # Handle possible domain prefix
            clean_name = group_user_name.split("\\")[-1].lower()
            if clean_name in selected_users_lower:
                users_removed.append(group_user_name)
            else:
                updated_policies.append(policy)
        
        payload = {"Id": report_id, "Policies": updated_policies}
        headers = {"Content-Type": "application/json"}
        
        try:
            url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            info = get_powerbi_report_info(request, report_id)  # Pass request here

            if not info:
                messages.error(request, "Failed to retrieve report information.")
                return redirect('powerbi_report:report_permissions', report_id=report_id)

            messages.success(request, f"Successfully removed {len(users_removed)} user(s) from report permissions.")
            log_history(request.user, f"Utilisateurs {', '.join(users_removed)} retirés du rapport '{info['name']}'")

            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès des utilisateurs {', '.join(users_removed)} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                )
            # Notify users who lost access
            for username in users_removed:
                user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
                if user_obj:
                    Notification.objects.create(
                        user=user_obj,
                        message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                    )
                else:
                    logger.warning("User with ad2000=%s not found for notification.", username)

            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.HTTPError as errh:
            logger.error("HTTP Error while removing permissions: %s", errh)
            messages.error(request, f"Failed to remove permissions due to HTTP error: {str(errh)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
        except requests.exceptions.RequestException as err:
            logger.error("Request Error while removing permissions: %s", err)
            messages.error(request, f"Failed to remove permissions due to request error: {str(err)}")
            return redirect('powerbi_report:report_permissions', report_id=report_id)
    
    messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
    return redirect('powerbi_report:report_permissions', report_id=report_id)



#################################################################################################################
#                    Displays users who do not have access to a specific Power BI report                        #
#################################################################################################################

@admin_required
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
    log_history(request.user, f"Permissions manquantes consultÃ©es pour le rapport (ID : {report_id})")
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

@admin_required
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

@admin_required
def user_permission(request, username):
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        messages.error(request, f"Utilisateur '{username}' introuvable dans la base de donnÃ©es locale.")
        return redirect('users_view')
    
    # Check for force sync
    if request.GET.get('force_sync') == 'true':
        try:
            # Re-run the full sync logic (the code we previously had)
            # Fetch all reports
            all_reports = get_powerbi_reports(request)
            allowed_reports_data = []
            current_username = username.lower()
            
            # Calculate permissions based on API
            user_ad_groups = set(selected_user.ad_groups) if selected_user and selected_user.ad_groups else set()
            
            # Parallel fetching of policies for all reports
            report_policies = {}
            with ThreadPoolExecutor(max_workers=20) as executor:
                future_to_report = {
                    executor.submit(
                        get_report_permissions, 
                        request, 
                        report['Id'], 
                        server_url=report.get('_server_url')
                    ): report for report in all_reports
                }
                for future in as_completed(future_to_report):
                    report = future_to_report[future]
                    try:
                        policies = future.result()
                        report_policies[report['Id']] = policies
                    except Exception as e:
                        logger.error(f"Error fetching permissions for report {report['Id']}: {e}")
                        report_policies[report['Id']] = []

            # Handle fallback to folder permissions for reports with no policies
            reports_needing_folder_perms = []
            for report in all_reports:
                if not report_policies.get(report['Id']):
                    parent_id = report.get("ParentFolderId")
                    if parent_id:
                        reports_needing_folder_perms.append(report)

            if reports_needing_folder_perms:
                # Group by ParentFolderId to avoid redundant calls
                folder_ids = {r['ParentFolderId'] for r in reports_needing_folder_perms}
                folder_policies = {}
                
                with ThreadPoolExecutor(max_workers=10) as executor:
                    # Need to find at least one report per folder to get the server_url
                    folder_to_server = {}
                    for r in reports_needing_folder_perms:
                        folder_to_server[r['ParentFolderId']] = r.get('_server_url')

                    future_to_folder = {
                        executor.submit(
                            get_folder_permissions, 
                            request, 
                            fid, 
                            server_url=folder_to_server[fid]
                        ): fid for fid in folder_ids
                    }
                    for future in as_completed(future_to_folder):
                        fid = future_to_folder[future]
                        try:
                            folder_policies[fid] = future.result()
                        except Exception as e:
                            logger.error(f"Error fetching folder permissions for {fid}: {e}")
                            folder_policies[fid] = []
                
                # Apply folder policies to reports
                for report in reports_needing_folder_perms:
                    report_policies[report['Id']] = folder_policies.get(report['ParentFolderId'], [])

            # Now calculate which reports are allowed
            for report in all_reports:
                policies = report_policies.get(report['Id'], [])
                
                is_allowed = False
                for policy in policies:
                    # Check for direct user assignment
                    policy_username = (policy.get("UserName") or "").split("\\")[-1].lower()
                    if policy_username == current_username:
                        is_allowed = True
                        break
                    
                    # Check for group assignment
                    group_username = (policy.get("GroupUserName") or "").split("\\")[-1]
                    if group_username and group_username in user_ad_groups:
                        is_allowed = True
                        break
                
                if is_allowed:
                    # Determine if direct or group (prioritize direct if both exist)
                    is_direct = False
                    for policy in policies:
                         # Check for direct user assignment
                        policy_username = (policy.get("UserName") or "").split("\\")[-1].lower()
                        if policy_username == current_username:
                            is_direct = True
                            break
                    
                    allowed_reports_data.append({
                        'report': report,
                        'is_direct': is_direct
                    })
            
            # Sync to DB
            if allowed_reports_data:
                # Upsert ReportRefs
                # Upsert ReportRefs
                for item in allowed_reports_data:
                    report = item['report']
                    ReportRef.objects.update_or_create(
                        pbirs_id=report.get('Id'),
                        defaults={
                            'name': report.get('Name'),
                            'path': report.get('Path'),
                        }
                    )
                
                # Update Permissions
                pbirs_ids = [item['report'].get('Id') for item in allowed_reports_data]
                report_refs = ReportRef.objects.filter(pbirs_id__in=pbirs_ids)
                report_ref_map = {ref.pbirs_id: ref for ref in report_refs}

                UserReportPermission.objects.filter(user=selected_user).delete()
                new_permissions = []
                for item in allowed_reports_data:
                    ref = report_ref_map.get(item['report'].get('Id'))
                    if ref:
                        new_permissions.append(UserReportPermission(
                            user=selected_user, 
                            report=ref,
                            is_direct=item['is_direct']
                        ))
                
                UserReportPermission.objects.bulk_create(new_permissions)
                messages.success(request, f"Permissions synchronisÃ©es avec succÃ¨s : {len(new_permissions)} rapports trouvÃ©s.")
            else:
                # No permissions found via per-report ACL API (may be inherited from folder).
                # Do NOT delete existing local permissions to avoid false resets.
                messages.warning(request, "La vÃ©rification PBIRS n'a pas retournÃ© de rÃ©sultats directs. Les permissions locales restent inchangÃ©es (les accÃ¨s hÃ©ritÃ©s via dossier ne sont pas dÃ©tectÃ©s par cette mÃ©thode).")

        except Exception as e:
            logger.error(f"Error force syncing permissions: {e}")
            messages.error(request, f"Erreur lors de la synchronisation : {e}")
        
        return redirect(reverse('powerbi_report:user_permission', args=[username]))

    # Default: Read from DB
    user_permissions = UserReportPermission.objects.filter(user=selected_user).select_related('report')
    allowed_reports = []
    for perm in user_permissions:
        # Construct dict to match template expectations
        allowed_reports.append({
            'Id': perm.report.pbirs_id,
            'Name': perm.report.name,
            'Path': perm.report.path,
            'is_direct': perm.is_direct,
            'pole': perm.report.pole or '',
            'direction': perm.report.direction or '',
            'societe': perm.report.societe or '',
            'report_type': perm.report.report_type or '',
            'is_consolide': perm.report.is_consolide,
        })
    
    # Sort allowed reports by name
    allowed_reports.sort(key=lambda x: x.get('Name', '').lower())

    # Pagination
    paginator = Paginator(allowed_reports, 10) # Show 10 reports per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)
    metadata_options = MetadataOption.objects.all().select_related('parent')

    return render(request, 'powerbi_report/user_permission.html', {
        'selected_user': username,
        'full_name': full_name,
        'reports': page_obj, # Pass page object instead of full list
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
        'metadata_directions': [o for o in metadata_options if o.option_type == 'direction'],
        'metadata_societes': [o for o in metadata_options if o.option_type == 'societe'],
        'metadata_modules': [o for o in metadata_options if o.option_type == 'module'],
        'metadata_poles': [o for o in metadata_options if o.option_type == 'pole'],
        'metadata_groups': [
            ('Directions', 'directions', [o for o in metadata_options if o.option_type == 'direction']),
            ('Sociétés', 'societes', [o for o in metadata_options if o.option_type == 'societe']),
            ('Modules', 'modules', [o for o in metadata_options if o.option_type == 'module']),
            ('Pôles', 'poles', [o for o in metadata_options if o.option_type == 'pole']),
        ],
    })


#################################################################################################################
#                    Displays reports a specific user does not have access to                                   #
#################################################################################################################

@admin_required
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
    
    # Calculate permissions based on API
    user_ad_groups = set(selected_user.ad_groups) if selected_user and selected_user.ad_groups else set()
    # Normalize groups to ensure case-insensitive matching if needed, 
    # but strictly speaking AD groups are case-insensitive. 
    # Let's assume the stored groups are just the CN part (e.g. "Marketing").
    # The policy.GroupUserName usually comes as "DOMAIN\Marketing".
    
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
        
        is_allowed = False
        for policy in policies:
            # Check for direct user assignment
            policy_username = (policy.get("UserName") or "").split("\\")[-1].lower()
            if policy_username == username.lower():
                is_allowed = True
                break
            
            # Check for group assignment
            group_username = (policy.get("GroupUserName") or "").split("\\")[-1]
            if group_username and group_username in user_ad_groups:
                is_allowed = True
                break
        
    # Fetch local metadata for these reports to enable better filtering
    report_refs = {ref.pbirs_id: ref for ref in ReportRef.objects.filter(pbirs_id__in=[r['Id'] for r in missing_reports])}
    
    enriched_missing_reports = []
    for report in missing_reports:
        ref = report_refs.get(report['Id'])
        enriched_missing_reports.append({
            **report,
            'pole': ref.pole if ref else '',
            'direction': ref.direction if ref else '',
            'societe': ref.societe if ref else '',
            'report_type': ref.report_type if ref else '',
            'is_consolide': ref.is_consolide if ref else False,
        })
    missing_reports = enriched_missing_reports

    log_history(request.user, f"Permissions incomplètes consultées pour l'utilisateur : {username}")
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


@admin_required
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

        users = [user for user in users if user.ad2000 and user.ad2000.lower() not in users_with_permissions]

    # Pagination
    paginator = Paginator(users, 10)  # Show 10 users per page
    page_number = request.GET.get('page')
    users = paginator.get_page(page_number)
    
    roles = Role.objects.all()

    return render(request, 'users/user_management.html', {
        'notifications': notifications,
        'unread': unread,
        'users': users,
        'roles': roles,
        'permissions': permissions,
    })


#################################################################################################################
#                    Adds permissions for a specific user to a Power BI report                                  #
#################################################################################################################
@admin_required
def add_permission_to_server(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        allowed_account = username  
        try:
            user = CustomUser.objects.get(ad2000=username)
        except ObjectDoesNotExist:
            messages.error(request, f"User with username '{username}' does not exist.")
            return redirect('powerbi_report:missing_permissions', username=username)

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

        roles = [{"Name": "Explorateur"}]
        if user.role and user.role.name.lower() == "admin":
            roles.extend([
                {"Name": "Gestionnaire de contenu"},
                {"Name": "Mes rapports"},
                {"Name": "Report Builder"},
                {"Name": "Serveur de publication"}
            ])
        
        if not permission_exists:
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
                message=_format_granted_reports_message([info['name']])
            )
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                if admin.id == user.id:
                    continue
                Notification.objects.create(
                    user=admin,
                    message=f"L'utilisateur {username} a obtenu l'accès au rapport '{info['name']}' dans '{info['path']}' par {request.user.username}."
                )
            messages.success(request, f"Successfully added permissions for user {username} to report.")
            log_history(request.user, f"Added permission for user {username} to report '{info['name']}' in '{info['path']}' with roles {', '.join(role['Name'] for role in roles)}")

            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
            return redirect('powerbi_report:missing_permissions', username=username)
        except requests.exceptions.RequestException as err:
            messages.error(request, f"Failed to add permission due to request error: {str(err)}")
            return redirect('powerbi_report:missing_permissions', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_permissions', username=username)
#################################################################################################################
#                    Grants a user access to all Power BI reports                                               #
#################################################################################################################

@admin_required
def add_all_permissions(request, username):
   
    if request.method == 'POST':
        try:
            user_obj = CustomUser.objects.get(ad2000__iexact=username)
        except CustomUser.DoesNotExist:
            return HttpResponse("User not found", status=404)

        all_reports = get_powerbi_reports(request)
        auth = get_current_user_auth(request)
        headers = {"Content-Type": "application/json"}
         
        granted_report_names = []
        granted_report_ids = []

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
                url = f"{ReportRef.get_server_url(report['Id'])}/Reports/api/v2.0/PowerBIReports({report['Id']})/Policies"
                try:
                    response = requests.put(url, json=payload, auth=auth, headers=headers)
                    response.raise_for_status()
                    granted_report_names.append(report.get('Name') or report.get('Id', 'Unknown Report'))
                    granted_report_ids.append(report['Id'])

                except Exception as e:
                    logger.error("Error adding permission for report %s: %s", report['Id'], e)
        if granted_report_names:
            report_refs = ReportRef.objects.filter(pbirs_id__in=granted_report_ids)
            for report_ref in report_refs:
                permission, created = UserReportPermission.objects.get_or_create(
                    user=user_obj,
                    report=report_ref,
                    defaults={"is_direct": True},
                )
                if not created and not permission.is_direct:
                    permission.is_direct = True
                    permission.save(update_fields=["is_direct"])
            Notification.objects.create(
                user=user_obj,
                message=_format_granted_reports_message(granted_report_names)
            )
        return redirect('powerbi_report:missing_permissions', username=username)
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Grants a user access to selected Power BI reports                                          #
#################################################################################################################

@admin_required
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
                url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
                payload = {"Policies": policies}
                headers = {"Content-Type": "application/json"}
                
                try:
                    response = requests.put(url, json=payload, auth=auth, headers=headers)
                    response.raise_for_status()
                    info = get_powerbi_report_info(request, report_id)  # Pass request here
                    if info:
                        granted_reports.append((report_id, info['name'], info['path']))
                        cache.set(cache_key, policies, timeout=300)
                        _sync_local_permissions_for_report(report_id, request.user)
                    else:
                        messages.error(request, f"Failed to retrieve report information for report ID '{report_id}'.")
                except requests.exceptions.RequestException as e:
                    messages.error(request, f"Failed to add permission for report ID '{report_id}': {str(e)}")
        
        if granted_reports:
            # Notify the affected user
            report_names = [name for _, name, _ in granted_reports]
            report_details = ", ".join([f"'{name}' in '{path}'" for rid, name, path in granted_reports])
            Notification.objects.create(
                user=user_obj,
                message=_format_granted_reports_message(report_names)
            )
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                if admin.id == user_obj.id:
                    continue
                Notification.objects.create(
                    user=admin,
                    message=f"L'utilisateur {username} a obtenu l'accès à {len(granted_reports)} rapport(s) : {report_details} par {request.user.username}."
                )
            messages.success(request, f"Successfully added permissions for user {username} to {len(granted_reports)} report(s).")
            log_history(request.user, f"Added permissions for user {username} to reports: {report_details} with roles {', '.join(role['Name'] for role in roles)}")
        
        if not granted_reports:
            messages.warning(request, "No new permissions were added as the user already has access to all selected reports or report info could not be retrieved.")
        
        return redirect('powerbi_report:missing_permissions', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to add user permissions.")
    return redirect('powerbi_report:missing_permissions', username=username)


@admin_required
def add_metadata_permissions(request, username):
    """Grant direct access to every local report matching selected metadata."""
    if request.method != 'POST':
        return redirect('powerbi_report:user_permission', username=username)

    from django.db.models import Q
    directions = request.POST.getlist('directions')
    societes = request.POST.getlist('societes')
    modules = request.POST.getlist('modules')
    poles = request.POST.getlist('poles')
    if not any((directions, societes, modules, poles)):
        messages.error(request, "Sélectionnez au moins une métadonnée.")
        return redirect('powerbi_report:user_permission', username=username)

    report_ids = list(ReportRef.objects.filter(
        Q(directions__id__in=directions)
        | Q(societes__id__in=societes)
        | Q(modules__id__in=modules)
        | Q(poles__id__in=poles)
    ).distinct().values_list('pbirs_id', flat=True))
    if not report_ids:
        messages.warning(request, "Aucun rapport ne correspond aux métadonnées sélectionnées.")
        return redirect('powerbi_report:user_permission', username=username)

    post_data = request.POST.copy()
    post_data.setlist('report_ids', report_ids)
    request.POST = post_data
    return add_selected_permissions(request, username)


#################################################################################################################
#                    Removes a specific user's permissions from a Power BI report                               #
#################################################################################################################

@admin_required
def remove_permission_from_server(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
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
            payload = {
                "Id": report_id,
                "Policies": updated_policies
            }
            response = requests.put(url, json=payload, auth=auth, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = get_powerbi_report_info(request, report_id)  # Pass request here

            if not info:
                messages.error(request, "Failed to retrieve report information.")
                return redirect('powerbi_report:user_permission', username=username)

            messages.success(request, f"Successfully removed permissions for user {username} to report.")
            log_history(request.user, f"Removed permission for user {username} to report '{info['name']}' in '{info['path']}'")
            try:
                user_obj = CustomUser.objects.get(ad2000__iexact=username)
                Notification.objects.create(
                    user=user_obj,
                    message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                )
            except ObjectDoesNotExist:
                logger.warning("User with ad2000=%s not found for notification.", username)
            
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès de l'utilisateur {username} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {request.user.username}."
                )
            _sync_local_permissions_for_report(report_id, request.user)
            return redirect('powerbi_report:user_permission', username=username)
        except requests.exceptions.RequestException as e:
            logger.error("Failed to remove permission for report ID '%s': %s", report_id, e)
            messages.error(request, f"Failed to remove permission due to request error: {str(e)}")
            return redirect('powerbi_report:user_permission', username=username)
    
    messages.error(request, "Invalid request method. Please use POST to remove user permissions.")
    return redirect('powerbi_report:user_permission', username=username)

#################################################################################################################
#                    Removes a specific user's permissions from all Power BI reports                            #
#################################################################################################################


@admin_required
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
            
            url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
            payload = {
                "Id": report_id,
                "Policies": updated_policies
            }
            headers = {"Content-Type": "application/json"}
            
            try:
                response = requests.put(url, json=payload, auth=auth, headers=headers)
                response.raise_for_status()
                
                cache.set(cache_key, updated_policies, timeout=300)
                logger.info("Permissions updated for report %s.", report_id)
                 
                _update_report_metadata(report_id, request.user)
                _sync_local_permissions_for_report(report_id, request.user)
            except requests.exceptions.HTTPError as errh:
                logger.error("HTTP Error updating report %s permissions: %s", report_id, errh)
            except requests.exceptions.RequestException as err:
                logger.error("Request Error updating report %s permissions: %s", report_id, err)
        
        user_obj = CustomUser.objects.get(ad2000__iexact=username)
        message = "Votre accÃ¨s Ã  tous les rapports a Ã©tÃ© retirÃ©."
        Notification.objects.create(user=user_obj, message=message)
        
        return redirect('powerbi_report:user_permission', username=username )
    
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Removes a specific user's permissions from selected Power BI reports                       #
#################################################################################################################

@admin_required
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
        
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        payload = {"Policies": updated_policies}
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers)
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = get_powerbi_report_info(request, report_id)  # Pass request here
            if info:
                _update_report_metadata(report_id, request.user)
                _sync_local_permissions_for_report(report_id, request.user)
                removed_reports.append((report_id, info['name'], info['path']))
            else:
                logger.error("Failed to retrieve report info for ID '%s'", report_id)
                error_messages.append(f"Report {report_id}: Failed to retrieve report information")
        except requests.exceptions.RequestException as e:
            logger.error("Failed to remove permission for report ID '%s': %s", report_id, e)
            error_messages.append(f"Report {report_id}: Failed to remove permission due to request error: {str(e)}")
    
    if removed_reports:
        # Notify the affected user
        report_details = ", ".join([f"'{name}' in '{path}'" for rid, name, path in removed_reports])
        Notification.objects.create(
            user=user_obj,
            message=f"Votre accès à {len(removed_reports)} rapport(s) a été retiré : {report_details} par {request.user.username}."
        )
        # Notify admin users
        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=f"L'accès de l'utilisateur {username} a été retiré de {len(removed_reports)} rapport(s) : {report_details} par {request.user.username}."
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

@admin_required
def dashboard(request):
    # log_history(request.user, "Accessed the dashboard")

    user_id = request.user.id
    cache_key = f"dashboard_data_{user_id}"
    cached_data = cache.get(cache_key)


    if cached_data:
        return render(request, 'home.html', cached_data)

    # Default dashboard data
    total_users = CustomUser.objects.filter(last_login__isnull=False).count()
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

    # Use local DB count instead of PBIRS API call
    from powerbi_report.models import ReportRef
    total_reports_count = ReportRef.objects.count()
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

    quarterly_logins = (
        LoginEvent.objects
        .filter(
            login_type=LoginEvent.LOGIN,
            datetime__year=current_year
        )
        .annotate(quarter=TruncQuarter('datetime'))
        .values('quarter')
        .annotate(count=Count('id'))
        .order_by('quarter')
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

    # ============ Dashboard KPIs ============
    
    from .models import CustomFolder, ReportRef, FolderReportItem
    from users.models import UserHistory
    
    # --- KPI: Counts per category ---
    # Dashboards = direction + pole view types
    dashboard_report_count = FolderReportItem.objects.filter(
        folder__view_type__in=['direction', 'pole']
    ).values('report').distinct().count()
    
    # Extraction reports = biblio view type
    extraction_report_count = FolderReportItem.objects.filter(
        folder__view_type__in=['biblio', 'bibliotheque']
    ).values('report').distinct().count()
    
    # Anomaly reports = anomalie view type
    anomaly_report_count = FolderReportItem.objects.filter(
        folder__view_type='anomalie'
    ).values('report').distinct().count()
    
    # Total synced reports
    total_synced_reports = ReportRef.objects.count()
    
    # --- Most requested dashboards and extraction reports (from UserHistory) ---
    from django.db.models import Q

    raw_most_requested = list(
        UserHistory.objects
        .filter(
           Q(action__icontains='Rapport consulté') |
           Q(action__icontains='Rapport accédé') |
           Q(action__icontains='Rapport ouvert') |
           Q(action__icontains='Direction consultée') |
           Q(action__icontains='Pôle consulté')
        )
        .values('action')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    most_requested_counter = Counter()
    for item in raw_most_requested:
        action = item.get('action', '')
        count = item.get('count', 0)
        match = re.search(r"'([^']+)'", action)
        if match:
            report_name = match.group(1).strip()
        else:
            cleaned_action = re.sub(
                r"^(Rapport consult[ée]|Rapport acc[ée]d[ée]|Rapport ouvert|Direction consult[ée]|P[ôo]le consult[ée])\s*",
                "",
                action,
                flags=re.IGNORECASE,
            ).strip(" :-")
            report_name = cleaned_action or action
        if report_name:
            most_requested_counter[report_name] += count

    most_requested = [
        {'name': name, 'count': count}
        for name, count in most_requested_counter.most_common(10)
    ]

    # Users by company - only active users (logged in at least once)
    users_by_societe = list(
        CustomUser.objects
        .filter(last_login__isnull=False)
        .exclude(societe__isnull=True)
        .exclude(societe='')
        .values('societe')
        .annotate(count=Count('id'))
        .order_by('-count')[:6]
    )

    # --- New reports per folder (anomalie + direction) ---
    from .models import FolderReportItem as FRI2
    
    # Monthly breakdown for the current year
    reports_per_folder_monthly = list(
        FRI2.objects
        .filter(
            folder__view_type__in=['direction', 'pole', 'consolide', 'biblio', 'anomalie'],
            report__last_synced__year=current_year
        )
        .annotate(month=TruncMonth('report__last_synced'))
        .values('month', 'folder__view_type')
        .annotate(count=Count('report', distinct=True))
        .order_by('month')
    )
    
    # Quarterly breakdown for the current year
    reports_per_folder_quarterly = list(
        FRI2.objects
        .filter(
            folder__view_type__in=['direction', 'pole', 'consolide', 'biblio', 'anomalie'],
            report__last_synced__year=current_year
        )
        .annotate(quarter=TruncQuarter('report__last_synced'))
        .values('quarter', 'folder__view_type')
        .annotate(count=Count('report', distinct=True))
        .order_by('quarter')
    )
    
    # Yearly breakdown
    reports_per_folder_yearly = list(
        FRI2.objects
        .filter(folder__view_type__in=['direction', 'pole', 'anomalie', 'biblio', 'consolide'])
        .annotate(year=TruncYear('report__last_synced'))
        .values('year', 'folder__view_type')
        .annotate(count=Count('report', distinct=True))
        .order_by('year')
    )

    context = {
        'total_users': total_users,
        'active_users': active_users,
        'users_this_month': users_this_month,
        'total_reports': total_reports_count,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
        'monthly_logins': list(monthly_logins),
        'daily_logins': list(daily_logins),
        'quarterly_logins': list(quarterly_logins),
        'weekly_logins': list(weekly_logins),
        'hourly_logins': list(hourly_logins),
        # New KPIs
        'dashboard_report_count': dashboard_report_count,
        'extraction_report_count': extraction_report_count,
        'anomaly_report_count': anomaly_report_count,
        'total_synced_reports': total_synced_reports,
        'most_requested': most_requested,
        'users_by_societe': users_by_societe,
        'reports_per_folder_monthly': reports_per_folder_monthly,
        'reports_per_folder_quarterly': reports_per_folder_quarterly,
        'reports_per_folder_yearly': reports_per_folder_yearly,
    }



    cache.set(cache_key, context, timeout=500)
    return render(request, 'home.html', context)




#################################################################################################################
#                    CUSTOM VIRTUAL FOLDERS - PBIRS Permission-Based                                            #
#################################################################################################################

def _get_allowed_report_types_for_view(view_type):
    """
    Mapping between view types and allowed ReportRef.report_type metadata.
    Also returns specific field requirements.
    Returns: (allowed_types_list, required_field_name, field_value)
    """
    if view_type == 'pole':
        return (['dashboard'], 'poles', True) # Required M2M field 'poles' (non-empty)
    elif view_type == 'direction':
        return (['dashboard'], 'directions', True) # Required M2M field 'directions' (non-empty)
    elif view_type == 'consolide':
        return (['dashboard'], 'is_consolide', True) # Required boolean 'is_consolide'
    elif view_type == 'biblio':
        return (['bibliotheque'], None, None)
    elif view_type == 'anomalie':
        return (['anomalie'], None, None)
    elif view_type == 'module':
        return ([], 'modules', True) # Required M2M field 'modules' (non-empty)
    return ([], None, None)


def get_visible_report_ids(request, view_type=None):
    """
    Get PBIRS report IDs the current user has access to.
    Reads from local UserReportPermission table (synced at login).
    If view_type is provided, also filters by report_type and specific fields.
    """
    user = request.user
    
    # Filter by metadata if view_type is specified
    allowed_types, req_field, _ = _get_allowed_report_types_for_view(view_type) if view_type else ([], None, None)

    if user.is_superuser or user.is_admin:
        qs = ReportRef.objects.all()
        if allowed_types:
            qs = qs.filter(report_type__in=allowed_types)
        if req_field:
            if req_field == 'is_consolide':
                qs = qs.filter(is_consolide=True)
            elif req_field in ['poles', 'directions', 'modules', 'societes']:
                qs = qs.filter(**{f"{req_field}__isnull": False}).distinct()
            else:
                qs = qs.exclude(**{f"{req_field}__isnull": True}).exclude(**{req_field: ""})
        if view_type and view_type not in {'consolide', 'direction', 'pole', 'module'}:
            qs = qs.exclude(is_consolide=True)
        return set(qs.values_list('pbirs_id', flat=True))

    # Get from local database only. Normal navigation must not call PBIRS.
    local_permissions = UserReportPermission.objects.filter(user=user).select_related('report').prefetch_related('report__poles', 'report__directions', 'report__societes', 'report__modules')
    if not local_permissions.exists():
        logger.warning(f"No local permissions found for {user.username}. User may need to log out and log back in.")
        return set()

    # Filter reports by permissions and leaf items.
    permission_reports = [perm.report for perm in local_permissions if getattr(perm, "report_id", None)]
    leaf_reports = _filter_reportref_to_leaf_items(permission_reports)
    
    # Apply metadata filter if applicable
    if allowed_types or req_field or view_type:
        filtered_ids = set()
        for report in leaf_reports:
            if not report.pbirs_id: continue
            if view_type and view_type not in {'consolide', 'direction', 'pole', 'module'} and report.is_consolide:
                continue
            if allowed_types and report.report_type not in allowed_types: continue
            if req_field:
                if req_field == 'is_consolide':
                    if not report.is_consolide: continue
                elif req_field in ['poles', 'directions', 'modules', 'societes']:
                    # Use prefetched M2M cache (calling .all() uses prefetched results, no DB hits)
                    if not getattr(report, req_field).all(): continue
                else:
                    val = getattr(report, req_field, None)
                    if not val: continue # Skip if null or empty string
            filtered_ids.add(report.pbirs_id)
        return filtered_ids
    
    return {report.pbirs_id for report in leaf_reports if report.pbirs_id}


def get_visible_folders(request, view_type):
    """
    Filter custom folders to only show those containing at least one visible report.
    Uses the local UserReportPermission cache as the source of truth.
    """
    visible_ids = get_visible_report_ids(request, view_type=view_type)
    
    if not visible_ids:
        return []

    all_folders = list(CustomFolder.objects.filter(view_type=view_type))
    parent_by_id = {folder.id: folder.parent_id for folder in all_folders}

    allowed_types, req_field, _ = _get_allowed_report_types_for_view(view_type)
    
    filter_kwargs = {
        "folder__view_type": view_type,
        "report__pbirs_id__in": visible_ids,
    }
    if allowed_types:
        filter_kwargs["report__report_type__in"] = allowed_types
    if req_field:
        if req_field == 'is_consolide':
            filter_kwargs["report__is_consolide"] = True
        else:
            filter_kwargs[f"report__{req_field}__isnull"] = False

    direct_folder_ids = set(
        FolderReportItem.objects.filter(**filter_kwargs)
        .exclude(**{f"report__{req_field}": ""} if req_field and req_field != 'is_consolide' else {})
        .values_list("folder_id", flat=True)
    )

    visible_folder_ids = set()
    for folder_id in direct_folder_ids:
        current_id = folder_id
        while current_id:
            if current_id in visible_folder_ids:
                break
            visible_folder_ids.add(current_id)
            current_id = parent_by_id.get(current_id)

    return [folder for folder in all_folders if folder.id in visible_folder_ids]


def get_visible_reports_in_folder(request, folder):
    """
    Get reports in a specific folder that the user has access to locally.
    """
    visible_ids = get_visible_report_ids(request, view_type=folder.view_type)
    
    # Get reports assigned to this folder
    folder_items = list(FolderReportItem.objects.filter(folder=folder).select_related('report'))
    leaf_report_ids = {
        report.id for report in _filter_reportref_to_leaf_items([item.report for item in folder_items if item.report_id])
    }
    
    visible_reports = []
    allowed_types, req_field, _ = _get_allowed_report_types_for_view(folder.view_type)
    for item in folder_items:
        # Check report type
        if allowed_types and item.report.report_type not in allowed_types:
            continue
        # Check specific field (pole, direction, etc)
        if req_field:
            if req_field == 'is_consolide':
                if not item.report.is_consolide: continue
            else:
                val = getattr(item.report, req_field, None)
                if not val: continue # skip null or empty

        if item.report.id in leaf_report_ids and item.report.pbirs_id in visible_ids:
            visible_reports.append({
                'id': item.report.id,
                'pbirs_id': item.report.pbirs_id,
                'name': item.report.name,
                'path': item.report.path,
                'embed_url': item.report.embed_url,
                'order': item.order,
            })
    
    return visible_reports


@admin_required
def sync_reports_from_pbirs(request):

    try:
        synced_count, removed_count = sync_report_refs(triggered_by=request.user)
        messages.success(request, f"Successfully synced {synced_count} reports from PBIRS.")
        if removed_count:
            messages.info(request, f"Cleaned {removed_count} stale cached items.")
        log_history(request.user, f"Synced {synced_count} reports from PBIRS to local cache")
        messages.info(
            request,
            "Reports synced. Use 'Synchroniser Permission' to refresh permissions."
        )
    except Exception as exc:
        logger.error("Report sync failed: %s", exc)
        messages.error(request, f"Report sync failed: {exc}")
    
    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_business'))


@admin_required
def sync_permissions(request):
    
    try:
        users_synced, permissions_count = sync_all_user_permissions(
            triggered_by=request.user,
        )
        messages.success(request, f"Permissions synced successfully. {users_synced} users updated, {permissions_count} permissions created.")
        log_history(request.user, f"Synced permissions: {users_synced} users, {permissions_count} permissions")
    except Exception as e:
        logger.error(f"Manual permission sync failed: {e}")
        messages.error(request, f"Sync failed: {str(e)}")
        log_history(request.user, f"Permission sync failed: {str(e)}")
    
    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_business'))


@login_required
def custom_folders_list(request, view_type='direction', folder_id=None):
    """
    Display custom folders and reports for a specific view type.
    Filters based on the local PBIRS permission cache.
    """
    # Validate view type
    if view_type not in ['direction', 'pole', 'biblio', 'anomalie', 'consolide', 'module']:
        raise Http404("Invalid view type")

    is_admin = request.user.is_admin
    # Check permissions
    if view_type == 'anomalie' and not (is_admin or request.user.can_view_anomalie):
        raise PermissionDenied
    if view_type == 'consolide' and not (is_admin or request.user.can_view_consolide):
        raise PermissionDenied
    if view_type == 'direction' and not (is_admin or request.user.can_view_direction):
        raise PermissionDenied
    if view_type == 'pole' and not (is_admin or request.user.can_view_pole):
        raise PermissionDenied
    # module and biblio are always allowed

    # Determine grouping metadata option type
    if view_type == 'direction':
        grouping_type = 'direction'
    elif view_type == 'pole':
        grouping_type = 'pole'
    elif view_type == 'module':
        grouping_type = 'module'
    else:
        grouping_type = 'direction'

    # Get permitted report IDs for the user in this view
    visible_report_ids = get_visible_report_ids(request, view_type=view_type)
    permitted_reports = ReportRef.objects.filter(pbirs_id__in=visible_report_ids)

    current_folder = None
    parent_folder = None
    subfolders = []
    reports = []
    breadcrumbs = []

    from powerbi_report.models import MetadataOption
    
    if folder_id is None:
        # Root level: list unique metadata values as folders
        if grouping_type == 'direction':
            options = MetadataOption.objects.filter(option_type='direction', direction_reports__in=permitted_reports).distinct()
        elif grouping_type == 'pole':
            options = MetadataOption.objects.filter(option_type='pole', pole_reports__in=permitted_reports).distinct()
        elif grouping_type == 'module':
            options = MetadataOption.objects.filter(option_type='module', module_reports__in=permitted_reports).distinct()
        else:
            options = MetadataOption.objects.filter(option_type='direction', direction_reports__in=permitted_reports).distinct()
        subfolders = list(options)
    else:
        # Inside a folder (MetadataOption)
        current_folder = get_object_or_404(MetadataOption, id=folder_id)
        if grouping_type == 'direction':
            reports_qs = permitted_reports.filter(directions=current_folder)
        elif grouping_type == 'pole':
            reports_qs = permitted_reports.filter(poles=current_folder)
        elif grouping_type == 'module':
            reports_qs = permitted_reports.filter(modules=current_folder)
        else:
            reports_qs = permitted_reports.filter(directions=current_folder)

        if view_type != 'consolide' and grouping_type in {'direction', 'pole'} and current_folder.option_type == grouping_type:
            subfolders = list(
                MetadataOption.objects.filter(
                    option_type='societe',
                    societe_reports__in=reports_qs,
                ).distinct().order_by('name')
            )
            reports_qs = reports_qs.filter(societes__isnull=True).distinct()
        elif current_folder.option_type == 'societe':
            parent_id = request.GET.get('parent_id')
            if parent_id:
                parent_folder = MetadataOption.objects.filter(
                    id=parent_id, option_type=grouping_type
                ).first()
            reports_qs = permitted_reports.filter(societes=current_folder)
            if parent_folder:
                parent_field = 'directions' if grouping_type == 'direction' else 'poles'
                reports_qs = reports_qs.filter(**{parent_field: parent_folder})

        for r in reports_qs.order_by('name'):
            reports.append({
                'id': r.id,
                'pbirs_id': r.pbirs_id,
                'name': r.name,
                'path': r.path,
                'embed_url': r.embed_url,
            })
        breadcrumbs = [folder for folder in (parent_folder, current_folder) if folder]

    # Admin only: list all reports for assignment (not applicable now, but kept for compatibility or empty)
    all_reports = []

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    view_titles = {
        'direction': 'CBI',
        'pole': 'Rapports par Pole',
        'biblio': 'Bibliotheque',
        'anomalie': 'Anomalie',
        'consolide': 'Consolidé',
        'module': 'Module',
    }
    view_title = view_titles.get(view_type, 'Dossiers personnalisés')

    log_history(request.user, f"A consulté les dossiers personnalisés ({view_type})")

    return render(request, 'powerbi_report/custom_folders_list.html', {
        'view_type': view_type,
        'view_title': view_title,
        'current_folder': current_folder,
        'subfolders': subfolders,
        'reports': reports,
        'breadcrumbs': breadcrumbs,
        'parent_folder': parent_folder,
        'all_reports': all_reports,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    })


@admin_required
def create_custom_folder(request, view_type):
    """
    Create a new custom folder. Admin only.
    """
    
    if request.method == 'POST':
        folder_name = request.POST.get('folder_name', '').strip()
        parent_id = request.POST.get('parent_id')
        
        if not folder_name:
            messages.error(request, "Folder name is required.")
            return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + view_type))
        
        parent = None
        if parent_id:
            parent = get_object_or_404(CustomFolder, id=parent_id, view_type=view_type)
        
        # Get the next order value
        if parent:
            max_order = CustomFolder.objects.filter(parent=parent).aggregate(
                max_order=models.Max('order')
            )['max_order'] or 0
        else:
            max_order = CustomFolder.objects.filter(
                parent__isnull=True, view_type=view_type
            ).aggregate(max_order=models.Max('order'))['max_order'] or 0
        
        folder = CustomFolder.objects.create(
            name=folder_name,
            view_type=view_type,
            parent=parent,
            order=max_order + 1,
            created_by=request.user,
        )
        
        messages.success(request, f"Folder '{folder_name}' created successfully.")
        log_history(request.user, f"Created custom folder '{folder_name}' in {view_type} view")
        
        # Redirect back to the parent folder or root
        if parent:
            return redirect('powerbi_report:custom_folder_detail', view_type=view_type, folder_id=parent.id)
        return redirect('powerbi_report:custom_' + view_type)
    
    return redirect('powerbi_report:custom_' + view_type)


@admin_required
def edit_custom_folder(request, folder_id):
    """
    Edit a custom folder. Admin only.
    """
    folder = get_object_or_404(CustomFolder, id=folder_id)
    
    if request.method == 'POST':
        folder_name = request.POST.get('folder_name', '').strip()
        
        if not folder_name:
            messages.error(request, "Folder name is required.")
            return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))
        
        old_name = folder.name
        folder.name = folder_name
        folder.save()
        
        messages.success(request, f"Folder renamed from '{old_name}' to '{folder_name}'.")
        log_history(request.user, f"Renamed custom folder from '{old_name}' to '{folder_name}'")
    
    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))


@admin_required
def move_custom_folder(request, folder_id):
    """
    Move a custom folder to a new parent or view type. Admin only.
    """
    folder = get_object_or_404(CustomFolder, id=folder_id)
    
    if request.method == 'POST':
        new_parent_id = request.POST.get('new_parent_id')
        
        # Check for circular dependency
        if new_parent_id:
            if int(new_parent_id) == folder.id:
                 messages.error(request, "Cannot move a folder into itself.")
                 return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))
            
            # Check if new parent is a child of current folder
            new_parent = get_object_or_404(CustomFolder, id=new_parent_id)
            if new_parent.view_type != folder.view_type:
                 messages.error(request, "Cannot move folder to a different view type.")
                 return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))
                 
            ancestor = new_parent
            while ancestor.parent:
                if ancestor.parent.id == folder.id:
                    messages.error(request, "Cannot move a folder into its own subfolder.")
                    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))
                ancestor = ancestor.parent
            
            folder.parent = new_parent
        else:
            folder.parent = None # Move to root
            
        folder.save()
        messages.success(request, f"Folder '{folder.name}' moved successfully.")
        log_history(request.user, f"Moved custom folder '{folder.name}' (ID: {folder.id})")
        
    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:custom_' + folder.view_type))


@admin_required
def delete_custom_folder(request, folder_id):
    """
    Delete a custom folder. Admin only.
    """
    folder = get_object_or_404(CustomFolder, id=folder_id)
    view_type = folder.view_type
    parent = folder.parent
    
    if request.method == 'POST':
        folder_name = folder.name
        folder.delete()
        
        messages.success(request, f"Folder '{folder_name}' deleted successfully.")
        log_history(request.user, f"Deleted custom folder '{folder_name}'")
        
        # Redirect to parent folder or root
        if parent:
            return redirect('powerbi_report:custom_folder_detail', view_type=view_type, folder_id=parent.id)
        return redirect('powerbi_report:custom_' + view_type)
    
    return redirect('powerbi_report:custom_' + view_type)


@admin_required
def assign_report_to_folder(request, folder_id):
    """
    Assign a report to a custom folder. Admin only.
    """
    folder = get_object_or_404(CustomFolder, id=folder_id)
    
    if request.method == 'POST':
        report_ids = request.POST.getlist('report_ids')
        
        if not report_ids:
            messages.error(request, "Please select at least one report.")
            return redirect('powerbi_report:custom_folder_detail', view_type=folder.view_type, folder_id=folder.id)
        
        # Get current max order
        max_order = FolderReportItem.objects.filter(folder=folder).aggregate(
            max_order=models.Max('order')
        )['max_order'] or 0
        
        assigned_count = 0
        for report_id in report_ids:
            report = get_object_or_404(ReportRef, id=report_id)
            
            # Create assignment if not already exists
            item, created = FolderReportItem.objects.get_or_create(
                folder=folder,
                report=report,
                defaults={'order': max_order + 1 + assigned_count}
            )
            
            if created:
                assigned_count += 1
        
        if assigned_count > 0:
            messages.success(request, f"Assigned {assigned_count} report(s) to '{folder.name}'.")
            log_history(request.user, f"Assigned {assigned_count} report(s) to folder '{folder.name}'")
        else:
            messages.info(request, "Selected reports are already in this folder.")
    
    return redirect('powerbi_report:custom_folder_detail', view_type=folder.view_type, folder_id=folder.id)


@admin_required
def remove_report_from_folder(request, folder_id, report_id):
    """
    Remove a report from a custom folder. Admin only.
    """
    folder = get_object_or_404(CustomFolder, id=folder_id)
    report = get_object_or_404(ReportRef, id=report_id)
    
    if request.method == 'POST':
        deleted_count, _ = FolderReportItem.objects.filter(folder=folder, report=report).delete()
        
        if deleted_count > 0:
            messages.success(request, f"Removed '{report.name}' from '{folder.name}'.")
            log_history(request.user, f"Removed report '{report.name}' from folder '{folder.name}'")
        else:
            messages.warning(request, "Report was not in this folder.")
    
    return redirect('powerbi_report:custom_folder_detail', view_type=folder.view_type, folder_id=folder.id)


@admin_required
def get_available_reports_json(request):
    """
    API endpoint to get available reports for assignment.
    Returns reports that are synced from PBIRS.
    """
    
    view_type = request.GET.get('view_type')
    allowed_types, req_field, _ = _get_allowed_report_types_for_view(view_type) if view_type else ([], None, None)
    
    reports = ReportRef.objects.all()
    if allowed_types:
        reports = reports.filter(report_type__in=allowed_types)
    if req_field:
        if req_field == 'is_consolide':
            reports = reports.filter(is_consolide=True)
        else:
            reports = reports.exclude(**{f"{req_field}__isnull": True}).exclude(**{req_field: ""})
    if view_type and view_type != 'consolide':
        reports = reports.exclude(is_consolide=True)
    
    reports = reports.order_by('name')
    report_list = [
        {
            'id': r.id,
            'pbirs_id': r.pbirs_id,
            'name': r.name,
            'path': r.path,
        }
        for r in reports
    ]
    
    return JsonResponse({'reports': report_list})


@admin_required
def delete_powerbi_report_server(request, report_id):
    """
    Delete a report from the PBIRS Server. Admin only.
    """
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can delete reports.")
        return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:report_list'))

    if request.method == "POST":
        # report_id here is the PBIRS ID (UUID)
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})"
        auth = get_current_user_auth(request)
        
        try:
            # Get report name for notification before deletion
            report_name = report_id
            report_ref = ReportRef.objects.filter(pbirs_id=report_id).first()
            if report_ref:
                report_name = report_ref.name

            response = requests.delete(url, auth=auth)
            response.raise_for_status()
            
            # Also remove local reference
            ReportRef.objects.filter(pbirs_id=report_id).delete()
            
            messages.success(request, "Report deleted permanently from server.")
            log_history(request.user, f"Deleted report '{report_name}' from PBIRS")
            
             # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Le rapport '{report_name}' a été supprimé du serveur par {request.user.username}."
                )
            
            # Deleting the local ReportRef cascades local UserReportPermission rows.
                
        except requests.exceptions.RequestException as e:
            messages.error(request, f"Failed to delete report: {str(e)}")
            
    return redirect(request.META.get('HTTP_REFERER', 'powerbi_report:report_list'))


@login_required
def embed_custom_report(request, view_type, folder_id, report_id):
    """
    Embed a report within the context of a custom folder to preserve breadcrumbs.
    """
    # This route is reached from a metadata grouping (direction, pôle, or
    # module). Preserve its parent chain so the report page can show the full
    # path a user used to reach the report.
    from powerbi_report.models import MetadataOption
    folder = get_object_or_404(MetadataOption, id=folder_id)
    report_ref = get_object_or_404(ReportRef, id=report_id)
    
    # Access control for the view type context
    is_admin = request.user.is_admin
    if view_type == 'anomalie' and not (is_admin or request.user.can_view_anomalie):
        raise PermissionDenied
    if view_type == 'consolide' and not (is_admin or request.user.can_view_consolide):
        raise PermissionDenied
    if view_type == 'direction' and not (is_admin or request.user.can_view_direction):
        raise PermissionDenied
    if view_type == 'pole' and not (is_admin or request.user.can_view_pole):
        raise PermissionDenied
    # module and biblio are always allowed

    # Check report-specific permissions
    if not _user_can_access_report(request.user, report_ref):
         return HttpResponse("You do not have permission to view this report.", status=403)

    context = {
        'report_id': report_ref.pbirs_id,
        'report_name': report_ref.name,
        'embed_url': report_ref.embed_url,
        'view_type': view_type,
        'current_folder': folder,
    }
    
    breadcrumbs = []
    parent_id = request.GET.get('parent_id')
    parent_folder = None
    if parent_id and folder.option_type == 'societe' and view_type in {'direction', 'pole'}:
        parent_folder = MetadataOption.objects.filter(
            id=parent_id, option_type=view_type
        ).first()
    if parent_folder:
        breadcrumbs.append(parent_folder)
    else:
        breadcrumb_folder = folder.parent
        while breadcrumb_folder:
            breadcrumbs.insert(0, breadcrumb_folder)
            breadcrumb_folder = breadcrumb_folder.parent
    breadcrumbs.append(folder)
    context['breadcrumbs'] = breadcrumbs
    
    log_history(request.user, f"Rapport consulté '{report_ref.name}' dans le dossier '{folder.name}'")
    return render(request, 'powerbi_report/embed_custom_report.html', context)


@admin_required
def get_refresh_plan_history(request, plan_id):
    return get_refresh_plan_history_response(request, plan_id, get_current_user_auth)



