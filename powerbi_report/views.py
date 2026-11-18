"""
PowerBI Report Views.

This module handles all view logic for Power BI Report management,
including report listing, embedding, permissions, and folder management.
"""

import logging
import json
import urllib.parse
import re
from collections import Counter
from datetime import datetime, timedelta

import requests
from requests_ntlm import HttpNtlmAuth
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from powerbi_report.pagination import paginate
from django.db import models
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncMonth, TruncDay, TruncDate, TruncHour, TruncQuarter, TruncYear
from django.http import Http404, HttpResponse, JsonResponse, HttpResponseRedirect
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from easyaudit.models import LoginEvent
from notifications.models import Notification
from django.views.decorators.http import require_GET, require_POST
from users.models import CustomUser, UserHistory, Role
from users.session_secrets import get_ldap_password
from users.utils import log_history, get_user_permissions, admin_required, person_label

from .models import ReportRef, CustomFolder, FolderReportItem, UserReportPermission, MetadataOption
from .services import rls as rls_service
from .services.pbirs_client import PBIRSClientError
from .services import (
    PBIRSClient,
    get_active_pbirs_server_urls,
    get_group_members,
    get_primary_pbirs_server_url,
    sync_all_user_permissions,
    sync_report_permissions,
    sync_report_refs,
)
from .services.ldap_group_members import is_ldap_group
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
    current_password = get_ldap_password(request)
    
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






def _clear_user_report_cache(user_id) -> None:
    """Drop every cached PBIRS report listing of a user (see get_powerbi_reports)."""
    cache.delete_many([
        f"powerbi_reports_cache_{user_id}_PowerBIReports",
        f"powerbi_reports_cache_{user_id}_CatalogItems",
        f"dashboard_data_{user_id}",
    ])


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
    servers_answered = 0
    for server_url in server_urls:
        try:
            url = f"{server_url}/Reports/api/v2.0/{endpoint}"
            response = session.get(url=url, auth=auth, timeout=10)
            response.raise_for_status()
            servers_answered += 1

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

    # Never cache a total failure: it would show an empty catalogue for minutes.
    if servers_answered:
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

    if user.is_admin:   # same rule as every other page: the admin role counts, not only the superuser flag
        report_refs = ReportRef.objects.all().select_related('modified_by').prefetch_related('poles', 'directions', 'societes', 'modules')
    else:
        permitted_report_ids = UserReportPermission.objects.filter(
            user=user
        ).values_list('report_id', flat=True)
        report_refs = ReportRef.objects.filter(id__in=permitted_report_ids).select_related('modified_by').prefetch_related('poles', 'directions', 'societes', 'modules')

    # Defensive fallback for historical cache pollution.
    report_refs = _filter_reportref_to_leaf_items(report_refs)

    primary_server_url = get_primary_pbirs_server_url()  # one lookup, not one per report
    reports = []
    for ref in report_refs:
        path = ref.path or ""
        clean_path = path.lstrip("/")
        encoded_path = urllib.parse.quote(clean_path, safe="/")
        server_url = ref.server_url or primary_server_url
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


def _get_report_label(request, report_id: str) -> dict:
    """
    Return {'name', 'path'} for a report, never None.

    Used for notifications and history after a PBIRS policy change: the change
    has already been applied, so a failed info lookup must not abort the view.
    """
    report_ref = ReportRef.objects.filter(pbirs_id__iexact=report_id).first()
    if report_ref and report_ref.name and report_ref.path:
        return {'name': report_ref.name, 'path': report_ref.path}
    info = get_powerbi_report_info(request, report_id) or {}
    if info.get('name') and info.get('path'):
        return info
    return {
        'name': info.get('name') or (report_ref.name if report_ref else None) or report_id,
        'path': info.get('path') or (report_ref.path if report_ref else None) or '',
    }


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

    # A path is a folder when another path continues it ("/A" with "/A/B"). Every such parent
    # prefix is collected once, so each report is checked with a set lookup instead of a scan.
    parents = set()
    for path in normalized_paths.values():
        for index, char in enumerate(path):
            if char == "/" and index > 0:
                parents.add(path[:index])

    leaf_refs = []
    for ref in refs:
        path = normalized_paths.get(ref.id, "")
        if not path or path == "/":
            continue
        if path not in parents:
            leaf_refs.append(ref)

    return leaf_refs


def _format_granted_reports_message(report_names: list[str]) -> str:
    """
    Build a concise user-facing granted-access message in French.
    Example: "Vous avez obtenu l'accès à 1 rapport(s) : Sales Report"
    """
    cleaned_names = [
        (name or "").strip()
        for name in report_names
        if (name or "").strip()
    ]
    details = ", ".join(cleaned_names) if cleaned_names else "N/A"
    return f"Vous avez obtenu l'accès à {len(cleaned_names)} rapport(s) : {details}"






#################################################################################################################
#                    Retrieves permissions for a specific Power BI report                                       #
#################################################################################################################

@admin_required
def get_report_permissions(request, report_id, server_url=None, timeout=10, auth=None, strict=False):
    base_url = server_url or ReportRef.get_server_url(report_id)
    if not base_url:
        logger.warning("No PBIRS server configured while fetching permissions for report %s.", report_id)
        return []
    url = f"{base_url}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
    if auth is None:
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
        if strict:
            raise   # the caller must tell "PBIRS did not answer" from "nobody has access"
        logger.error(f"HTTP Error (Permissions) for report {report_id} on {base_url}: {errh}")
        return []
    except requests.exceptions.RequestException as err:
        if strict:
            raise
        logger.error(f"Request Error (Permissions) for report {report_id} on {base_url}: {err}")
        return []
POLICIES_UNREADABLE_MESSAGE = (
    "Impossible de lire les permissions actuelles du rapport sur PBIRS. "
    "Aucune modification n'a été appliquée ; veuillez réessayer."
)


def _policies_for_update(request, report_id: str) -> list[dict] | None:
    """
    Live policies of a report for a read-modify-write, or None when unreadable.

    A failed read must never look like "nobody has access": the list is written
    back to PBIRS, so an empty fallback would erase everyone else's access.
    Callers must not write when this returns None.
    """
    try:
        return PBIRSClient(request).get_report_policies_strict(
            report_id, base_url=ReportRef.get_server_url(report_id) or None,
        )
    except PBIRSClientError as exc:
        logger.error("Policies of report %s could not be read; nothing written: %s", report_id, exc)
        return None


def _redirect_back(request, fallback: str = 'powerbi_report:report_list'):
    """Redirect to the page the request came from (same host only)."""
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect(fallback)


#################################################################################################################
#                    Retrieves permissions for a specific folder on the report server                           #
#################################################################################################################

@admin_required
def get_folder_permissions(request, folder_id, server_url=None, timeout=10, auth=None, strict=False):
    base_url = server_url or get_primary_pbirs_server_url()
    if not base_url:
        logger.warning("No PBIRS server configured while fetching permissions for folder %s.", folder_id)
        return []
    url = f"{base_url}/Reports/api/v2.0/Folders({folder_id})/Policies"
    if auth is None:
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
        if strict:
            raise
        logger.error(f"HTTP Error (Folder Permissions) for folder {folder_id} on {base_url}: {errh}")
        return []
    except requests.exceptions.RequestException as err:
        if strict:
            raise
        logger.error(f"Request Error (Folder Permissions) for folder {folder_id} on {base_url}: {err}")
        return []


#################################################################################################################
#                    Fetches folder structure from Power BI Report Server for jsTree                            #
#################################################################################################################

@admin_required
@require_GET
def get_folders(request):
    return get_folders_response(request, get_current_user_auth)

def _update_report_metadata(report_id, user, name=None, path=None, description=None):
    """Update modification metadata for a report in ReportRef."""
    try:
        
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
#                    Renames a report in the app only (PBIRS keeps its name) and notifies admin users           #
#################################################################################################################
@admin_required
def edit_powerbi_report_name(request, report_id):
    return edit_powerbi_report_name_view(
        request=request,
        report_id=report_id,
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
        # Only this report's access changed: refreshing it takes a moment, a full sync takes minutes.
        permissions_syncer=lambda triggered_by=None: _sync_local_permissions_for_report(report_id, triggered_by),
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
        report_server_url=ReportRef.get_server_url(report_id),   # the server that holds this report
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
     # Log only when folder_path is root (empty or "/")
    # if not folder_path or folder_path == "/":
        # log_history(request.user, "Liste des rapports Power BI consultée (hiérarchie)")

    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_list_hierarchy.html', {
        'folder_structure': current_folder,
        'breadcrumbs': breadcrumbs,
        'current_path': folder_path,
        'permissions': permissions,
    })


#################################################################################################################
#                    Uploads a Power BI report to the report server and notifies admin users                    #
#################################################################################################################

@admin_required
def upload_powerbi_report(request):
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
            response = session.post(api_url, headers=headers, files=files, timeout=300)
            response.raise_for_status()
            messages.success(request, f"Report '{report_name}' uploaded successfully.")
            log_history(request.user, f"Rapport Power BI téléversé : {report_path}")
            # Clear the cache for the current user
            _clear_user_report_cache(request.user.id)

            # Notify all admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Un nouveau rapport '{report_name}' a été téléversé dans {report_path} par {person_label(request.user)}."
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
        ref_defaults = {
            'name': report_name,
            'path': pbirs_path,
            'server_url': server_url,
            'is_consolide': is_consolide,
            'report_type': report_type or None,
            'modified_by': request.user,
        }
        if ReportRef.objects.filter(pbirs_id=pbirs_id, name_locked=True).exists():
            ref_defaults.pop('name')  # keep the app-side name set on purpose
        report_ref, _ = ReportRef.objects.update_or_create(
            pbirs_id=pbirs_id,
            defaults=ref_defaults,
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


        _clear_user_report_cache(request.user.id)
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

    # A library report only shows the module in the form: its directions (hidden, so not posted) are kept.
    if report_ref.report_type != 'bibliotheque':
        report_ref.directions.set(request.POST.getlist('directions'))

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
    report_ref = ReportRef.objects.filter(pbirs_id=report_id).first()
    
    if not report_ref:
        log_history(request.user, f"Tentative d'accès à un rapport inexistant ID : {report_id}")
        raise Http404("Report not found")

    if not _user_can_access_report(request.user, report_ref):
        log_history(request.user, f"Tentative d'accès non autorisée au rapport ID : {report_id}")
        return HttpResponse("You do not have permission to view this report.", status=403)

    # Build report dict to match expected format
    report = {
        'Id': report_ref.pbirs_id,
        'Name': report_ref.name,
        'Path': report_ref.path,
        'Description': getattr(report_ref, 'description', ""),
    }
    # log_history(request.user, f"Détails du rapport Power BI consultés : {report.get('Name', 'Inconnu')} (ID : {report_id})")
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
            log_history(request.user, f"Échec d'authentification pour l'action {action} sur le plan d'actualisation ID : {refresh_plan_id} pour le rapport ID : {report_id}")
        else:
            if action == "refresh":
                refresh_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/CacheRefreshPlans({refresh_plan_id})/Model.Execute"
                try:
                    response = requests.post(refresh_url, auth=auth, headers={"Content-Type": "application/json"}, timeout=30)
                    response.raise_for_status()
                    messages.success(request, "Refresh started successfully!")
                    log_history(request.user, f"Actualisation initiée pour le plan ID : {refresh_plan_id} sur le rapport '{report['Name']}'")
                    
                    # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Actualisation lancée pour le plan (ID : {refresh_plan_id}) sur le rapport '{report['Name']}' par {person_label(request.user)}."
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
                    response = requests.delete(delete_url, auth=auth, timeout=30)
                    response.raise_for_status()
                    messages.success(request, "Refresh plan deleted successfully!")
                    log_history(request.user, f"Plan d'actualisation ID : {refresh_plan_id} supprimé pour le rapport '{report['Name']}'")
                     # Notify admin users
                    admin_users = CustomUser.objects.filter(is_superuser=True)
                    for admin in admin_users:
                        Notification.objects.create(
                            user=admin,
                            message=f"Plan d'actualisation (ID : {refresh_plan_id}) supprimé pour le rapport '{report['Name']}' par {person_label(request.user)}."
                        )
                    return redirect('powerbi_report:report_detail', report_id=report_id)

                except requests.exceptions.RequestException as err:
                    logger.error("Refresh plan delete failed for plan %s: %s", refresh_plan_id, err)
                    messages.error(request, "Failed to delete refresh plan.")
                    log_history(request.user, f"Échec de la suppression du plan ID : {refresh_plan_id} pour le rapport '{report['Name']}'. Erreur : {str(err)}")
                    return redirect('powerbi_report:report_detail', report_id=report_id)

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

    from powerbi_report.models import REPORT_TYPE_CHOICES
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
        'report': report,
        'report_ref': report_ref,
        'report_type_choices': REPORT_TYPE_CHOICES,
        'refresh_plans': refresh_plans,
        'shared_schedules': shared_schedules,
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
                        message=f"Un nouveau plan d'actualisation pour le rapport '{report_name}' (Plan: {description}) a été ajouté par {person_label(request.user)}."
                    )
            else:
                messages.error(request, f"Failed to add refresh plan: {response.text}")
                logger.error("PBIRS rejected refresh plan for report %s: %s", report_id, response.text)
        except Exception as e:
            messages.error(request, f"Error adding refresh plan: {str(e)}")

        # Redirect back to the report details page
        return HttpResponseRedirect(reverse('powerbi_report:report_detail', args=[report_id]))

    return redirect('powerbi_report:report_detail', report_id=report_id)


#################################################################################################################
#                    Displays permissions for a specific Power BI report                                        #
################################################################************************************************#

@admin_required
def report_permissions(request, report_id):
    # The local row answers without a round-trip to PBIRS (the catalogue call took seconds).
    report_ref = ReportRef.objects.filter(pbirs_id__iexact=report_id).first()
    report = None
    if report_ref:
        report = {
            "Id": report_ref.pbirs_id,
            "Name": report_ref.name,
            "Path": report_ref.path,
            "Type": "PowerBIReport",
        }
    else:
        lookup_id = str(report_id).casefold()
        report = next(
            (r for r in get_powerbi_reports(request) if str(r.get('Id', '')).casefold() == lookup_id),
            None
        )
        if not report:
            log_history(request.user, f"Tentative d'accès aux permissions d'un rapport inexistant ID : {report_id}")
            raise Http404("Report not found")

    canonical_report_id = report.get("Id", report_id)
    report_name = report.get('Name', 'Unknown Report')
    # log_history(request.user, f"Permissions du rapport Power BI consultées : {report_name} (ID : {report_id})")

    policies = get_report_permissions(request, canonical_report_id)
    if not policies:
        parent_folder_id = report.get("ParentFolderId")
        if not parent_folder_id:
            # Only now is the PBIRS catalogue needed: to learn which folder this report sits in.
            lookup_id = str(canonical_report_id).casefold()
            listed = next((r for r in get_powerbi_reports(request) if str(r.get('Id', '')).casefold() == lookup_id), None)
            parent_folder_id = (listed or {}).get("ParentFolderId")
        if parent_folder_id:
            policies = get_folder_permissions(request, parent_folder_id)

    # Process policies to identify groups and individual users
    domain_prefix = f"{settings.LDAP_DOMAIN}\\"
    identifiers = [
        (policy, (policy.get("GroupUserName") or policy.get("UserName") or "").replace(domain_prefix, "").split("\\")[-1])
        for policy in policies
    ]
    known_people = {
        value.casefold()
        for value in CustomUser.objects.filter(
            Q(ad2000__in=[i for _, i in identifiers if i]) | Q(username__in=[i for _, i in identifiers if i])
        ).values_list('ad2000', flat=True).union(
            CustomUser.objects.filter(username__in=[i for _, i in identifiers if i]).values_list('username', flat=True)
        )
        if value
    }
    group_members = {
        identifier: sorted(get_group_members(identifier))
        for _, identifier in identifiers
        if identifier and identifier.casefold() not in known_people and is_ldap_group(identifier)
    }
    usernames = {identifier for _, identifier in identifiers}
    usernames.update(member for members in group_members.values() for member in members)
    full_names = {
        user.ad2000: user.get_full_name()
        for user in CustomUser.objects.filter(ad2000__in=usernames)
    }

    processed_policies = []
    for policy, identifier in identifiers:
        if identifier in group_members:
            members = [
                {"UserName": member, "FullName": full_names.get(member) or member}
                for member in group_members[identifier]
            ]
            processed_policies.append({
                **policy,
                "GroupUserName": identifier,
                "UserName": identifier,  # For template compatibility
                "Identifier": identifier,
                "DisplayName": identifier,
                "IsGroup": True,
                "Members": members,
                "MemberCount": len(members),
            })
        else:
            display_name = full_names.get(identifier) or identifier
            processed_policies.append({
                **policy,
                "UserName": identifier,
                "GroupUserName": identifier,  # For template compatibility
                "Identifier": identifier,
                "DisplayName": display_name,
                "FullName": display_name,
                "IsGroup": False,
                "Members": [],
                "MemberCount": 0,
            })

    # Row-level security roles of each entry, read from PBIRS (best effort: the page works without them).
    rls_ref = ReportRef.objects.filter(pbirs_id__iexact=canonical_report_id).first()
    rls_available, report_has_rls = True, False
    role_ids_by_identity, rls_roles = {}, []
    try:
        state = rls_service.load_rls(PBIRSClient(request), rls_ref or ReportRef(pbirs_id=canonical_report_id))
        report_has_rls = bool(state.roles)
        rls_roles = [{'id': r['ModelRoleId'], 'name': r['ModelRoleName']} for r in state.roles]
        for entry in state.assignments:
            role_ids_by_identity[rls_service.short_name(entry.get('GroupUserName'))] = state.entry_role_ids(entry)
    except PBIRSClientError as exc:
        logger.warning("Permissions page: RLS roles of %s unavailable: %s", canonical_report_id, exc)
        rls_available = False
    role_name = {r['id']: r['name'] for r in rls_roles}
    for policy in processed_policies:
        ids = role_ids_by_identity.get(rls_service.short_name(policy.get('Identifier')), [])
        policy['RlsRoles'] = sorted(role_name[i] for i in ids)
        policy['RlsRoleIds'] = ",".join(ids)  # for the edit dialog

    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/report_permissions.html', {
        'report_id': canonical_report_id,
        'rls_roles': rls_roles,
        'rls_available': rls_available,
        'report_has_rls': report_has_rls,
        'report_name': report_name,
        'policies': processed_policies,
        'permissions': permissions,
    })





@admin_required
@require_POST
def report_rls_save(request, report_id):
    """
    Save the RLS roles of one user on one report, from the permissions page.
    The posted roles are the complete set: ticked roles are given, unticked ones removed.
    Only an employee who can open the report may hold a role.
    """

    report = ReportRef.objects.filter(pbirs_id__iexact=report_id).first() or ReportRef(pbirs_id=report_id)
    back = redirect('powerbi_report:report_permissions', report_id=report_id)
    identity = request.POST.get('identity', '').strip()
    employee = CustomUser.objects.filter(ad2000__iexact=rls_service.short_name(identity), is_active=True).first() \
        if identity else None
    if employee is None:
        messages.error(request, "Employé introuvable.")
        return back

    client = PBIRSClient(request)
    try:
        has_access = UserReportPermission.objects.filter(user=employee, report__pbirs_id__iexact=report_id).exists() or any(
            rls_service.short_name(p.get('GroupUserName') or p.get('UserName')) == rls_service.short_name(identity)
            for p in client.get_report_policies_strict(report_id, base_url=rls_service._server_url(report))
        )
        if not has_access:
            messages.error(request, f"{employee.get_full_name() or employee.username} n'a pas accès à ce rapport.")
            return back
        changed = rls_service.set_user_roles(client, report, employee.ad2000, request.POST.getlist('roles'))
    except PBIRSClientError as exc:
        logger.error("RLS save on report %s failed: %s", report_id, exc)
        messages.error(request, f"PBIRS a refusé l'opération, rien n'a été modifié ({exc}).")
        return back

    if changed:
        log_history(request.user, f"RLS : rôles de {employee.username} modifiés sur « {report.name or report_id} »")
    messages.success(request, "Rôles RLS enregistrés." if changed else "Aucun changement.")
    return back


#################################################################################################################
#                    Adds a specific user to a Power BI report's permissions                                    #
#################################################################################################################


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
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
        
        permission_exists = any(
            policy.get("GroupUserName", "").lower() == allowed_account.lower()
            for policy in current_policies
        )

        roles = [{"Name": "Explorateur"}]
        if user.role and user.role.name.lower() == settings.ADMIN_ROLE_NAME:
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
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()
            messages.success(request, f"Successfully added permissions for user {username} to report.")
            report_name = _get_report_label(request, report_id)['name']
            log_history(request.user, f"Permission ajoutée pour l'utilisateur {username} au rapport '{report_name}' avec les rôles {', '.join(role['Name'] for role in roles)}")

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
                    message=f"L'utilisateur {person_label(username)} a obtenu l'accès au rapport '{report_name}' par {person_label(request.user)}."
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
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
        
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
            log_history(request.user, f"Aucun nouvel utilisateur ajouté au rapport (ID : {report_id}) car tous les utilisateurs sélectionnés y ont déjà accès")
            messages.warning(request, "All selected users already have access to the report.")
            return redirect('powerbi_report:missing_users', report_id=report_id)
        
        payload = {"Id": report_id, "Policies": current_policies}
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()
            info = _get_report_label(request, report_id)
            messages.success(request, f"Successfully added {len(new_users_added)} user(s) to report permissions.")
            log_history(request.user, f"Utilisateurs ajoutés : {', '.join(person_label(u) for u in new_users_added)} au rapport (ID : {report_id})")

            selected_users_lower = {u.lower() for u in new_users_added}
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                admin_identifier = (admin.ad2000 or admin.username or "").lower()
                if admin_identifier in selected_users_lower:
                    continue
                Notification.objects.create(
                    user=admin,
                    message=f"Les utilisateurs {', '.join(person_label(u) for u in new_users_added)} ont obtenu l'accès au rapport {info['name']} (ID : {report_id}) dans {info['path']} par {person_label(request.user)}."
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
    
    log_history(request.user, f"Méthode de requête invalide (non POST) pour l'ajout d'utilisateurs au rapport ID : {report_id}")
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
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
        full_policies = {"Id": report_id, "InheritParentPolicy": False, "Policies": current_policies}
        
        # Active people only: a deactivated account or a "simulation." fake must not receive access.
        local_users = CustomUser.objects.filter(is_active=True).exclude(username__startswith='simulation.')

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
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=60)
            response.raise_for_status()
            messages.success(request, "Tous les utilisateurs ont reçu l'accès au rapport.")
            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
        except requests.exceptions.RequestException as err:
            logger.error("Error while adding all permissions to report %s: %s", report_id, err)
            messages.error(request, f"Échec de l'ajout des permissions sur le serveur PBIRS : {err}")
        return redirect('powerbi_report:missing_users', report_id=report_id)
    return HttpResponse("Method not allowed", status=405)


#################################################################################################################
#                    Removes a specific user from a Power BI report's permissions                               #
#################################################################################################################



@admin_required
def remove_users_from_report(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)

        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)

        updated_policies = [
            policy for policy in current_policies
            if (policy.get("GroupUserName") or policy.get("UserName", "")).split("\\")[-1].lower() != username.lower()
        ]

        payload = {
            "Id": report_id,
            "Policies": updated_policies
        }
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()
            info = _get_report_label(request, report_id)
            messages.success(request, f"Successfully removed permissions for user {username} from report.")
            log_history(request.user, f"Utilisateur {username} retiré du rapport '{info['name']}'")
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès de l'utilisateur {person_label(username)} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
                )
            user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
            if user_obj:
                Notification.objects.create(
                    user=user_obj,
                    message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
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
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
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
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()
            info = _get_report_label(request, report_id)

            messages.success(request, f"Successfully removed {len(users_removed)} user(s) from report permissions.")
            log_history(request.user, f"Utilisateurs {', '.join(person_label(u) for u in users_removed)} retirés du rapport '{info['name']}'")

            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès des utilisateurs {', '.join(person_label(u) for u in users_removed)} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
                )
            # Notify users who lost access
            for username in users_removed:
                user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
                if user_obj:
                    Notification.objects.create(
                        user=user_obj,
                        message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
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
    
    query = (request.GET.get('q') or '').strip()
    all_users = CustomUser.objects.order_by('first_name', 'last_name', 'username')
    if query:
        all_users = all_users.filter(
            Q(first_name__icontains=query) | Q(last_name__icontains=query)
            | Q(username__icontains=query) | Q(ad2000__icontains=query) | Q(email__icontains=query)
        )
    missing_users = [
        user for user in all_users.only('id', 'first_name', 'last_name', 'username', 'ad2000', 'email')
        if (user.ad2000 or user.username).lower() not in allowed_accounts
    ]
    # Thousands of accounts: render one page at a time.
    page = Paginator(missing_users, 50).get_page(request.GET.get('page'))
    log_history(request.user, f"Permissions manquantes consultées pour le rapport (ID : {report_id})")
    context = {
        'report_id': report_id,
        'missing_users': page,
        'total_missing': page.paginator.count,
        'query': query,
    }
    return render(request, 'powerbi_report/missing_users.html', context)





#################################################################################################################
#                    Displays Power BI reports a specific user has access to                                    #
#################################################################################################################

@admin_required
def user_permission(request, username):
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        messages.error(request, f"Utilisateur '{username}' introuvable dans la base de données locale.")
        return redirect('users_view')
    
    # Force sync: it rewrites stored access, so it only runs on a POST (a link or a prefetch must not trigger it)
    if request.method == 'POST' and request.POST.get('force_sync') == 'true':
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
            unreadable_ids = set()   # reports PBIRS did not answer for: their stored access is left alone
            with ThreadPoolExecutor(max_workers=20) as executor:
                future_to_report = {
                    executor.submit(
                        get_report_permissions, 
                        request, 
                        report['Id'], 
                        server_url=report.get('_server_url'),
                        strict=True,
                    ): report for report in all_reports
                }
                for future in as_completed(future_to_report):
                    report = future_to_report[future]
                    try:
                        policies = future.result()
                        report_policies[report['Id']] = policies
                    except Exception as e:
                        logger.error(f"Error fetching permissions for report {report['Id']}: {e}")
                        unreadable_ids.add(report['Id'])
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
                            server_url=folder_to_server[fid],
                            strict=True,
                        ): fid for fid in folder_ids
                    }
                    for future in as_completed(future_to_folder):
                        fid = future_to_folder[future]
                        try:
                            folder_policies[fid] = future.result()
                        except Exception as e:
                            logger.error(f"Error fetching folder permissions for {fid}: {e}")
                            folder_policies[fid] = []
                            unreadable_ids.update(
                                r['Id'] for r in reports_needing_folder_perms if r.get('ParentFolderId') == fid
                            )
                
                # Apply folder policies to reports
                for report in reports_needing_folder_perms:
                    report_policies[report['Id']] = folder_policies.get(report['ParentFolderId'], [])

            # Now calculate which reports are allowed. PBIRS fills GroupUserName for a person as well as
            # for a group, so both fields are compared with the person's names and their AD groups.
            own_names = {current_username, (selected_user.username or '').lower(), (selected_user.ad2000 or '').lower()} - {''}
            own_groups = {str(group).lower() for group in user_ad_groups}

            def _identity(policy):
                return (policy.get("UserName") or policy.get("GroupUserName") or "").split("\\")[-1].lower()

            for report in all_reports:
                policies = report_policies.get(report['Id'], [])
                identities = [_identity(policy) for policy in policies]
                is_direct = any(identity in own_names for identity in identities)
                is_allowed = is_direct or any(identity in own_groups for identity in identities)

                if is_allowed:
                    
                    allowed_reports_data.append({
                        'report': report,
                        'is_direct': is_direct
                    })
            
            # Sync to DB
            if allowed_reports_data:
                # Upsert ReportRefs (an app-side name set on purpose is never overwritten)
                locked_ids = set(
                    ReportRef.objects.filter(name_locked=True).values_list('pbirs_id', flat=True)
                )
                for item in allowed_reports_data:
                    report = item['report']
                    ref_defaults = {'path': report.get('Path')}
                    if report.get('Id') not in locked_ids:
                        ref_defaults['name'] = report.get('Name')
                    ReportRef.objects.update_or_create(
                        pbirs_id=report.get('Id'),
                        defaults=ref_defaults,
                    )
                
                # Update Permissions
                pbirs_ids = [item['report'].get('Id') for item in allowed_reports_data]
                report_refs = ReportRef.objects.filter(pbirs_id__in=pbirs_ids)
                report_ref_map = {ref.pbirs_id: ref for ref in report_refs}

                UserReportPermission.objects.filter(user=selected_user).exclude(
                    report__pbirs_id__in=unreadable_ids
                ).delete()
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
                messages.success(request, f"Permissions synchronisées avec succès : {len(new_permissions)} rapports trouvés.")
            else:
                # No permissions found via per-report ACL API (may be inherited from folder).
                # Do NOT delete existing local permissions to avoid false resets.
                messages.warning(request, "La vérification PBIRS n'a pas retourné de résultats directs. Les permissions locales restent inchangées (les accès hérités via dossier ne sont pas détectés par cette méthode).")

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
    page_obj, _ = paginate(request, allowed_reports)
    
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/user_permission.html', {
        'selected_user': username,
        'full_name': full_name,
        'reports': page_obj, # Pass page object instead of full list
        'permissions': permissions,
    })


#################################################################################################################
#                    Displays reports a specific user does not have access to                                   #
#################################################################################################################

@admin_required
def missing_permissions(request, username):
    """
    Reports this user does not have yet, read from the LOCAL copy of PBIRS (ReportRef and
    UserReportPermission), so the page opens at once: no PBIRS call happens here. The copy is refreshed
    by the permission sync; the grant buttons still write to PBIRS.
    """
    try:
        selected_user = CustomUser.objects.get(ad2000__iexact=username)
        full_name = f"{selected_user.first_name} {selected_user.last_name}".strip()
    except CustomUser.DoesNotExist:
        selected_user = None
        full_name = username

    have, synced_at = set(), None
    if selected_user:
        perms = UserReportPermission.objects.filter(user=selected_user)   # direct and through a group
        have = set(perms.values_list('report_id', flat=True))
        synced_at = perms.aggregate(last=Max('synced_at'))['last']

    missing_reports = [{
        'Id': ref.pbirs_id,
        'Name': ref.name,
        'Path': ref.path,
        'pole': ref.pole,
        'direction': ref.direction,
        'societe': ref.societe,
        'report_type': ref.report_type,
        'is_consolide': ref.is_consolide,
    } for ref in ReportRef.objects.exclude(pk__in=have).order_by('name')]

    log_history(request.user, f"Permissions incomplètes consultées pour l'utilisateur : {username}")
    permissions = get_user_permissions(request.user)

    return render(request, 'powerbi_report/missing_permissions.html', {
        'selected_username': username,
        'full_name': full_name,
        'missing_reports': missing_reports,
        'permissions_synced_at': synced_at,
        'permissions': permissions,
    })



#################################################################################################################
#                    Lists users with or without report permissions based on filter                             #
#################################################################################################################


@admin_required
def users_no_reports_view(request):  

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
    users, _ = paginate(request, users)
    
    roles = Role.objects.all()

    return render(request, 'users/user_management.html', {
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

        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
        
        permission_exists = any(
            policy.get("GroupUserName", "").lower() == allowed_account.lower()
            for policy in current_policies
        )

        roles = [{"Name": "Explorateur"}]
        if user.role and user.role.name.lower() == settings.ADMIN_ROLE_NAME:
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
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()

            cache.set(f"report_permissions_{report_id}", current_policies, timeout=300)
            # Notify the affected user
            info = _get_report_label(request, report_id)

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
                    message=f"L'utilisateur {person_label(username)} a obtenu l'accès au rapport '{info['name']}' dans '{info['path']}' par {person_label(request.user)}."
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
            policies = _policies_for_update(request, report['Id'])
            if policies is None:
                continue
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
                    response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
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
        if user_obj.role and user_obj.role.name.lower() == settings.ADMIN_ROLE_NAME:
            roles.extend([
                {"Name": "Gestionnaire de contenu"},
                {"Name": "Mes rapports"},
                {"Name": "Report Builder"},
                {"Name": "Serveur de publication"}
            ])
        
        for report_id in report_ids:
            cache_key = f"report_permissions_{report_id}"
            policies = _policies_for_update(request, report_id)
            if policies is None:
                messages.error(request, f"Permissions illisibles pour le rapport '{report_id}' : aucune modification appliquée.")
                continue
            
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
                    response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
                    response.raise_for_status()
                    info = _get_report_label(request, report_id)
                    granted_reports.append((report_id, info['name'], info['path']))
                    cache.set(cache_key, policies, timeout=300)
                    _sync_local_permissions_for_report(report_id, request.user)
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
                    message=f"L'utilisateur {person_label(username)} a obtenu l'accès à {len(granted_reports)} rapport(s) : {report_details} par {person_label(request.user)}."
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

@admin_required
def remove_permission_from_server(request, report_id, username):
    if request.method == 'POST':
        auth = get_current_user_auth(request)
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        
        current_policies = _policies_for_update(request, report_id)
        if current_policies is None:
            messages.error(request, POLICIES_UNREADABLE_MESSAGE)
            return _redirect_back(request)
        
        updated_policies = [
            policy for policy in current_policies 
            if (policy.get("GroupUserName") or policy.get("UserName")) and (policy.get("GroupUserName") or policy.get("UserName") or "").split("\\")[-1].lower() != username.lower()
        ]
        
        try:
            payload = {
                "Id": report_id,
                "Policies": updated_policies
            }
            response = requests.put(url, json=payload, auth=auth, headers={"Content-Type": "application/json"}, timeout=30)
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = _get_report_label(request, report_id)

            messages.success(request, f"Successfully removed permissions for user {username} to report.")
            log_history(request.user, f"Removed permission for user {username} to report '{info['name']}' in '{info['path']}'")
            try:
                user_obj = CustomUser.objects.get(ad2000__iexact=username)
                Notification.objects.create(
                    user=user_obj,
                    message=f"Votre accès au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
                )
            except ObjectDoesNotExist:
                logger.warning("User with ad2000=%s not found for notification.", username)
            
            # Notify admin users
            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"L'accès de l'utilisateur {person_label(username)} au rapport '{info['name']}' dans '{info['path']}' a été retiré par {person_label(request.user)}."
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
            
            policies = _policies_for_update(request, report_id)
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
                response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
                response.raise_for_status()
                
                cache.set(cache_key, updated_policies, timeout=300)
                logger.info("Permissions updated for report %s.", report_id)
                 
                _update_report_metadata(report_id, request.user)
                _sync_local_permissions_for_report(report_id, request.user)
            except requests.exceptions.HTTPError as errh:
                logger.error("HTTP Error updating report %s permissions: %s", report_id, errh)
            except requests.exceptions.RequestException as err:
                logger.error("Request Error updating report %s permissions: %s", report_id, err)
        
        user_obj = CustomUser.objects.filter(ad2000__iexact=username).first()
        if user_obj:
            Notification.objects.create(user=user_obj, message="Votre accès à tous les rapports a été retiré.")
        
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
        policies = _policies_for_update(request, report_id)
        if policies is None:
            error_messages.append(f"Permissions illisibles pour le rapport '{report_id}' : aucune modification appliquée.")
            continue

        updated_policies = [
            policy for policy in policies 
            if (policy.get("GroupUserName") or policy.get("UserName")) and 
               (policy.get("GroupUserName") or policy.get("UserName") or "").split("\\")[-1].lower() != username.lower()
        ]
        
        url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
        payload = {"Policies": updated_policies}
        headers = {"Content-Type": "application/json"}
        
        try:
            response = requests.put(url, json=payload, auth=auth, headers=headers, timeout=30)
            response.raise_for_status()
            cache.set(f"report_permissions_{report_id}", updated_policies, timeout=300)
            info = _get_report_label(request, report_id)
            _update_report_metadata(report_id, request.user)
            _sync_local_permissions_for_report(report_id, request.user)
            removed_reports.append((report_id, info['name'], info['path']))
        except requests.exceptions.RequestException as e:
            logger.error("Failed to remove permission for report ID '%s': %s", report_id, e)
            error_messages.append(f"Report {report_id}: Failed to remove permission due to request error: {str(e)}")
    
    if removed_reports:
        # Notify the affected user
        report_details = ", ".join([f"'{name}' in '{path}'" for rid, name, path in removed_reports])
        Notification.objects.create(
            user=user_obj,
            message=f"Votre accès à {len(removed_reports)} rapport(s) a été retiré : {report_details} par {person_label(request.user)}."
        )
        # Notify admin users
        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=f"L'accès de l'utilisateur {person_label(username)} a été retiré de {len(removed_reports)} rapport(s) : {report_details} par {person_label(request.user)}."
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
    total_users = CustomUser.objects.filter(is_active=True).count()
    active_users = CustomUser.objects.filter(
        is_active=True, last_login__gte=timezone.now() - timedelta(days=30)
    ).count()

    now = timezone.now()
    current_time = timezone.localtime(now)
    current_year = current_time.year
    current_month = current_time.month
    users_this_month = CustomUser.objects.filter(
        last_login__year=current_year,
        last_login__month=current_month
    ).count()

    # Use local DB count instead of PBIRS API call
    total_reports_count = ReportRef.objects.count()
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
    
    
    # --- KPI: Counts per category (from each report's own type, set when the report is classified) ---
    type_counts = dict(ReportRef.objects.values_list('report_type').annotate(total=Count('id')))
    dashboard_report_count = type_counts.get('dashboard', 0)
    extraction_report_count = type_counts.get('bibliotheque', 0)
    anomaly_report_count = type_counts.get('anomalie', 0)

    # Total synced reports
    total_synced_reports = total_reports_count

    # --- Most requested dashboards and extraction reports (from UserHistory) ---

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

    # --- Reports per category over time, by the date each report first appeared in the application.
    def reports_over_time(trunc, key, only_current_year=False):
        moment = 'first_seen'
        rows = ReportRef.objects.filter(report_type__in=['dashboard', 'bibliotheque', 'anomalie'])
        rows = (
            rows.annotate(period=trunc(moment))
            .values('period', 'report_type', 'is_consolide')
            .annotate(count=Count('id'))
            .order_by('period')
        )
        merged = {}
        for row in rows:
            if only_current_year and row['period'] and row['period'].year != current_year:
                continue
            if row['report_type'] == 'dashboard':
                kind = 'consolide' if row['is_consolide'] else 'direction'
            else:
                kind = 'biblio' if row['report_type'] == 'bibliotheque' else 'anomalie'
            merged[(row['period'], kind)] = merged.get((row['period'], kind), 0) + row['count']
        return [
            {key: period, 'folder__view_type': kind, 'count': count}
            for (period, kind), count in sorted(merged.items(), key=lambda item: (item[0][0] is None, item[0][0]))
            if period is not None
        ]

    reports_per_folder_monthly = reports_over_time(TruncMonth, 'month', only_current_year=True)
    reports_per_folder_quarterly = reports_over_time(TruncQuarter, 'quarter', only_current_year=True)
    reports_per_folder_yearly = reports_over_time(TruncYear, 'year')

    context = {
        'total_users': total_users,
        'active_users': active_users,
        'users_this_month': users_this_month,
        'total_reports': total_reports_count,
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
        return (['dashboard'], 'modules', True) # dashboards only (the library has its own view); needs a module
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






@admin_required
@require_POST
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
    
    return _redirect_back(request, 'powerbi_report:custom_direction')


@admin_required
@require_POST
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
    
    return _redirect_back(request, 'powerbi_report:custom_direction')




def _nav_option(raw_id, option_type: str):
    """The folder a user came through (from the query string), if it really is of that type."""
    if not raw_id or not str(raw_id).isdigit():
        return None
    return MetadataOption.objects.filter(id=int(raw_id), option_type=option_type).first()


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
    if view_type == 'module' and not (is_admin or request.user.can_view_module):
        raise PermissionDenied   # the mobile app already honours this flag
    # biblio is always allowed

    # Determine grouping metadata option type
    if view_type == 'direction':
        grouping_type = 'direction'
    elif view_type == 'pole':
        grouping_type = 'pole'
    elif view_type in ('module', 'biblio'):
        grouping_type = 'module'   # the library is organised by module, not by direction
    else:
        grouping_type = 'direction'

    # Get permitted report IDs for the user in this view
    visible_report_ids = get_visible_report_ids(request, view_type=view_type)
    permitted_reports = ReportRef.objects.filter(pbirs_id__in=visible_report_ids)

    current_folder = None
    parent_folder = None
    subfolders = []
    reports = []
    trail = []          # folders the user came through, outermost first
    back_url = None
    report_query = ''   # carried on report links so the report page can show the path
    loose_reports = []  # library reports that have no module yet: shown next to the module folders

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
        # A report the user may open but that has no direction / pôle / module yet would otherwise
        # vanish from its view: it is listed next to the folders instead.
        if view_type == 'biblio':
            loose_qs, loose_label = permitted_reports.filter(modules__isnull=True), 'Sans module'
        else:
            # Only reports with no classification at all: one that sits in another view is not "lost".
            unclassified = ReportRef.objects.filter(
                pbirs_id__in=get_visible_report_ids(request), report_type='dashboard', is_consolide=False,
                directions__isnull=True, poles__isnull=True, modules__isnull=True,
            )
            loose_qs, loose_label = (
                (unclassified, 'Non classé') if view_type in ('direction', 'pole', 'module')
                else (ReportRef.objects.none(), '')
            )
        loose_reports = [
            {'id': r.id, 'name': r.name, 'route': (r.path or '').strip('/'), 'label': loose_label}
            for r in loose_qs.order_by('name')
        ]
    else:
        # Inside a folder (MetadataOption)
        current_folder = get_object_or_404(MetadataOption, id=folder_id)
        root_url = reverse('powerbi_report:custom_' + view_type)

        def detail_url(folder, query=''):
            return reverse(
                'powerbi_report:custom_folder_detail',
                kwargs={'view_type': view_type, 'folder_id': folder.id},
            ) + query

        if view_type == 'pole':
            # Pôle -> société -> direction -> reports. The pôle and société we came
            # through travel in the query string so each level can offer "Retour".
            pole_ctx = _nav_option(request.GET.get('pole') or request.GET.get('parent_id'), 'pole')
            if current_folder.option_type == 'societe':
                reports_qs = permitted_reports.filter(societes=current_folder)
                if pole_ctx:
                    trail = [pole_ctx]
                    reports_qs = reports_qs.filter(poles=pole_ctx)
                    subfolders = list(
                        MetadataOption.objects.filter(option_type='direction', direction_reports__in=reports_qs)
                        .distinct().order_by('name')
                    )
                    for folder in subfolders:
                        folder.nav_query = f'?pole={pole_ctx.id}&societe={current_folder.id}'
                    # Reports that have no direction are listed here, right under the société.
                    reports_qs = reports_qs.filter(directions__isnull=True).distinct()
                    report_query = f'?pole={pole_ctx.id}'
                back_url = detail_url(pole_ctx) if pole_ctx else root_url
            elif current_folder.option_type == 'direction':
                societe_ctx = _nav_option(request.GET.get('societe'), 'societe')
                reports_qs = permitted_reports.filter(directions=current_folder)
                query = []
                if pole_ctx:
                    reports_qs = reports_qs.filter(poles=pole_ctx)
                    query.append(f'pole={pole_ctx.id}')
                if societe_ctx:
                    reports_qs = reports_qs.filter(societes=societe_ctx)
                    query.append(f'societe={societe_ctx.id}')
                trail = [folder for folder in (pole_ctx, societe_ctx) if folder]
                report_query = '?' + '&'.join(query) if query else ''
                if societe_ctx:
                    back_url = detail_url(societe_ctx, f'?pole={pole_ctx.id}' if pole_ctx else '')
                else:
                    back_url = detail_url(pole_ctx) if pole_ctx else root_url
            else:
                # The pôle itself: its sociétés, then the reports that have no société.
                reports_qs = permitted_reports.filter(poles=current_folder)
                subfolders = list(
                    MetadataOption.objects.filter(option_type='societe', societe_reports__in=reports_qs)
                    .distinct().order_by('name')
                )
                for folder in subfolders:
                    folder.nav_query = f'?pole={current_folder.id}'
                reports_qs = reports_qs.filter(societes__isnull=True).distinct()
                back_url = root_url
        else:
            # Direction (and consolidé, module): the reports come right after the
            # folder, with no société level in between.
            if current_folder.option_type == 'societe':
                # Old bookmarks: direction -> société -> reports.
                direction_ctx = _nav_option(request.GET.get('parent_id'), 'direction')
                reports_qs = permitted_reports.filter(societes=current_folder)
                if direction_ctx:
                    trail = [direction_ctx]
                    reports_qs = reports_qs.filter(directions=direction_ctx)
                    report_query = f'?parent_id={direction_ctx.id}'
            elif grouping_type == 'module':
                reports_qs = permitted_reports.filter(modules=current_folder)
            else:
                reports_qs = permitted_reports.filter(directions=current_folder)
            back_url = root_url

        for r in reports_qs.order_by('name'):
            reports.append({
                'id': r.id,
                'pbirs_id': r.pbirs_id,
                'name': r.name,
                'path': r.path,
                'embed_url': r.embed_url,
            })
        parent_folder = trail[-1] if trail else None

    breadcrumbs = trail + [current_folder] if current_folder else []

    # Admin only: list all reports for assignment (not applicable now, but kept for compatibility or empty)
    all_reports = []

    permissions = get_user_permissions(request.user)

    view_titles = {
        'direction': 'CBI',
        'pole': 'Rapports par Pôle',
        'biblio': 'Bibliothèque',
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
        'loose_reports': loose_reports,
        'breadcrumbs': breadcrumbs,
        'parent_folder': parent_folder,
        'back_url': back_url,
        'report_query': report_query,
        'all_reports': all_reports,
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
            return _redirect_back(request, 'powerbi_report:custom_' + view_type)
        
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
        
        CustomFolder.objects.create(
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
            return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)
        
        old_name = folder.name
        folder.name = folder_name
        folder.save()
        
        messages.success(request, f"Folder renamed from '{old_name}' to '{folder_name}'.")
        log_history(request.user, f"Renamed custom folder from '{old_name}' to '{folder_name}'")
    
    return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)


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
                 return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)
            
            # Check if new parent is a child of current folder
            new_parent = get_object_or_404(CustomFolder, id=new_parent_id)
            if new_parent.view_type != folder.view_type:
                 messages.error(request, "Cannot move folder to a different view type.")
                 return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)
                 
            ancestor = new_parent
            while ancestor.parent:
                if ancestor.parent.id == folder.id:
                    messages.error(request, "Cannot move a folder into its own subfolder.")
                    return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)
                ancestor = ancestor.parent
            
            folder.parent = new_parent
        else:
            folder.parent = None # Move to root
            
        folder.save()
        messages.success(request, f"Folder '{folder.name}' moved successfully.")
        log_history(request.user, f"Moved custom folder '{folder.name}' (ID: {folder.id})")
        
    return _redirect_back(request, 'powerbi_report:custom_' + folder.view_type)


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
def delete_powerbi_report_server(request, report_id):
    """
    Delete a report from the PBIRS Server. Admin only.
    """
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can delete reports.")
        return _redirect_back(request, 'powerbi_report:report_list')

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

            response = requests.delete(url, auth=auth, timeout=30)
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
                    message=f"Le rapport '{report_name}' a été supprimé du serveur par {person_label(request.user)}."
                )
            
            # Deleting the local ReportRef cascades local UserReportPermission rows.
                
        except requests.exceptions.RequestException as e:
            messages.error(request, f"Failed to delete report: {str(e)}")
            
    return _redirect_back(request, 'powerbi_report:report_list')


@login_required
def embed_custom_report(request, view_type, folder_id, report_id):
    """
    Embed a report within the context of a custom folder to preserve breadcrumbs.
    """
    # This route is reached from a metadata grouping (direction, pôle, or
    # module). Preserve its parent chain so the report page can show the full
    # path a user used to reach the report.
    folder = get_object_or_404(MetadataOption, id=folder_id)
    report_ref = get_object_or_404(ReportRef, id=report_id)
    
    # Check report-specific permissions
    if not _user_can_access_report(request.user, report_ref):
        raise PermissionDenied

    # Access control for the view type context. A shared link can land a user
    # who has the report but not this folder view: open the plain report page.
    is_admin = request.user.is_admin
    view_flags = {
        'anomalie': 'can_view_anomalie',
        'consolide': 'can_view_consolide',
        'direction': 'can_view_direction',
        'pole': 'can_view_pole',
        'module': 'can_view_module',
    }  # the library is always allowed
    flag = view_flags.get(view_type)
    if flag and not (is_admin or getattr(request.user, flag, False)):
        return redirect('powerbi_report:embed_report', report_path=(report_ref.path or '').strip('/'))

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
    if view_type == 'pole' and folder.option_type in {'societe', 'direction'}:
        # Pôle -> société -> direction: rebuild the path from the query string.
        pole = _nav_option(request.GET.get('pole') or parent_id, 'pole')
        societe = _nav_option(request.GET.get('societe'), 'societe') if folder.option_type == 'direction' else None
        breadcrumbs = [item for item in (pole, societe) if item]
    elif parent_id and folder.option_type == 'societe' and view_type in {'direction', 'pole'}:
        parent_folder = MetadataOption.objects.filter(
            id=parent_id, option_type=view_type
        ).first()
    if breadcrumbs:
        pass
    elif parent_folder:
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



