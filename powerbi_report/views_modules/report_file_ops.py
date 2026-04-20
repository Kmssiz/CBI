"""Power BI report file operation view helpers."""

import logging
from collections.abc import Callable

import requests
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import StreamingHttpResponse
from django.shortcuts import redirect

from notifications.models import Notification
from powerbi_report.models import ReportRef
from users.models import CustomUser

logger = logging.getLogger("powerbi_report")


def _clear_user_report_cache(request) -> None:
    user_id = request.user.id if request.user.is_authenticated else "anonymous"
    cache.delete(f"powerbi_reports_cache_{user_id}")
    cache.delete(f"powerbi_reports_cache_{user_id}_PowerBIReports")
    cache.delete(f"powerbi_reports_cache_{user_id}_CatalogItems")


def replace_powerbi_report_view(
    request,
    report_id: str,
    auth_getter: Callable,
    report_permissions_getter: Callable,
    metadata_updater: Callable,
    history_logger: Callable,
    permissions_syncer: Callable,
):
    """Replace an existing PBIRS Power BI report with a new PBIX file."""
    if not request.user.has_perm("powerbi_report.change_powerbireport"):
        messages.error(request, "Vous n'avez pas la permission de remplacer les rapports.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    if request.method != "POST":
        return redirect("powerbi_report:report_detail", report_id=report_id)

    pbix_file = request.FILES.get("pbix_file")
    report_path = request.POST.get("report_path", "").strip()

    if not pbix_file:
        messages.error(request, "Veuillez fournir un fichier .pbix valide.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    if not report_path:
        messages.error(request, "Le chemin du rapport est manquant.")
        return redirect("powerbi_report:report_detail", report_id=report_id)

    server_url = ReportRef.get_server_url(report_id)
    api_url = (
        f"{server_url}/Reports/api/v2.0/"
        f"PowerBIReports({report_id})/Model.Upload"
    )
    auth = auth_getter(request)
    if not auth:
        messages.error(request, "Erreur d'authentification. Veuillez vous reconnecter.")
        return redirect("login")

    session = requests.Session()
    session.auth = auth
    files = {"file": (pbix_file.name, pbix_file.read(), "application/octet-stream")}
    headers = {"Accept": "application/json"}

    try:
        logger.info(
            "[REPLACE] User %s attempting to replace report %s at path %s",
            request.user.username,
            report_id,
            report_path,
        )

        try:
            policies = report_permissions_getter(request, report_id)
            logger.info(
                "[DEBUG_PERMS] Policies for report %s for user %s: %s",
                report_id,
                request.user.username,
                policies,
            )
        except Exception as exc:  # defensive: optional diagnostic log
            logger.error("[DEBUG_PERMS] Failed to fetch policies: %s", exc)

        response = session.post(api_url, headers=headers, files=files, timeout=120)
        response.raise_for_status()

        report_name = report_path.split("/")[-1]
        messages.success(request, f"Le rapport '{report_name}' a été remplacé avec succès.")
        history_logger(request.user, f"Rapport Power BI remplacé : {report_path}")

        _clear_user_report_cache(request)
        metadata_updater(report_id, request.user)

        admin_users = CustomUser.objects.filter(is_superuser=True)
        for admin in admin_users:
            if admin.id != request.user.id:
                Notification.objects.create(
                    user=admin,
                    message=f"Le rapport '{report_name}' a été remplacé par {request.user.username}.",
                )
        logger.info("Sent notifications to admin users about replacing report '%s'.", report_name)

        try:
            permissions_syncer(triggered_by=request.user)
        except Exception as exc:  # defensive: do not block user action on sync failure
            logger.error("Auto-sync permissions failed after replace: %s", exc)

    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 403:
            logger.error(
                "Replace forbidden for report %s by user %s: %s",
                report_id,
                request.user.username,
                exc.response.text,
            )
            messages.error(
                request,
                "Acces refuse. Vous n'avez pas les permissions necessaires "
                "sur le serveur de rapports pour remplacer ce rapport.",
            )
        else:
            messages.error(request, f"Echec du remplacement du rapport. Erreur HTTP: {exc}")
            logger.error("HTTP Error replacing report %s: %s", report_path, exc)
    except requests.exceptions.RequestException as exc:
        messages.error(request, f"Echec du remplacement du rapport. Erreur: {exc}")
        logger.error("Request Error replacing report %s: %s", report_path, exc)

    return redirect("powerbi_report:report_detail", report_id=report_id)


def download_report_view(
    request,
    report_id: str,
    auth_getter: Callable,
    report_info_getter: Callable,
    report_permissions_getter: Callable,
    report_server_url: str,
):
    """Download a PBIX file by report id."""
    url = f"{report_server_url}/Reports/api/v2.0/PowerBIReports({report_id})/Content/$value"
    auth = auth_getter(request)
    if not auth:
        messages.error(request, "Session expiree. Veuillez vous reconnecter.")
        return redirect("login")

    info = report_info_getter(request, report_id)
    if not info:
        messages.error(request, "Echec de la recuperation des informations du rapport.")
        return redirect(request.META.get("HTTP_REFERER", "powerbi_report:report_list"))

    try:
        logger.info(
            "[DOWNLOAD] User %s attempting to download report %s",
            request.user.username,
            report_id,
        )

        try:
            policies = report_permissions_getter(request, report_id)
            logger.info(
                "[DEBUG_PERMS] Policies for report %s for user %s: %s",
                report_id,
                request.user.username,
                policies,
            )
        except Exception as exc:  # defensive: optional diagnostic log
            logger.error("[DEBUG_PERMS] Failed to fetch policies: %s", exc)

        response = requests.get(
            url,
            auth=auth,
            headers={"Accept": "application/octet-stream"},
            stream=True,
            timeout=60,
        )

        if response.status_code == 200:
            file_response = StreamingHttpResponse(
                response.iter_content(chunk_size=8192),
                content_type="application/octet-stream",
            )
            file_response["Content-Disposition"] = f'attachment; filename="report_{info["name"]}.pbix"'
            return file_response

        if response.status_code == 403:
            logger.error(
                "Download forbidden for report %s by user %s: %s",
                report_id,
                request.user.username,
                response.text,
            )
            messages.error(
                request,
                "Acces refuse. Vous n'avez pas les permissions necessaires sur "
                "le serveur de rapports pour telecharger ce rapport.",
            )
            return redirect(request.META.get("HTTP_REFERER", "powerbi_report:report_list"))

        messages.error(request, f"Echec du telechargement du rapport. Code: {response.status_code}")
        logger.error("Download failed for report %s: status %s", report_id, response.status_code)
        return redirect(request.META.get("HTTP_REFERER", "powerbi_report:report_list"))

    except requests.exceptions.Timeout:
        messages.error(request, "Le telechargement a expire. Veuillez reessayer.")
        logger.error("Timeout downloading report %s", report_id)
        return redirect(request.META.get("HTTP_REFERER", "powerbi_report:report_list"))
    except Exception as exc:
        messages.error(request, f"Erreur lors du telechargement: {exc}")
        logger.error("Error downloading report %s: %s", report_id, exc)
        return redirect(request.META.get("HTTP_REFERER", "powerbi_report:report_list"))
