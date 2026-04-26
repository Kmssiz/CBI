"""Refresh-plan API helpers extracted from the monolithic views module."""

import logging
from collections.abc import Callable

import requests
from django.http import JsonResponse

from powerbi_report.models import ReportRef
from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url

logger = logging.getLogger("powerbi_report")


def get_refresh_plans_data(report_id: str, request, auth_getter: Callable) -> list[dict]:
    """Fetch cache refresh plans for a report."""
    url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})/CacheRefreshPlans"
    auth = auth_getter(request)

    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        data = response.json()
        logger.debug(
            "Fetched %s refresh plans for report %s.",
            len(data.get("value", [])),
            report_id,
        )
        return data.get("value", [])
    except requests.exceptions.RequestException as exc:
        logger.error("Error fetching cache refresh plans for report %s: %s", report_id, exc)
        return []


def get_shared_schedules_data(request, auth_getter: Callable) -> list[dict]:
    """Fetch shared PBIRS schedules."""
    base_url = get_primary_pbirs_server_url()
    if not base_url:
        return []
    url = f"{base_url}/Reports/api/v2.0/Schedules"
    auth = auth_getter(request)

    try:
        response = requests.get(url, auth=auth)
        response.raise_for_status()
        data = response.json()
        return data.get("value", [])
    except requests.exceptions.RequestException as exc:
        logger.error("Error fetching shared schedules: %s", exc)
        return []


def get_refresh_plan_history_response(request, plan_id: str, auth_getter: Callable) -> JsonResponse:
    """Fetch execution history for a refresh plan."""
    base_url = get_primary_pbirs_server_url()
    if not base_url:
        return JsonResponse({"error": "No active PBIRS server configured."}, status=503)
    url = f"{base_url}/Reports/api/v2.0/CacheRefreshPlans({plan_id})/History"
    auth = auth_getter(request)

    try:
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()
        data = response.json()
        return JsonResponse(data, safe=False)
    except requests.exceptions.RequestException as exc:
        logger.error("Error fetching refresh plan history for plan %s: %s", plan_id, exc)
        return JsonResponse({"error": str(exc)}, status=500)
