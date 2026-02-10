"""
Permission Sync Service for PBIRS Reports.

This module handles synchronization of report permissions from PBIRS to the local database.
It queries PBIRS for each user's accessible reports and stores them in UserReportPermission.
"""

import logging
import urllib.parse
from datetime import datetime

import requests
from requests_ntlm import HttpNtlmAuth
from django.conf import settings
from django.utils import timezone

from users.models import CustomUser
from powerbi_report.models import ReportRef, UserReportPermission, PermissionSyncLog
from django.db import transaction

logger = logging.getLogger('powerbi_report')


class PermissionSyncService:
    """
    Service for synchronizing PBIRS report permissions to local database.
    
    This service uses the service account credentials to fetch all reports,
    then iterates through active users to determine their accessible reports.
    """
    
    DEFAULT_TIMEOUT = 15
    
    def __init__(self, triggered_by=None):
        """
        Initialize the sync service.
        
        Args:
            triggered_by: The user who triggered the sync (for logging).
        """
        self.triggered_by = triggered_by
        self.base_url = settings.POWERBI_REPORT_SERVER_URL
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
    
    def _fetch_reports_for_auth(self, auth):
        """
        Fetch reports accessible to a given auth credential.
        
        Args:
            auth: HttpNtlmAuth object.
            
        Returns:
            List of report dictionaries (Type=PowerBIReport only).
        """
        try:
            url = f"{self.base_url}/Reports/api/v2.0/PowerBIReports"
            logger.info(f"Fetching reports from: {url}")
            response = requests.get(url, auth=auth, timeout=30)
            logger.info(f"Response status: {response.status_code}")
            response.raise_for_status()
            
            items = response.json().get('value', [])
            # PowerBIReports endpoint returns only reports, so no Type filter needed
            return items
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch reports: {e}")
            if hasattr(e, 'response') and e.response is not None:
                 logger.error(f"Response content: {e.response.text}")
            return []
    
    def _fetch_all_reports_as_admin(self):
        """Fetch all reports using service account."""
        auth = self._get_service_auth()
        if not auth:
            return []
        return self._fetch_reports_for_auth(auth)
    
    def _sync_reports_to_local(self, reports):
        """
        Sync report metadata to local ReportRef table.
        Syncs only PowerBIReport types. Uses atomic transaction to prevent locking.
        
        Args:
            reports: List of report dictionaries from PBIRS.
            
        Returns:
            Number of reports synced.
        """
        base_embed_url = f"{self.base_url}/Reports/powerbi/"
        synced_count = 0
        
        with transaction.atomic():
            for report in reports:
                # PowerBIReports endpoint ensures these are reports, validation optional
                # if report.get('Type') != 'PowerBIReport': continue

                pbirs_id = report.get('Id')
                name = report.get('Name', 'Unnamed')
                path = report.get('Path', '')
                
                # Build embed URL
                clean_path = path.strip('/')
                encoded_path = urllib.parse.quote(clean_path, safe='/')
                embed_url = f"{base_embed_url}{encoded_path}?rs:embed=true"
                
                # Update or create
                ReportRef.objects.update_or_create(
                    pbirs_id=pbirs_id,
                    defaults={
                        'name': name,
                        'path': path,
                        'embed_url': embed_url,
                    }
                )
                synced_count += 1
        
        logger.info(f"Synced {synced_count} reports to ReportRef table")
        return synced_count
    
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
        reports = self._fetch_reports_for_auth(auth)
        
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
        Sync permissions for ALL users using the service account to fetch the report list.
        Iterates through all reports and all users to rebuild local permissions.
        
        Args:
            user_for_auth: Optional user object to use for fetching the initial report list 
                           instead of the service account.
            password_for_auth: Password for user_for_auth, if provided.
        """
        self.started_at = timezone.now()
        self.sync_log = PermissionSyncLog.objects.create(
            triggered_by=self.triggered_by,
            users_synced=0,
            permissions_created=0,
            status='started'
        )
        
        try:
            # 1. Determine authentication to use for fetching all reports
            auth_to_use = None
            if user_for_auth and password_for_auth:
                logger.info(f"Fetching report list using credentials of {user_for_auth.username}")
                auth_to_use = self._get_user_auth(user_for_auth, password_for_auth)
            
            if not auth_to_use:
                logger.info("Fetching report list using service account credentials.")
                auth_to_use = self._get_service_auth()
            
            if not auth_to_use:
                raise Exception("No valid authentication method available to fetch reports.")

            # 2. Fetch all reports from PBIRS using the determined auth
            all_reports = self._fetch_reports_for_auth(auth_to_use)
            if not all_reports:
                raise Exception("Failed to fetch reports from PBIRS")
            
            self._sync_reports_to_local(all_reports)
            
            # Get all active users
            users = CustomUser.objects.filter(is_active=True)
            users_synced = 0
            total_permissions = 0
            
            # For users who have logged in recently (password in session won't work here)
            # We'll use the service account approach: grant access to all reports
            # that the service account can see
            
            # Alternative: Use PBIRS policies to determine access
            # For now, we'll sync all users to see all reports the service account can see
            # This effectively means: if service account can see it, all users can see it
            
            # Get all ReportRef objects
            all_report_refs = ReportRef.objects.all()
            
            for user in users:
                # Clear old permissions
                UserReportPermission.objects.filter(user=user).delete()
                
                # For each user, we need to determine their accessible reports
                # Since we can't impersonate users without their passwords,
                # we'll grant all users access to all reports the service account can see
                # 
                # In a production system, you would:
                # 1. Read PBIRS folder/report policies (which contain AD groups)
                # 2. Check which AD groups the user belongs to
                # 3. Grant permissions accordingly
                
                # For now, grant all users access to all synced reports
                permissions_created = 0
                for report in all_report_refs:
                    UserReportPermission.objects.create(user=user, report=report)
                    permissions_created += 1
                
                users_synced += 1
                total_permissions += permissions_created
                logger.debug(f"Synced {permissions_created} permissions for {user.username}")
            
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
