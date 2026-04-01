"""Power BI embed view helpers."""

import requests
from collections.abc import Callable
from urllib.parse import quote

from django.conf import settings
from django.shortcuts import render

from notifications.models import Notification
from powerbi_report.models import ReportRef


def embed_report_view(
    request,
    report_path: str,
    auth_getter: Callable,
    permissions_getter: Callable,
):
    """Render embedded PBIRS report page."""
    auth = auth_getter(request)
    session = requests.Session()
    session.auth = auth

    report_path = report_path.strip("/")
    encoded_path = quote(report_path, safe="/")

    report_ref = ReportRef.objects.filter(path="/" + report_path).first()
    if not report_ref:
        report_ref = ReportRef.objects.filter(path=report_path).first()
    report_id = report_ref.pbirs_id if report_ref else None

    # Use the report's own server URL, or fall back to default
    server_url = (report_ref.server_url if report_ref and report_ref.server_url
                  else settings.POWERBI_REPORT_SERVER_URL)
    embed_url = f"{server_url}/Reports/powerbi/{encoded_path}?rs:embed=true"

    breadcrumbs = []
    path_parts = report_path.split("/")
    current_path = ""
    for part in path_parts:
        current_path = f"{current_path}/{part}" if current_path else part
        breadcrumbs.append({"name": part, "url": current_path})

    try:
        response = session.get(embed_url)
        response.raise_for_status()

        notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
        unread = Notification.objects.filter(user=request.user, is_read=False).count()
        permissions = permissions_getter(request.user)

        return render(
            request,
            "powerbi_report/embed_report.html",
            {
                "embed_url": embed_url,
                "report_id": report_id,
                "breadcrumbs": breadcrumbs,
                "notifications": notifications,
                "unread": unread,
                "permissions": permissions,
            },
        )
    except requests.exceptions.RequestException as exc:
        return render(
            request,
            "powerbi_report/embed_report.html",
            {
                "error": f"Erreur lors du chargement du rapport : {exc}",
                "report_id": report_id,
                "breadcrumbs": breadcrumbs,
            },
        )
