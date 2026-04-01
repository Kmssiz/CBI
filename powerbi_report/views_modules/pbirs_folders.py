"""PBIRS folder management view helpers."""

import logging
from collections.abc import Callable

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render

from notifications.models import Notification
from users.models import CustomUser

logger = logging.getLogger("powerbi_report")


def report_folders_list_view(
    request,
    folder_path: str,
    local_reports_getter: Callable,
    local_folders_getter: Callable,
    permissions_getter: Callable,
):
    """Render folder/report list for the selected PBIRS path."""
    reports = local_reports_getter(request.user, request=request)
    folders = local_folders_getter(reports)
    folder_dict: dict[str, dict] = {}
    report_dict: dict[str, dict] = {}

    for report in reports:
        path = report.get("Path", "").strip("/")
        if folder_path:
            if path.startswith(folder_path + "/") and path.count("/") == folder_path.count("/") + 1:
                report_name = path.split("/")[-1]
                report_dict[report_name] = {
                    "url": report.get("embed_url"),
                    "type": "report",
                }
        else:
            if "/" not in path:
                report_name = path
                report_dict[report_name] = {
                    "url": report.get("embed_url"),
                    "type": "report",
                }

    for folder in folders:
        path = folder.get("Path", "").strip("/")
        if folder_path:
            if path.startswith(folder_path + "/") and path.count("/") == folder_path.count("/") + 1:
                folder_name = path.split("/")[-1]
                folder_id = folder.get("Id", f"folder_{path}")
                folder_dict[folder_name] = {"id": folder_id, "type": "folder"}
        else:
            if "/" not in path:
                folder_name = path
                folder_id = folder.get("Id", f"folder_{path}")
                folder_dict[folder_name] = {"id": folder_id, "type": "folder"}

    current_folder_structure = {**folder_dict, **report_dict}

    breadcrumbs = []
    if folder_path:
        parts = folder_path.strip("/").split("/")
        for idx, part in enumerate(parts):
            breadcrumbs.append({"name": part, "url": "/".join(parts[: idx + 1])})

    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread = notifications.filter(is_read=False).count()
    permissions = permissions_getter(request.user)

    return render(
        request,
        "powerbi_report/report_folders_list.html",
        {
            "folder_structure": current_folder_structure,
            "breadcrumbs": breadcrumbs,
            "notifications": notifications,
            "unread": unread,
            "permissions": permissions,
            "current_folder": folder_path,
        },
    )


def add_powerbi_folder_view(request, auth_getter: Callable, history_logger: Callable):
    """Create PBIRS folder and notify admins."""
    if request.method == "POST":
        folder_name = request.POST.get("folder_name", "").strip()
        parent_folder = request.POST.get("parent_folder", "").strip()

        if not folder_name:
            messages.error(request, "Folder name is required.")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders"  # Uses primary server
        auth = auth_getter(request)
        session = requests.Session()
        session.auth = auth

        payload = {
            "Name": folder_name,
            "Path": f"{parent_folder}/{folder_name}" if parent_folder else f"/{folder_name}",
        }

        try:
            response = session.post(url, json=payload)
            response.raise_for_status()
            history_logger(request.user, f"Dossier '{folder_name}' crÃ©Ã©")

            admin_users = CustomUser.objects.filter(is_superuser=True)
            folder_path = payload["Path"]
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=(
                        f"Un nouveau dossier '{folder_name}' a Ã©tÃ© crÃ©Ã© dans "
                        f"'{folder_path}' par {request.user.username}."
                    ),
                )

            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.HTTPError as exc:
            messages.error(request, f"Failed to create folder due to HTTP error: {exc}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.RequestException as exc:
            messages.error(request, f"Failed to create folder due to request error: {exc}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

    return render(request, "powerbi_report/report_folders_list.html")


def delete_powerbi_folder_view(
    request,
    folder_id: str,
    auth_getter: Callable,
    history_logger: Callable,
):
    """Delete PBIRS folder and notify admins."""
    if request.method == "POST":
        url = f"{settings.POWERBI_REPORT_SERVER_URL}/Reports/api/v2.0/Folders({folder_id})"
        auth = auth_getter(request)
        session = requests.Session()
        session.auth = auth

        try:
            response = session.delete(url)
            response.raise_for_status()
            history_logger(request.user, f"Dossier avec ID '{folder_id}' supprimÃ©")

            admin_users = CustomUser.objects.filter(is_superuser=True)
            for admin in admin_users:
                Notification.objects.create(
                    user=admin,
                    message=f"Le dossier (ID : '{folder_id}') a Ã©tÃ© supprimÃ© par {request.user.username}.",
                )

            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.HTTPError as exc:
            messages.error(request, f"Failed to delete folder due to HTTP error: {exc}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
        except requests.exceptions.RequestException as exc:
            messages.error(request, f"Failed to delete folder due to request error: {exc}")
            return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))

    messages.error(request, "Invalid request method. Please use POST to delete a folder.")
    return redirect(request.META.get("HTTP_REFERER", "report_folders_list"))
