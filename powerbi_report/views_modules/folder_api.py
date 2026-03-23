"""Folder API view helpers."""

import logging
from collections.abc import Callable

import requests
from django.conf import settings
from django.http import JsonResponse

logger = logging.getLogger("powerbi_report")


def get_folders_response(request, auth_getter: Callable) -> JsonResponse:
    """Return PBIRS folder paths for jsTree."""
    url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
    auth = auth_getter(request)
    try:
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()
        folders = response.json().get("value", [])
        folder_paths = [folder["Path"] for folder in folders if folder.get("Path")]
        return JsonResponse({"folders": folder_paths}, status=200)
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch PBIRS folders: %s", exc)
        return JsonResponse({"error": f"Failed to fetch folders: {exc}"}, status=500)


def get_folder_list_response(request, auth_getter: Callable) -> JsonResponse:
    """Return PBIRS folder paths as JSON."""
    try:
        auth = auth_getter(request)
        if not auth:
            return JsonResponse({"error": "Authentication failed"}, status=401)

        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()
        folders = response.json().get("value", [])
        folder_paths = [folder["Path"] for folder in folders if folder.get("Path")]
        logger.debug(
            "Fetched %s folders for user %s.",
            len(folder_paths),
            request.user.username,
        )
        return JsonResponse({"folders": folder_paths}, status=200)
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch folder list: %s", exc)
        return JsonResponse({"error": f"Failed to fetch folders: {exc}"}, status=500)
    except Exception as exc:
        logger.error("Unexpected folder list error: %s", exc)
        return JsonResponse({"error": f"An unexpected error occurred: {exc}"}, status=500)
