"""Power BI report metadata view helpers."""

import logging
from users.utils import person_label
from collections.abc import Callable

import requests
from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import redirect
from django.utils import timezone

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
        response = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=30)
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
    history_logger: Callable,
):
    """
    Rename a report in the app only. The name in PBIRS is never touched: the new name is stored in the
    platform's database and flagged (name_locked) so the hourly PBIRS sync does not put the old one back.
    """
    if request.method != "POST":
        return redirect("powerbi_report:report_detail", report_id=report_id)

    new_name = " ".join((request.POST.get("name") or "").split())
    if not new_name:
        messages.error(request, "Veuillez saisir un nouveau nom pour le rapport.")
        return redirect("powerbi_report:report_detail", report_id=report_id)
    max_length = ReportRef._meta.get_field("name").max_length
    if len(new_name) > max_length:
        messages.error(request, f"Le nom du rapport ne peut pas dépasser {max_length} caractères.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    report_ref = ReportRef.objects.filter(pbirs_id=report_id).first()
    if report_ref is None:
        messages.error(request, "Rapport introuvable dans la base de données locale.")
        return redirect("powerbi_report:report_list")

    old_name = report_ref.name
    if new_name == old_name and report_ref.name_locked:
        messages.info(request, "Le nom du rapport est inchangé.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    report_ref.name = new_name
    report_ref.name_locked = True
    report_ref.modified_by = request.user
    report_ref.modified_at = timezone.now()
    report_ref.save(update_fields=["name", "name_locked", "modified_by", "modified_at", "last_synced"])
    logger.info("[EDIT NAME] %s renamed report %s in the app only: %r -> %r",
                request.user.username, report_id, old_name, new_name)
    messages.success(request, "Nom du rapport mis à jour dans CBI (le nom sur le serveur Power BI n'est pas modifié).")
    history_logger(request.user, f"Nom du rapport (dans CBI) mis à jour à '{new_name}' pour l'ID '{report_id}'")

    admin_users = CustomUser.objects.filter(is_superuser=True)
    for admin in admin_users:
        Notification.objects.create(
            user=admin,
            message=f"Le rapport '{old_name}' a été renommé '{new_name}' dans CBI par {person_label(request.user)}.",
        )

    _clear_user_report_cache(request)
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
        response = requests.patch(update_url, json=data, auth=auth, headers=headers, timeout=30)
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
        response = requests.patch(update_url, json=data, auth=auth, headers=headers, timeout=30)
        response.raise_for_status()
        _clear_user_report_cache(request)
        messages.success(request, "Report description updated successfully!")
        metadata_updater(report_id, request.user, description=new_description)

        report_name = info.get("name") if info else report_id
        report_path = info.get("path") if info else ""
        history_logger(
            request.user,
            (
                "Description mise à jour pour le rapport "
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
                    f"avec la description '{new_description}' par {person_label(request.user)}."
                ),
            )
    except requests.exceptions.RequestException as exc:
        messages.error(request, f"Failed to update report description: {exc}")

    return redirect("powerbi_report:report_detail", report_id=report_id)
