"""Folder API view helpers."""

import logging
from collections.abc import Callable

import requests
from django.http import JsonResponse
from powerbi_report.services.pbirs_servers import get_active_pbirs_server_urls

logger = logging.getLogger("powerbi_report")


def _fetch_folders_from_server(auth, server_url: str) -> list[str]:
    """Fetch folder paths from a single PBIRS server."""
    try:
        url = f"{server_url}/Reports/api/v2.0/Folders"
        response = requests.get(url, auth=auth, timeout=10)
        response.raise_for_status()
        folders = response.json().get("value", [])
        return [folder["Path"] for folder in folders if folder.get("Path")]
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to fetch PBIRS folders from %s: %s", server_url, exc)
        return []


def get_folders_response(request, auth_getter: Callable) -> JsonResponse:
    """Return PBIRS folder paths for jsTree (aggregated from all servers)."""
    auth = auth_getter(request)
    server_urls = get_active_pbirs_server_urls()

    all_folder_paths = []
    for server_url in server_urls:
        all_folder_paths.extend(_fetch_folders_from_server(auth, server_url))

    # Deduplicate paths (same folder name can exist on different servers)
    unique_paths = sorted(set(all_folder_paths))
    return JsonResponse({"folders": unique_paths}, status=200)


def get_folder_list_response(request, auth_getter: Callable) -> JsonResponse:
    """Return PBIRS folder paths as JSON (aggregated from all servers)."""
    try:
        auth = auth_getter(request)
        if not auth:
            return JsonResponse({"error": "Authentication failed"}, status=401)

        server_urls = get_active_pbirs_server_urls()

        all_folder_paths = []
        for server_url in server_urls:
            all_folder_paths.extend(_fetch_folders_from_server(auth, server_url))

        unique_paths = sorted(set(all_folder_paths))
        logger.debug(
            "Fetched %s folders for user %s from %s servers.",
            len(unique_paths),
            request.user.username,
            len(server_urls),
        )
        return JsonResponse({"folders": unique_paths}, status=200)
    except Exception as exc:
        logger.error("Unexpected folder list error: %s", exc)
        return JsonResponse({"error": f"An unexpected error occurred: {exc}"}, status=500)
