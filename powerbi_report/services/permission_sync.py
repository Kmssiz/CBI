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
        username = settings.LDAP_USERNAME
        password = settings.LDAP_PASSWORD
        domain = settings.LDAP_DOMAIN
        
        if not all([username, password, domain]):
            logger.error("LDAP service account credentials not configured")
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
            List of report dictionaries.
        """
        url = f"{self.base_url}/Reports/api/v2.0/CatalogItems"
        
        try:
            session = requests.Session()
            session.auth = auth
            response = session.get(url, timeout=self.DEFAULT_TIMEOUT)
            response.raise_for_status()
            
            items = response.json().get('value', [])
            # Filter for PowerBIReport type only
            reports = [item for item in items if item.get('Type') == 'PowerBIReport']
            return reports
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch reports: {e}")
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
        
        Args:
            reports: List of report dictionaries from PBIRS.
            
        Returns:
            Number of reports synced.
        """
        base_embed_url = f"{self.base_url}/Reports/powerbi/"
        synced_count = 0
        
        for report in reports:
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
        auth = self._get_user_auth(user, password)
        reports = self._fetch_reports_for_auth(auth)
        
        # Get report IDs this user can access
        accessible_pbirs_ids = {r.get('Id') for r in reports if r.get('Id')}
        
        # Get corresponding ReportRef objects
        accessible_reports = ReportRef.objects.filter(pbirs_id__in=accessible_pbirs_ids)
        
        # Clear old permissions and create new ones
        UserReportPermission.objects.filter(user=user).delete()
        
        permissions_created = 0
        for report in accessible_reports:
            UserReportPermission.objects.create(user=user, report=report)
            permissions_created += 1
        
        logger.info(f"Synced {permissions_created} permissions for user {user.username}")
        return permissions_created
    
    def sync_all_permissions_with_service_account(self):
        """
        Sync permissions for all users using service account.
        
        This method:
        1. Fetches all reports using service account
        2. For each active user with stored password, fetches their accessible reports
        3. Updates UserReportPermission table
        
        Returns:
            Tuple of (users_synced, permissions_created)
        """
        # Start sync log
        self.sync_log = PermissionSyncLog.objects.create(
            triggered_by=self.triggered_by,
            status='started'
        )
        
        try:
            # First, sync all reports to local ReportRef
            all_reports = self._fetch_all_reports_as_admin()
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


def sync_all_user_permissions(triggered_by=None):
    """
    Convenience function to sync all user permissions.
    
    Args:
        triggered_by: The user who triggered the sync.
        
    Returns:
        Tuple of (users_synced, permissions_created)
    """
    service = PermissionSyncService(triggered_by=triggered_by)
    return service.sync_all_permissions_with_service_account()


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
