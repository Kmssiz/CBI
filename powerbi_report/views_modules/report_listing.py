"""Power BI report listing view helpers."""

from collections.abc import Callable
from urllib.parse import quote

from django.core.paginator import Paginator
from django.shortcuts import render

from notifications.models import Notification


def report_list_flat_view(
    request,
    report_server_url: str,
    context_root_folders: dict[str, str],
    reports_getter: Callable,
    permissions_getter: Callable,
    local_reports_getter: Callable | None = None,
):
    """Render flat Power BI report listing."""
    if local_reports_getter is not None:
        reports = local_reports_getter(request.user, request=request)
    else:
        pbirs_reports = reports_getter(request, endpoint="CatalogItems")
        base_embed_url = f"{report_server_url}/Reports/powerbi/"
        reports = []
        for report in pbirs_reports:
            path = report.get("Path", "")
            clean_path = path.strip("/")
            encoded_path = quote(clean_path, safe="/")
            embed_url = f"{base_embed_url}{encoded_path}?rs:embed=true"
            reports.append(
                {
                    "Id": report.get("Id"),
                    "Name": report.get("Name", "Unnamed"),
                    "Path": path,
                    "Type": "PowerBIReport",
                    "embed_url": embed_url,
                }
            )

    context_param = request.GET.get("context", "").strip()
    root_folder = context_root_folders.get(context_param)
    if root_folder:
        reports = [r for r in reports if r.get("Path", "").startswith(root_folder + "/")]

    query = request.GET.get("q", "").strip()
    if query:
        reports = [r for r in reports if query.lower() in r.get("Name", "").lower()]

    reports.sort(key=lambda x: x.get("Name", "").lower())

    paginator = Paginator(reports, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = permissions_getter(request.user)

    return render(
        request,
        "powerbi_report/report_list_flat.html",
        {
            "reports": page_obj,
            "notifications": notifications,
            "unread": unread,
            "permissions": permissions,
            "query": query,
        },
    )


def report_list_view(
    request,
    local_reports_getter: Callable,
    permissions_getter: Callable,
):
    """Render default report listing from local DB permissions."""
    query = request.GET.get("q", "").strip()
    reports = local_reports_getter(request.user, request=request)

    if query:
        reports = [r for r in reports if query.lower() in r.get("Name", "").lower()]

    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = permissions_getter(request.user)

    reports.sort(key=lambda x: x.get("Name", "").lower())

    paginator = Paginator(reports, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "powerbi_report/report_list.html",
        {
            "reports": page_obj,
            "notifications": notifications,
            "unread": unread,
            "permissions": permissions,
            "query": query,
        },
    )
