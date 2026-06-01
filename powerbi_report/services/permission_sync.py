"""
Permission Sync Service for PBIRS Reports.

This module handles synchronization of report permissions from PBIRS to the local database.
It queries PBIRS for each user's accessible reports and stores them in UserReportPermission.
"""

import logging
import re
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from requests_ntlm import HttpNtlmAuth
from django.conf import settings
from django.utils import timezone

from users.models import CustomUser
from powerbi_report.models import ReportRef, UserReportPermission, PermissionSyncLog
from django.db import transaction, connection, close_old_connections
from django.db.models import Q

from powerbi_report.services.pbirs_servers import get_active_pbirs_server_urls, get_primary_pbirs_server_url


logger = logging.getLogger('powerbi_report')


class PermissionSyncService:
    """
    Service for synchronizing PBIRS report permissions to local database.
    
    This service uses the service account credentials to fetch all reports,
    then iterates through active users to determine their accessible reports.
    """
    
    DEFAULT_TIMEOUT = 15
    BULK_BATCH_SIZE = 5000
    FULL_SYNC_LOCK_ID = 2026041601
    
    def __init__(self, triggered_by=None):
        """
        Initialize the sync service.
        
        Args:
            triggered_by: The user who triggered the sync (for logging).
        """
        self.triggered_by = triggered_by
        self.base_url = get_primary_pbirs_server_url()
        self.server_urls = get_active_pbirs_server_urls()
        self._report_fetch_success = {}
        self._sync_lock_acquired = False
        self.sync_log = None
    
    def _get_service_auth(self):
        """Get NTLM auth using service account credentials."""
        username = settings.LDAP_SERVICE_USERNAME
        password = settings.LDAP_SERVICE_PASSWORD
        domain = settings.LDAP_DOMAIN
        
        logger.info(f"Using service account: {domain}\\{username}")
        if not all([username, password, domain]):
            logger.error(f"LDAP service account credentials not configured. username={username}, domain={domain}")
            return None
        
        ntlm_username = f"{domain}\\{username}"
        return HttpNtlmAuth(ntlm_username, password)
    
    def _get_user_auth(self, user, password):
        """Get NTLM auth for a specific user."""
        domain = settings.LDAP_DOMAIN
        ntlm_username = f"{domain}\\{user.username}"
        return HttpNtlmAuth(ntlm_username, password)

    @staticmethod
    def _clean_identity(value):
        """Normalize a PBIRS policy identity to the account/group short name."""
        if not value:
            return ""

        cleaned = str(value).strip()
        if not cleaned:
            return ""

        if "\\" in cleaned:
            cleaned = cleaned.split("\\")[-1]

        # Some LDAP payloads come as distinguished names.
        if cleaned.upper().startswith("CN="):
            cleaned = cleaned[3:].split(",", 1)[0]

        return cleaned.strip()

    @classmethod
    def _identity_key(cls, value):
        """Case-insensitive key for usernames, AD2000 identifiers, and groups."""
        return cls._clean_identity(value).casefold()

    @staticmethod
    def _normalize_path(path):
        """Normalize PBIRS paths for local comparisons."""
        if not path:
            return ""
        normalized = str(path).strip()
        if normalized != "/":
            normalized = normalized.rstrip("/")
        return normalized.casefold()

    @staticmethod
    def _parent_paths(report_path):
        """Return parent folder paths from closest parent up to root."""
        parts = [part for part in (report_path or "").strip("/").split("/") if part]
        parent_parts = parts[:-1]
        paths = []
        while parent_parts:
            paths.append("/" + "/".join(parent_parts))
            parent_parts = parent_parts[:-1]
        paths.append("/")
        return paths

    @classmethod
    def _looks_like_group(cls, value):
        """Avoid calling the group API for user/ad2000-looking policy identities."""
        cleaned = cls._clean_identity(value)
        if not cleaned:
            return False

        if cls._is_ignored_policy_identity(cleaned):
            return False

        if re.fullmatch(r"H\d+", cleaned, flags=re.IGNORECASE):
            return False

        return "-" in cleaned or cleaned.isupper()

    @classmethod
    def _is_ignored_policy_identity(cls, value):
        """Return true for PBIRS/system identities that are not AD report groups."""
        cleaned = cls._clean_identity(value).casefold()
        return cleaned in {
            "administrators",
            "administrator",
            "system",
        }
    
    def _fetch_reports_for_auth(self, auth, base_url=None):
        """
        Fetch reports accessible to a given auth credential from a single server.
        
        Args:
            auth: HttpNtlmAuth object.
            base_url: PBIRS server base URL. Defaults to primary.
            
        Returns:
            List of report dictionaries (Type=PowerBIReport only).
        """
        base_url = base_url or self.base_url
        try:
            url = f"{base_url}/Reports/api/v2.0/PowerBIReports"
            logger.info(f"Fetching reports from: {url}")
            response = requests.get(url, auth=auth, timeout=30)
            logger.info(f"Response status: {response.status_code}")
            response.raise_for_status()
            self._report_fetch_success[base_url] = True
            
            items = response.json().get('value', [])
            folder_paths = {
                self._normalize_path(path)
                for path in self._fetch_folder_paths_for_auth(auth, base_url=base_url)
            }

            filtered_reports = []
            for item in items:
                item_path = item.get("Path", "")
                if not item_path:
                    continue

                item_type = self._normalize_pbirs_item_type(item)
                if item_type == "Folder":
                    continue
                if self._normalize_path(item_path) in folder_paths:
                    continue

                # Tag each report with the server it came from
                item['_server_url'] = base_url
                filtered_reports.append(item)

            return filtered_reports
        except requests.exceptions.RequestException as e:
            self._report_fetch_success[base_url] = False
            logger.error(f"Failed to fetch reports from {base_url}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                 logger.error(f"Response content: {e.response.text}")
            return []

    def _fetch_reports_for_auth_all_servers(self, auth):
        """
        Fetch reports from ALL configured PBIRS servers for a given auth.
        
        Args:
            auth: HttpNtlmAuth object.
            
        Returns:
            List of report dictionaries from all servers.
        """
        all_reports = []
        for server_url in self.server_urls:
            reports = self._fetch_reports_for_auth(auth, base_url=server_url)
            all_reports.extend(reports)
            logger.info(f"Fetched {len(reports)} reports from {server_url}")
        return all_reports

    @staticmethod
    def _normalize_pbirs_item_type(item: dict) -> str:
        """Normalize PBIRS type fields to canonical values."""
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
            if raw_type == 1:
                return "Folder"
            if raw_type in (2, 13):
                return "PowerBIReport"

        return ""

    def _fetch_folder_paths_for_auth(self, auth, base_url=None) -> set[str]:
        """Fetch folder paths from a PBIRS server using provided auth."""
        base_url = base_url or self.base_url
        try:
            url = f"{base_url}/Reports/api/v2.0/Folders"
            response = requests.get(url, auth=auth, timeout=30)
            response.raise_for_status()
            items = response.json().get("value", [])
            return {item.get("Path", "") for item in items if item.get("Path")}
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to fetch folder paths from {base_url}: {e}")
            return set()

    def _fetch_folders_for_auth(self, auth, base_url=None):
        """Fetch folder objects from a PBIRS server using provided auth."""
        base_url = base_url or self.base_url
        try:
            url = f"{base_url}/Reports/api/v2.0/Folders"
            response = requests.get(url, auth=auth, timeout=30)
            response.raise_for_status()
            folders = response.json().get("value", [])
            for folder in folders:
                folder["_server_url"] = base_url
            return folders
        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to fetch folders from {base_url}: {e}")
            return []

    def _fetch_folders_for_auth_all_servers(self, auth):
        """Fetch folders from all configured PBIRS servers."""
        folders = []
        for server_url in self.server_urls:
            folders.extend(self._fetch_folders_for_auth(auth, base_url=server_url))
        return folders
    
    def _fetch_all_reports_as_admin(self):
        """Fetch all reports from all servers using service account."""
        auth = self._get_service_auth()
        if not auth:
            return []
        return self._fetch_reports_for_auth_all_servers(auth)

    def _fetch_report_policies_for_auth(
        self,
        auth,
        report,
        folder_by_server_path=None,
        folder_policy_cache=None,
        lock=None,
    ):
        """
        Fetch policies for a report, falling back to inherited folder policies.

        Normal page navigation never calls this; it is intentionally used by
        sync jobs/manual admin sync only.
        """
        report_id = report.get("Id")
        server_url = report.get("_server_url") or self.base_url
        if not report_id:
            return []

        try:
            url = f"{server_url}/Reports/api/v2.0/PowerBIReports({report_id})/Policies"
            self._pace_policy_request()
            response = self._get_pbirs_policy_response(
                url,
                auth,
                f"report policies for {report_id} on {server_url}",
            )
            data = response.json()
            policies = data.get("Policies", [])
            if policies:
                return policies
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Failed to fetch report policies for %s on %s: %s",
                report_id,
                server_url,
                e,
            )

        folder_by_server_path = folder_by_server_path or {}
        folder_policy_cache = folder_policy_cache if folder_policy_cache is not None else {}

        parent_folder_id = report.get("ParentFolderId")
        if parent_folder_id:
            policies = self._fetch_folder_policies_for_auth(
                auth,
                server_url,
                parent_folder_id,
                folder_policy_cache,
                lock=lock,
            )
            if policies:
                return policies

        for parent_path in self._parent_paths(report.get("Path", "")):
            folder = folder_by_server_path.get((server_url, self._normalize_path(parent_path)))
            if not folder:
                continue

            folder_id = folder.get("Id")
            if not folder_id:
                continue

            policies = self._fetch_folder_policies_for_auth(
                auth,
                server_url,
                folder_id,
                folder_policy_cache,
                lock=lock,
            )
            if policies:
                return policies

        return []


    def _pace_policy_request(self):
        """Apply a tiny delay between PBIRS policy requests during bulk sync."""
        delay = max(0.0, getattr(settings, "PBIRS_SYNC_POLICY_REQUEST_DELAY", 0.0))
        if delay:
            time.sleep(delay)

    def _get_pbirs_policy_response(self, url, auth, context):
        """
        Fetch a PBIRS policy URL with short retries for transient connection refusals.
        """
        retries = max(0, getattr(settings, "PBIRS_SYNC_POLICY_RETRIES", 2))
        retry_delay = max(0.0, getattr(settings, "PBIRS_SYNC_POLICY_RETRY_DELAY", 1.5))
        timeout = getattr(settings, "PBIRS_SYNC_POLICY_TIMEOUT", self.DEFAULT_TIMEOUT)

        for attempt in range(retries + 1):
            try:
                response = requests.get(url, auth=auth, timeout=timeout)
                response.raise_for_status()
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                if attempt >= retries:
                    raise

                sleep_seconds = retry_delay * (attempt + 1)
                logger.info(
                    "Transient PBIRS failure while fetching %s; retrying in %.1fs (%s/%s).",
                    context,
                    sleep_seconds,
                    attempt + 1,
                    retries,
                )
                time.sleep(sleep_seconds)

        raise requests.exceptions.RequestException(f"Failed to fetch {context}")

    def _fetch_folder_policies_for_auth(self, auth, server_url, folder_id, folder_policy_cache, lock=None):
        """Fetch and cache policies for a PBIRS folder."""
        cache_key = (server_url, folder_id)
        
        if lock:
            with lock:
                in_cache = cache_key in folder_policy_cache
        else:
            in_cache = cache_key in folder_policy_cache

        if in_cache:
            if lock:
                with lock:
                    return folder_policy_cache[cache_key]
            else:
                return folder_policy_cache[cache_key]

        try:
            url = f"{server_url}/Reports/api/v2.0/Folders({folder_id})/Policies"
            self._pace_policy_request()
            response = self._get_pbirs_policy_response(
                url,
                auth,
                f"folder policies for {folder_id} on {server_url}",
            )
            policies = response.json().get("Policies", [])
        except requests.exceptions.RequestException as e:
            logger.warning(
                "Failed to fetch folder policies for %s on %s: %s",
                folder_id,
                server_url,
                e,
            )
            policies = []

        if lock:
            with lock:
                folder_policy_cache[cache_key] = policies
        else:
            folder_policy_cache[cache_key] = policies
        return policies


    def _build_local_identity_maps(self):
        """Build fast lookup maps for local users and their cached AD groups."""
        users = list(
            CustomUser.objects.filter(is_active=True).only(
                "id",
                "username",
                "ad2000",
                "ad_groups",
            )
        )

        users_by_identity = {}
        users_by_group = defaultdict(list)

        for user in users:
            for identifier in (user.username, user.ad2000):
                key = self._identity_key(identifier)
                if key:
                    users_by_identity[key] = user

            raw_groups = user.ad_groups or []
            if not isinstance(raw_groups, list):
                raw_groups = [raw_groups]

            for group in raw_groups:
                group_key = self._identity_key(group)
                if group_key:
                    users_by_group[group_key].append(user)

        return users, users_by_identity, users_by_group

    def _resolve_policy_users(
        self,
        policy,
        users_by_identity,
        users_by_group,
        resolved_group_cache=None,
        lock=None,
    ):
        """
        Resolve one PBIRS policy to local users.

        Uses only the local ad_groups data (synced by the user sync button).
        No external LDAP API calls are made here.

        1. If the policy identity matches a local user directly → return that user.
        2. Otherwise, look up the identity in users_by_group (built from
           each user's cached ad_groups field) → return all matching users.
        """
        raw_identity = policy.get("UserName") or policy.get("GroupUserName")
        identity_key = self._identity_key(raw_identity)
        if not identity_key:
            return []

        # 1. Direct user match (username or ad2000)
        direct_user = users_by_identity.get(identity_key)
        if direct_user:
            return [(direct_user, True)]

        # 2. Group match via local ad_groups
        group_users = users_by_group.get(identity_key, [])
        if group_users:
            return [(user, False) for user in group_users]

        # 3. Skip well-known system identities silently
        if self._is_ignored_policy_identity(raw_identity):
            return []

        # 4. No match found — log for visibility
        logger.debug(
            "Policy identity '%s' not matched to any local user or ad_group.",
            raw_identity,
        )
        return []


    
    def _sync_reports_to_local(self, reports):
        """
        Sync report metadata to local ReportRef table.
        Syncs only PowerBIReport types. Uses atomic transaction to prevent locking.
        
        Args:
            reports: List of report dictionaries from PBIRS.
            
        Returns:
            Number of reports synced.
        """
        synced_count = 0
        
        with transaction.atomic():
            for report in reports:
                pbirs_id = report.get('Id')
                name = report.get('Name', 'Unnamed')
                path = report.get('Path', '')
                server_url = report.get('_server_url', self.base_url)
                
                # Build embed URL using the report's own server
                base_embed_url = f"{server_url}/Reports/powerbi/"
                clean_path = path.strip('/')
                encoded_path = urllib.parse.quote(clean_path, safe='/')
                embed_url = f"{base_embed_url}{encoded_path}?rs:embed=true"
                
                # Update or create
                ReportRef.objects.update_or_create(
                    pbirs_id=pbirs_id,
                    defaults={
                        'name': name,
                        'path': path,
                        'server_url': server_url,
                        'embed_url': embed_url,
                    }
                )
                synced_count += 1
        
        logger.info(f"Synced {synced_count} reports to ReportRef table")
        return synced_count

    def _remove_stale_report_refs(self, reports):
        """
        Remove local reports that disappeared from successfully fetched PBIRS servers.

        If one server is unreachable, its local reports are left untouched instead
        of being deleted from a partial sync result.
        """
        valid_ids_by_server = defaultdict(set)
        for report in reports:
            report_id = report.get("Id")
            server_url = report.get("_server_url") or self.base_url
            if report_id:
                valid_ids_by_server[server_url].add(report_id)

        removed_report_refs = 0
        removed_related_rows = 0
        for server_url in self.server_urls:
            if not self._report_fetch_success.get(server_url):
                continue

            server_filter = Q(server_url=server_url)
            if server_url == self.base_url:
                server_filter |= Q(server_url__isnull=True) | Q(server_url="")

            queryset = ReportRef.objects.filter(server_filter)
            valid_ids = valid_ids_by_server.get(server_url, set())
            if valid_ids:
                queryset = queryset.exclude(pbirs_id__in=valid_ids)

            deleted_count, deleted_details = queryset.delete()
            deleted_report_refs = deleted_details.get("powerbi_report.ReportRef", 0)
            removed_report_refs += deleted_report_refs
            removed_related_rows += max(deleted_count - deleted_report_refs, 0)

        if removed_report_refs:
            logger.info(
                "Removed %s stale ReportRef rows and %s related rows.",
                removed_report_refs,
                removed_related_rows,
            )

        return removed_report_refs

    def sync_reports_only(self):
        """
        Sync only PBIRS report metadata into ReportRef using the service account.
        """
        auth = self._get_service_auth()
        if not auth:
            raise Exception("LDAP service account credentials are not configured.")

        reports = self._fetch_reports_for_auth_all_servers(auth)
        if not reports:
            raise Exception("Failed to fetch reports from PBIRS")

        synced_count = self._sync_reports_to_local(reports)
        removed_count = self._remove_stale_report_refs(reports)

        return synced_count, removed_count
    
    def sync_permissions_for_user_with_password(self, user, password):
        """
        Sync permissions for a single user using their credentials.
        
        Args:
            user: CustomUser instance.
            password: User's LDAP password.
            
        Returns:
            Number of permissions created/updated.
        """
        from django.db import transaction
        
        auth = self._get_user_auth(user, password)
        reports = self._fetch_reports_for_auth_all_servers(auth)
        
        # Robustness fix: Ensure these reports exist in ReportRef
        # This handles case where service account cannot see some reports
        # And ReportRef might be empty or incomplete
        self._sync_reports_to_local(reports)
        
        # PowerBIReports endpoint returns only valid reports
        accessible_pbirs_ids = {
            r.get('Id') for r in reports 
            if r.get('Id')
        }
        
        # Get corresponding ReportRef objects
        accessible_reports = ReportRef.objects.filter(pbirs_id__in=accessible_pbirs_ids)
        
        permissions_to_create = []
        
        # Fetch policies in bulk or parallel would be better, but for now we'll skip the policy check 
        # for every single report to improve performance and avoid locking.
        # We will assume is_direct=False (Group) by default and rely on direct checks only if needed.
        # This drastically reduces HTTP requests and DB writes time.
        
        for report in accessible_reports:
            permissions_to_create.append(
                UserReportPermission(user=user, report=report, is_direct=False)
            )
            
        with transaction.atomic():
            # Clear old permissions
            UserReportPermission.objects.filter(user=user).delete()
            # Bulk create new permissions
            UserReportPermission.objects.bulk_create(permissions_to_create)
        
        permissions_created = len(permissions_to_create)
        logger.info(f"Synced {permissions_created} permissions for user {user.username}")
        return permissions_created
    
    def sync_all_permissions_with_service_account(self, user_for_auth=None, password_for_auth=None):
        """
        Sync permissions for ALL users from PBIRS policies into local DB.

        This is the expensive operation. It should run manually, on an
        external hourly schedule, or after admin permission changes. Normal
        report navigation reads UserReportPermission and never calls this.
        
        Args:
            user_for_auth: Optional user object to use for fetching the initial report list 
                           instead of the service account.
            password_for_auth: Password for user_for_auth, if provided.
        """
        if not self._acquire_full_sync_lock():
            raise Exception("A PBIRS permission sync is already running.")

        self.started_at = timezone.now()
        self.sync_log = PermissionSyncLog.objects.create(
            triggered_by=self.triggered_by,
            users_synced=0,
            permissions_created=0,
            status='started'
        )
        
        try:
            # 1. Determine authentication to use for fetching policies.
            auth_to_use = None
            if user_for_auth and password_for_auth:
                logger.info(f"Using credentials of {user_for_auth.username} for authentication")
                auth_to_use = self._get_user_auth(user_for_auth, password_for_auth)
            
            if not auth_to_use:
                logger.info("Using service account credentials for authentication.")
                auth_to_use = self._get_service_auth()
            
            if not auth_to_use:
                raise Exception("No valid authentication method available to fetch policies.")

            # 2. Retrieve reports from the local ReportRef table.
            report_refs = list(ReportRef.objects.all())
            if not report_refs:
                raise Exception("No reports found in local ReportRef database. Please sync reports first.")

            all_reports = [
                {
                    "Id": ref.pbirs_id,
                    "Name": ref.name,
                    "Path": ref.path,
                    "_server_url": ref.server_url or self.base_url,
                }
                for ref in report_refs
            ]
            logger.info(f"Loaded {len(all_reports)} reports from local database for permission syncing.")

            folders = self._fetch_folders_for_auth_all_servers(auth_to_use)
            folder_by_server_path = {
                (folder.get("_server_url") or self.base_url, self._normalize_path(folder.get("Path", ""))): folder
                for folder in folders
                if folder.get("Path")
            }

            _, users_by_identity, users_by_group = self._build_local_identity_maps()
            logger.info(
                "Built local identity maps: %s user identities, %s unique groups from ad_groups.",
                len(users_by_identity),
                len(users_by_group),
            )
            report_ref_by_pbirs_id = {ref.pbirs_id: ref for ref in report_refs}

            permission_map = {}
            folder_policy_cache = {}

            folder_policy_lock = threading.Lock()
            permission_map_lock = threading.Lock()

            def process_report(report):
                close_old_connections()
                try:
                    report_id = report.get("Id")
                    report_ref = report_ref_by_pbirs_id.get(report_id)
                    if not report_ref:
                        return

                    policies = self._fetch_report_policies_for_auth(
                        auth_to_use,
                        report,
                        folder_by_server_path=folder_by_server_path,
                        folder_policy_cache=folder_policy_cache,
                        lock=folder_policy_lock,
                    )

                    local_permissions = []
                    for policy in policies:
                        resolved = self._resolve_policy_users(
                            policy,
                            users_by_identity,
                            users_by_group,
                        )
                        for user, is_direct in resolved:
                            local_permissions.append((user.id, report_ref.id, is_direct))

                    with permission_map_lock:
                        for user_id, ref_id, is_direct in local_permissions:
                            key = (user_id, ref_id)
                            permission_map[key] = permission_map.get(key, False) or is_direct
                finally:
                    close_old_connections()

            max_workers = getattr(settings, 'PBIRS_SYNC_MAX_WORKERS', 20)
            logger.info(f"Starting parallel policy fetch with {max_workers} worker threads.")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(process_report, report) for report in all_reports]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        logger.error(f"Error processing report policies in thread: {exc}")


            permissions_to_create = [
                UserReportPermission(
                    user_id=user_id,
                    report_id=report_id,
                    is_direct=is_direct,
                )
                for (user_id, report_id), is_direct in permission_map.items()
            ]
            users_synced = len({user_id for user_id, _ in permission_map.keys()})
            total_permissions = len(permissions_to_create)

            # Rebuild the permission table in bulk to avoid per-row CRUD signal overhead.
            with transaction.atomic():
                self._clear_user_report_permissions_fast()
                if permissions_to_create:
                    UserReportPermission.objects.bulk_create(
                        permissions_to_create,
                        batch_size=self.BULK_BATCH_SIZE,
                    )
            
            # Update sync log
            self.sync_log.status = 'completed'
            self.sync_log.completed_at = timezone.now()
            self.sync_log.users_synced = users_synced
            self.sync_log.permissions_created = total_permissions
            self.sync_log.save()
            
            logger.info(f"Permission sync completed: {users_synced} users, {total_permissions} permissions")
            return users_synced, total_permissions
            
        except Exception as e:
            logger.error(f"Permission sync failed: {e}")
            if self.sync_log:
                self.sync_log.status = 'failed'
                self.sync_log.error_message = str(e)
                self.sync_log.completed_at = timezone.now()
                self.sync_log.save()
            raise
        finally:
            self._release_full_sync_lock()

    def _clear_user_report_permissions_fast(self):
        """
        Clear UserReportPermission rows using SQL to avoid per-row delete signals.

        This is significantly faster than ORM `delete()` for full-table rebuilds.
        """
        table_name = connection.ops.quote_name(UserReportPermission._meta.db_table)
        with connection.cursor() as cursor:
            if connection.vendor == 'postgresql':
                cursor.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY")
            else:
                cursor.execute(f"DELETE FROM {table_name}")

    def _acquire_full_sync_lock(self):
        """Prevent concurrent full permission syncs across web and worker containers."""
        if connection.vendor != 'postgresql':
            self._sync_lock_acquired = True
            return True

        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [self.FULL_SYNC_LOCK_ID])
            self._sync_lock_acquired = bool(cursor.fetchone()[0])

        return self._sync_lock_acquired

    def _release_full_sync_lock(self):
        """Release the PostgreSQL advisory lock used by full permission sync."""
        if not self._sync_lock_acquired:
            return

        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [self.FULL_SYNC_LOCK_ID])

        self._sync_lock_acquired = False
    
    def sync_permissions_on_login(self, user, password):
        """
        Sync permissions for a user when they log in.
        Called from the login view with the user's actual password.
        
        Args:
            user: CustomUser instance.
            password: User's LDAP password (from login form).
            
        Returns:
            Number of permissions synced.
        """
        try:
            # First ensure ReportRef is up to date
            all_reports = self._fetch_all_reports_as_admin()
            if all_reports:
                self._sync_reports_to_local(all_reports)
            
            # Now sync this user's permissions using their credentials
            return self.sync_permissions_for_user_with_password(user, password)
        except Exception as e:
            logger.error(f"Failed to sync permissions on login for {user.username}: {e}")
            return 0

    def sync_permissions_for_report_id(self, report_id):
        """
        Refresh local permissions for a single report using the service account.

        This is useful after an admin grants/revokes permissions on one report:
        it keeps local navigation accurate without running a full all-report sync.
        """
        auth = self._get_service_auth()
        if not auth:
            raise Exception("LDAP service account credentials are not configured.")

        report_ref = ReportRef.objects.filter(pbirs_id__iexact=report_id).first()
        if not report_ref:
            report_data = None
            for report in self._fetch_reports_for_auth_all_servers(auth):
                if str(report.get("Id", "")).casefold() == str(report_id).casefold():
                    self._sync_reports_to_local([report])
                    report_ref = ReportRef.objects.filter(pbirs_id=report.get("Id")).first()
                    report_data = report
                    break
            if not report_ref:
                raise Exception(f"Report {report_id} was not found in PBIRS or local cache.")
        else:
            report_data = {
                "Id": report_ref.pbirs_id,
                "Name": report_ref.name,
                "Path": report_ref.path,
                "_server_url": report_ref.server_url or self.base_url,
            }

        folders = self._fetch_folders_for_auth(auth, base_url=report_data.get("_server_url"))
        folder_by_server_path = {
            (folder.get("_server_url") or self.base_url, self._normalize_path(folder.get("Path", ""))): folder
            for folder in folders
            if folder.get("Path")
        }
        _, users_by_identity, users_by_group = self._build_local_identity_maps()
        folder_policy_cache = {}
        policies = self._fetch_report_policies_for_auth(
            auth,
            report_data,
            folder_by_server_path=folder_by_server_path,
            folder_policy_cache=folder_policy_cache,
        )

        permission_map = {}
        for policy in policies:
            for user, is_direct in self._resolve_policy_users(
                policy,
                users_by_identity,
                users_by_group,
            ):
                key = (user.id, report_ref.id)
                permission_map[key] = permission_map.get(key, False) or is_direct

        permissions_to_create = [
            UserReportPermission(
                user_id=user_id,
                report_id=local_report_id,
                is_direct=is_direct,
            )
            for (user_id, local_report_id), is_direct in permission_map.items()
        ]

        with transaction.atomic():
            UserReportPermission.objects.filter(report=report_ref).delete()
            if permissions_to_create:
                UserReportPermission.objects.bulk_create(
                    permissions_to_create,
                    batch_size=self.BULK_BATCH_SIZE,
                )

        return len(permissions_to_create)


def sync_all_user_permissions(triggered_by=None, user_for_auth=None, password_for_auth=None):
    """
    Convenience function to sync all user permissions.
    
    Args:
        triggered_by: The user who triggered the sync.
        
    Returns:
        Tuple of (users_synced, permissions_created)
    """
    service = PermissionSyncService(triggered_by=triggered_by)
    # Run the full sync
    users_synced, permissions_count = service.sync_all_permissions_with_service_account(
        user_for_auth=user_for_auth, 
        password_for_auth=password_for_auth
    )
    
    return users_synced, permissions_count


def sync_report_refs(triggered_by=None):
    """
    Convenience function to sync ReportRef metadata only.
    """
    service = PermissionSyncService(triggered_by=triggered_by)
    return service.sync_reports_only()


def sync_report_permissions(report_id, triggered_by=None):
    """
    Convenience function to sync local permissions for one PBIRS report.
    """
    service = PermissionSyncService(triggered_by=triggered_by)
    return service.sync_permissions_for_report_id(report_id)


def sync_user_permissions_on_login(user, password):
    """
    Convenience function to sync a user's permissions on login.
    
    Args:
        user: CustomUser instance.
        password: User's LDAP password.
        
    Returns:
        Number of permissions synced.
    """
    service = PermissionSyncService()
    return service.sync_permissions_on_login(user, password)
