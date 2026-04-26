"""
Power BI Report Server (PBIRS) Client Service.

This module provides a centralized client for all PBIRS API interactions,
handling authentication, error handling, and caching.
"""

import logging
import urllib.parse
from typing import Optional

import requests
from requests_ntlm import HttpNtlmAuth
from django.conf import settings
from django.core.cache import cache
from .pbirs_servers import get_primary_pbirs_server_url

logger = logging.getLogger('powerbi_report')


class PBIRSClientError(Exception):
    """Base exception for PBIRS client errors."""
    pass


class PBIRSAuthenticationError(PBIRSClientError):
    """Raised when PBIRS authentication fails."""
    pass


class PBIRSConnectionError(PBIRSClientError):
    """Raised when PBIRS server is unreachable."""
    pass


class PBIRSClient:
    """
    Client for interacting with Power BI Report Server API.
    
    This class centralizes all PBIRS API calls with proper authentication,
    error handling, timeouts, and caching.
    
    Usage:
        client = PBIRSClient(request)
        reports = client.get_reports()
    """
    
    DEFAULT_TIMEOUT = 15  # seconds
    CACHE_TIMEOUT = 200  # seconds for reports cache
    
    def __init__(self, request):
        """
        Initialize the PBIRS client with the current request context.
        
        Args:
            request: Django HttpRequest object containing user and session.
        """
        self.request = request
        self.user = request.user
        self.base_url = get_primary_pbirs_server_url()
        self._auth = None
        self._session = None
    
    @property
    def auth(self) -> Optional[HttpNtlmAuth]:
        """
        Get NTLM authentication object for the current user.
        
        The password is stored in the session (not DB) for PBIRS NTLM auth.
        This is required because PBIRS uses Windows Integrated Authentication.
        """
        if self._auth is None:
            password = self.request.session.get('ldap_password')
            if not password:
                logger.warning(f"LDAP password not found in session for user {self.user.username}")
                return None
            
            # NTLM requires DOMAIN\\username format
            ntlm_username = f"{settings.LDAP_DOMAIN}\\{self.user.username}"
            self._auth = HttpNtlmAuth(ntlm_username, password)
        return self._auth
    
    @property
    def session(self) -> requests.Session:
        """Get or create a requests session with authentication."""
        if self._session is None:
            self._session = requests.Session()
            self._session.auth = self.auth
        return self._session
    
    def _get_cache_key(self, operation: str, identifier: str = "") -> str:
        """Generate a cache key for the given operation."""
        user_id = self.user.id if self.user.is_authenticated else "anon"
        return f"pbirs_{operation}_{user_id}_{identifier}"
    
    def _make_request(
        self,
        method: str,
        endpoint: str,
        json_data: dict = None,
        timeout: int = None,
        raise_on_error: bool = True
    ) -> Optional[requests.Response]:
        """
        Make an HTTP request to the PBIRS API.
        
        Args:
            method: HTTP method (GET, POST, PUT, DELETE, PATCH).
            endpoint: API endpoint (will be appended to base URL).
            json_data: Optional JSON payload for POST/PUT/PATCH.
            timeout: Request timeout in seconds.
            raise_on_error: Whether to raise exceptions on HTTP errors.
            
        Returns:
            Response object or None if request fails and raise_on_error is False.
            
        Raises:
            PBIRSAuthenticationError: If authentication is not available.
            PBIRSConnectionError: If server is unreachable.
            PBIRSClientError: For other request errors.
        """
        if self.auth is None:
            if raise_on_error:
                raise PBIRSAuthenticationError("No authentication available. Session may have expired.")
            return None
        
        url = f"{self.base_url}{endpoint}"
        timeout = timeout or self.DEFAULT_TIMEOUT
        
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                json=json_data,
                headers=headers,
                timeout=timeout
            )
            
            if raise_on_error:
                response.raise_for_status()
            
            return response
            
        except requests.exceptions.Timeout:
            logger.error(f"PBIRS request timed out: {method} {endpoint}")
            if raise_on_error:
                raise PBIRSConnectionError(f"Request to PBIRS timed out after {timeout}s")
            return None
            
        except requests.exceptions.ConnectionError as e:
            logger.error(f"PBIRS connection error: {e}")
            if raise_on_error:
                raise PBIRSConnectionError(f"Cannot connect to PBIRS server: {e}")
            return None
            
        except requests.exceptions.HTTPError as e:
            logger.error(f"PBIRS HTTP error: {e}")
            if raise_on_error:
                raise PBIRSClientError(f"PBIRS API error: {e}")
            return None
            
        except requests.exceptions.RequestException as e:
            logger.error(f"PBIRS request error: {e}")
            if raise_on_error:
                raise PBIRSClientError(f"PBIRS request failed: {e}")
            return None
    
    # =========================================================================
    # Reports API
    # =========================================================================
    
    def get_reports(self, use_cache: bool = True) -> list[dict]:
        """
        Fetch all Power BI reports from PBIRS.
        
        Args:
            use_cache: Whether to use cached results.
            
        Returns:
            List of report dictionaries.
        """
        cache_key = self._get_cache_key("reports")
        
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Returning cached reports for user {self.user.id}")
                return cached
        
        response = self._make_request("GET", "/Reports/api/v2.0/PowerBIReports", raise_on_error=False)
        
        if response is None or response.status_code != 200:
            logger.warning(f"Failed to fetch reports, returning empty list")
            return []
        
        reports = response.json().get('value', [])
        cache.set(cache_key, reports, timeout=self.CACHE_TIMEOUT)
        
        logger.info(f"Fetched {len(reports)} reports from PBIRS")
        return reports
    
    def get_report_by_id(self, report_id: str) -> Optional[dict]:
        """
        Fetch a specific report by ID.
        
        Args:
            report_id: The PBIRS report UUID.
            
        Returns:
            Report dictionary or None if not found.
        """
        response = self._make_request(
            "GET",
            f"/Reports/api/v2.0/PowerBIReports({report_id})",
            raise_on_error=False
        )
        
        if response and response.status_code == 200:
            return response.json()
        return None
    
    def get_report_info(self, report_id: str) -> Optional[dict]:
        """
        Get basic report info (name and path).
        
        Args:
            report_id: The PBIRS report UUID.
            
        Returns:
            Dict with 'name' and 'path' keys, or None.
        """
        data = self.get_report_by_id(report_id)
        if data:
            return {"name": data.get("Name"), "path": data.get("Path")}
        return None
    
    def update_report(self, report_id: str, updates: dict) -> bool:
        """
        Update report properties (name, description, path).
        
        Args:
            report_id: The PBIRS report UUID.
            updates: Dictionary of properties to update.
            
        Returns:
            True if successful, False otherwise.
        """
        response = self._make_request(
            "PATCH",
            f"/Reports/api/v2.0/PowerBIReports({report_id})",
            json_data=updates,
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            # Invalidate cache
            cache.delete(self._get_cache_key("reports"))
            logger.info(f"Updated report {report_id}: {updates}")
            return True
        return False
    
    def delete_report(self, report_id: str) -> bool:
        """
        Delete a report from PBIRS.
        
        Args:
            report_id: The PBIRS report UUID.
            
        Returns:
            True if successful, False otherwise.
        """
        response = self._make_request(
            "DELETE",
            f"/Reports/api/v2.0/PowerBIReports({report_id})",
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            cache.delete(self._get_cache_key("reports"))
            logger.info(f"Deleted report {report_id}")
            return True
        return False
    
    def upload_report(self, report_path: str, file_content: bytes) -> bool:
        """
        Upload a PBIX file to PBIRS.
        
        Args:
            report_path: Full path for the report (e.g., /Sales/Q1Report).
            file_content: The PBIX file content as bytes.
            
        Returns:
            True if successful, False otherwise.
        """
        encoded_path = report_path.replace("'", "''").replace(" ", "%20")
        endpoint = f"/Reports/api/v2.0/PowerBIReports(path='{encoded_path}')/Model.Upload"
        
        # Upload requires multipart, not JSON
        if self.auth is None:
            return False
        
        url = f"{self.base_url}{endpoint}"
        files = {'file': ('report.pbix', file_content, 'application/octet-stream')}
        
        try:
            response = self.session.post(url, files=files, timeout=60)
            response.raise_for_status()
            cache.delete(self._get_cache_key("reports"))
            logger.info(f"Uploaded report to {report_path}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to upload report to {report_path}: {e}")
            return False
    
    # =========================================================================
    # Permissions API
    # =========================================================================
    
    def get_report_permissions(self, report_id: str) -> list[dict]:
        """
        Get permission policies for a report.
        
        Args:
            report_id: The PBIRS report UUID.
            
        Returns:
            List of policy dictionaries.
        """
        response = self._make_request(
            "GET",
            f"/Reports/api/v2.0/PowerBIReports({report_id})/Policies",
            raise_on_error=False
        )
        
        if response and response.status_code == 200:
            return response.json().get('Policies', [])
        return []
    
    def update_report_permissions(self, report_id: str, policies: list[dict]) -> bool:
        """
        Update permission policies for a report.
        
        Args:
            report_id: The PBIRS report UUID.
            policies: List of policy dictionaries.
            
        Returns:
            True if successful, False otherwise.
        """
        payload = {"Id": report_id, "Policies": policies}
        response = self._make_request(
            "PUT",
            f"/Reports/api/v2.0/PowerBIReports({report_id})/Policies",
            json_data=payload,
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            logger.info(f"Updated permissions for report {report_id}")
            return True
        return False
    
    def get_folder_permissions(self, folder_id: str) -> list[dict]:
        """
        Get permission policies for a folder.
        
        Args:
            folder_id: The PBIRS folder UUID.
            
        Returns:
            List of policy dictionaries.
        """
        response = self._make_request(
            "GET",
            f"/Reports/api/v2.0/Folders({folder_id})/Policies",
            raise_on_error=False
        )
        
        if response and response.status_code == 200:
            return response.json().get('Policies', [])
        return []
    
    # =========================================================================
    # Folders API
    # =========================================================================
    
    def get_folders(self) -> list[dict]:
        """
        Fetch all folders from PBIRS.
        
        Returns:
            List of folder dictionaries.
        """
        response = self._make_request("GET", "/Reports/api/v2.0/Folders", raise_on_error=False)
        
        if response and response.status_code == 200:
            return response.json().get('value', [])
        return []
    
    def create_folder(self, name: str, parent_path: str = "") -> bool:
        """
        Create a new folder in PBIRS.
        
        Args:
            name: Folder name.
            parent_path: Parent folder path (empty for root).
            
        Returns:
            True if successful, False otherwise.
        """
        path = f"{parent_path}/{name}" if parent_path else f"/{name}"
        payload = {"Name": name, "Path": path}
        
        response = self._make_request(
            "POST",
            "/Reports/api/v2.0/Folders",
            json_data=payload,
            raise_on_error=False
        )
        
        if response and response.status_code == 201:
            logger.info(f"Created folder: {path}")
            return True
        return False
    
    def delete_folder(self, folder_id: str) -> bool:
        """
        Delete a folder from PBIRS.
        
        Args:
            folder_id: The PBIRS folder UUID.
            
        Returns:
            True if successful, False otherwise.
        """
        response = self._make_request(
            "DELETE",
            f"/Reports/api/v2.0/Folders({folder_id})",
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            logger.info(f"Deleted folder {folder_id}")
            return True
        return False
    
    # =========================================================================
    # Cache Refresh Plans API
    # =========================================================================
    
    def get_refresh_plans(self, report_id: str) -> list[dict]:
        """
        Get cache refresh plans for a report.
        
        Args:
            report_id: The PBIRS report UUID.
            
        Returns:
            List of refresh plan dictionaries.
        """
        response = self._make_request(
            "GET",
            f"/Reports/api/v2.0/PowerBIReports({report_id})/CacheRefreshPlans",
            raise_on_error=False
        )
        
        if response and response.status_code == 200:
            return response.json().get('value', [])
        return []
    
    def execute_refresh_plan(self, plan_id: str) -> bool:
        """
        Execute a cache refresh plan.
        
        Args:
            plan_id: The refresh plan UUID.
            
        Returns:
            True if successful, False otherwise.
        """
        response = self._make_request(
            "POST",
            f"/Reports/api/v2.0/CacheRefreshPlans({plan_id})/Model.Execute",
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            logger.info(f"Executed refresh plan {plan_id}")
            return True
        return False
    
    def delete_refresh_plan(self, plan_id: str) -> bool:
        """
        Delete a cache refresh plan.
        
        Args:
            plan_id: The refresh plan UUID.
            
        Returns:
            True if successful, False otherwise.
        """
        response = self._make_request(
            "DELETE",
            f"/Reports/api/v2.0/CacheRefreshPlans({plan_id})",
            raise_on_error=False
        )
        
        if response and response.status_code in (200, 204):
            logger.info(f"Deleted refresh plan {plan_id}")
            return True
        return False
    
    def create_refresh_plan(self, report_path: str, description: str, schedule: dict) -> bool:
        """
        Create a new cache refresh plan.
        
        Args:
            report_path: Path to the report.
            description: Plan description.
            schedule: Schedule definition dictionary.
            
        Returns:
            True if successful, False otherwise.
        """
        payload = {
            "Owner": None,
            "Description": description,
            "CatalogItemPath": report_path,
            "EventType": "DataModelRefresh",
            "Schedule": schedule,
            "ScheduleDescription": "",
            "ParameterValues": []
        }
        
        response = self._make_request(
            "POST",
            "/Reports/api/v2.0/CacheRefreshPlans",
            json_data=payload,
            raise_on_error=False
        )
        
        if response and response.status_code == 201:
            logger.info(f"Created refresh plan for {report_path}")
            return True
        return False
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def build_embed_url(self, report_path: str) -> str:
        """
        Build an embed URL for a report.
        
        Args:
            report_path: The report path from PBIRS.
            
        Returns:
            Full embed URL.
        """
        clean_path = report_path.strip('/')
        encoded_path = urllib.parse.quote(clean_path, safe='/')
        return f"{self.base_url}/Reports/powerbi/{encoded_path}?rs:embed=true"
    
    def clear_reports_cache(self):
        """Clear the reports cache for the current user."""
        cache.delete(self._get_cache_key("reports"))
        logger.debug(f"Cleared reports cache for user {self.user.id}")
