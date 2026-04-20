"""Power BI report metadata view helpers."""

import logging
from collections.abc import Callable

import requests
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import redirect

from notifications.models import Notification
from powerbi_report.models import ReportRef
from users.models import CustomUser

logger = logging.getLogger("powerbi_report")


def get_powerbi_report_info_data(
    request,
    report_id: str,
    auth_getter: Callable,
):
    """Retrieve basic report info from PBIRS."""
    url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})"
    auth = auth_getter(request)
    if not auth:
        return None

    try:
        response = requests.get(url, auth=auth, headers={"Accept": "application/json"})
        if response.status_code == 200:
            data = response.json()
            return {
                "name": data.get("Name"),
                "path": data.get("Path"),
            }
        return None
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to retrieve report info for ID '%s': %s", report_id, exc)
        return None


def _clear_user_report_cache(request) -> None:
    """Clear known report cache keys for the current user."""
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache.delete(f"powerbi_reports_cache_{user_id}")
    cache.delete(f"powerbi_reports_cache_{user_id}_PowerBIReports")
    cache.delete(f"powerbi_reports_cache_{user_id}_CatalogItems")


def edit_powerbi_report_name_view(
    request,
    report_id: str,
    auth_getter: Callable,
    report_permissions_getter: Callable,
    metadata_updater: Callable,
    history_logger: Callable,
):
    """Update report name in PBIRS and notify admins."""
    if request.method != "POST":
        return redirect("powerbi_report:report_detail", report_id=report_id)

    new_name = request.POST.get("name")
    if not new_name:
        messages.error(request, "Please provide a new report name.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    update_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})"
    auth = auth_getter(request)
    if not auth:
        messages.error(request, "Session expiree. Veuillez vous reconnecter.")
        return redirect("login")

    data = {"Name": new_name}
    headers = {"Content-Type": "application/json"}

    try:
        logger.info(
            "[EDIT NAME] User %s attempting to rename report %s to '%s'",
            request.user.username,
            report_id,
            new_name,
        )

        try:
            policies = report_permissions_getter(request, report_id)
            logger.info(
                "[DEBUG_PERMS] Policies for report %s for user %s: %s",
                report_id,
                request.user.username,
                policies,
            )
        except Exception as exc:  # defensive: this is optional diagnostic logging
            logger.error("[DEBUG_PERMS] Failed to fetch policies: %s", exc)

        response = requests.patch(update_url, json=data, auth=auth, headers=headers)
        response.raise_for_status()
        messages.success(request, "Report name updated successfully!")
        history_logger(
            request.user,
            f"Nom du rapport mis a jour a '{new_name}' pour l'ID '{report_id}'",
        )

        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=f"Le rapport '{new_name}' a été renommé par {request.user.username}.",
            )
        logger.info(
            "Sent rename notification for report %s to %s admins.",
            report_id,
            len(admin_users),
        )

        _clear_user_report_cache(request)
        metadata_updater(report_id, request.user, name=new_name)

    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            logger.error(
                "Edit name forbidden for report %s by user %s: %s",
                report_id,
                request.user.username,
                exc.response.text,
            )
            messages.error(
                request,
                "Acces refuse. Vous n'avez pas les permissions necessaires "
                "sur le serveur de rapports pour modifier ce rapport.",
            )
        else:
            messages.error(request, f"Failed to update report name: {exc}")
            logger.error("HTTP error updating report name %s: %s", report_id, exc)
    except requests.exceptions.RequestException as exc:
        messages.error(request, f"Failed to update report name: {exc}")
        logger.error("Request error updating report name %s: %s", report_id, exc)

    return redirect("powerbi_report:report_detail", report_id=report_id)


def edit_powerbi_report_path_view(
    request,
    report_id: str,
    auth_getter: Callable,
    metadata_updater: Callable,
):
    """Update report path in PBIRS."""
    if request.method != "POST":
        return redirect("powerbi_report:report_list")

    new_path = request.POST.get("path")
    if not new_path:
        messages.error(request, "Please provide a new report path.")
        return redirect("powerbi_report:report_list")

    update_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})"
    auth = auth_getter(request)
    if not auth:
        messages.error(request, "Session expiree. Veuillez vous reconnecter.")
        return redirect("login")

    data = {"Path": new_path}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.patch(update_url, json=data, auth=auth, headers=headers)
        response.raise_for_status()
        _clear_user_report_cache(request)
        messages.success(request, "Report path updated successfully!")
        metadata_updater(report_id, request.user, path=new_path)
    except requests.exceptions.RequestException as exc:
        messages.error(request, f"Failed to update report path: {exc}")

    return redirect("powerbi_report:report_list")


def edit_powerbi_report_description_view(
    request,
    report_id: str,
    auth_getter: Callable,
    report_info_getter: Callable,
    metadata_updater: Callable,
    history_logger: Callable,
):
    """Update report description in PBIRS and notify admins."""
    if request.method != "POST":
        return redirect("powerbi_report:report_detail", report_id=report_id)

    new_description = request.POST.get("description")
    if not new_description:
        messages.error(request, "Please provide a new report description.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    update_url = f"{ReportRef.get_server_url(report_id)}/Reports/api/v2.0/PowerBIReports({report_id})"
    auth = auth_getter(request)
    if not auth:
        messages.error(request, "Session expiree. Veuillez vous reconnecter.")
        return redirect("login")

    data = {"Description": new_description}
    headers = {"Content-Type": "application/json"}
    info = report_info_getter(request, report_id)

    try:
        response = requests.patch(update_url, json=data, auth=auth, headers=headers)
        response.raise_for_status()
        _clear_user_report_cache(request)
        messages.success(request, "Report description updated successfully!")
        metadata_updater(report_id, request.user, description=new_description)

        report_name = info.get("name") if info else report_id
        report_path = info.get("path") if info else ""
        history_logger(
            request.user,
            (
                "Description mise a jour pour le rapport "
                f"{report_name} ID: {report_id} chemin {report_path} "
                f"a '{new_description}'"
            ),
        )

        # Get report name for notification
        report_info = get_powerbi_report_info_data(request, report_id, auth_getter)
        report_name = report_info.get("name") if report_info else report_id

        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            Notification.objects.create(
                user=admin,
                message=(
                    f"Description mise à jour pour le rapport '{report_name}' "
                    f"avec la description '{new_description}' par {request.user.username}."
                ),
            )
    except requests.exceptions.RequestException as exc:
        messages.error(request, f"Failed to update report description: {exc}")

    return redirect("powerbi_report:report_detail", report_id=report_id)
