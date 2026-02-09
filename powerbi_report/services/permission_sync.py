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
            List of report dictionaries.
        """
        url = f"{self.base_url}/Reports/api/v2.0/CatalogItems"
        
        """Fetch all reports using provided auth."""
        try:
            url = f"{self.base_url}/Reports/api/v2.0/CatalogItems"
            logger.info(f"Fetching reports from: {url}")
            response = requests.get(url, auth=auth, timeout=30)
            logger.info(f"Response status: {response.status_code}")
            response.raise_for_status()
            
            items = response.json().get('value', [])
            return [item for item in items if item.get('Type') == 'PowerBIReport']
            # Filter for PowerBIReport type only
            reports = [item for item in items if item.get('Type') == 'PowerBIReport']
            return reports
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
            # We need to know if it's direct or not. 
            # Since we only have the report list from the API here (which doesn't include policies), 
            # we can't determine is_direct without an extra call per report.
            # However, for performance, we might assume False (Group) in this bulk sync scenario if we can't check.
            # BUT, sync_permissions_for_user_with_password uses the user's OWN credentials.
            # If the user can see the report, they have access. 
            # It's safer to default to False (Group/Unknown) if we're not sure, 
            # OR fetch policies if we want accuracy (slower).
            
            # Let's try to fetch policies for each accessible report to be accurate.
            # This will slow down login significantly if there are many reports.
            # Alternative: Assume False (Group) for now, and let the UI view update it on demand?
            # Or, better: default to True (Direct) only if we can prove it?
            
            # Since this function is used on login, performance is key. 
            # Maybe we skip is_direct here and let the view handle it?
            # No, the view trusts the DB.
            
            # Let's check policies for just the reports they can see.
            is_direct = False
            try:
                # Fetch policies for this specific report to check for direct assignment
                # We use the user's auth for this check
                policy_url = f"{self.base_url}/Reports/api/v2.0/PowerBIReports({report.pbirs_id})/Policies"
                policy_response = requests.get(policy_url, auth=auth, timeout=5)
                if policy_response.status_code == 200:
                    policies = policy_response.json().get('Policies', [])
                    current_username = user.username.lower()
                    for policy in policies:
                        policy_user = (policy.get("UserName") or "").split("\\")[-1].lower()
                        if policy_user == current_username:
                            is_direct = True
                            break
            except Exception as e:
                logger.warning(f"Failed to fetch policies for report {report.pbirs_id} during sync: {e}")

            UserReportPermission.objects.create(user=user, report=report, is_direct=is_direct)
            permissions_created += 1
        
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
