"""Power BI report listing view helpers."""

from collections.abc import Callable
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.shortcuts import render

from notifications.models import Notification
from powerbi_report.services.pbirs_servers import get_active_pbirs_server_urls


def get_allowed_report_types_for_view(view_type):
    """
    Mapping between view types and allowed ReportRef.report_type metadata.
    Also returns specific field requirements.
    Keep in sync with views.py version.
    """
    if view_type == 'pole':
        return (['dashboard'], 'pole') # Required field 'pole'
    elif view_type == 'direction':
        return (['dashboard'], 'direction') # Required field 'direction'
    elif view_type == 'consolide':
        return (['dashboard'], 'is_consolide') # Required boolean 'is_consolide'
    elif view_type == 'biblio':
        return (['bibliotheque'], None)
    elif view_type == 'anomalie':
        return (['anomalie'], None)
    return ([], None)


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
    
    # Strictly enforce metadata mapping for context-driven views
    if context_param:
        allowed_types, req_field = get_allowed_report_types_for_view(context_param)
        if allowed_types:
            reports = [r for r in reports if r.get("report_type") in allowed_types]
        if req_field:
            if req_field == 'is_consolide':
                reports = [r for r in reports if r.get("is_consolide")]
            else:
                reports = [r for r in reports if r.get(req_field)]

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

    # Calculate page range for pagination UI
    try:
        page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1)
    except:
        page_range = []

    return render(
        request,
        "powerbi_report/report_list_flat.html",
        {
            "reports": page_obj,
            "page_range": page_range,
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
    
    # New metadata filters
    f_direction = request.GET.get("direction", "").strip()
    f_pole = request.GET.get("pole", "").strip()
    f_societe = request.GET.get("societe", "").strip()
    f_type = request.GET.get("type", "").strip()
    f_consolide = request.GET.get("consolide") == "on"

    reports = local_reports_getter(request.user, request=request)

    # Collect metadata for filters (from the unfiltered list of accessible reports)
    all_directions = sorted(list({r.get("direction") for r in reports if r.get("direction")}))
    all_poles = sorted(list({r.get("pole") for r in reports if r.get("pole")}))
    all_societes = sorted(list({r.get("societe") for r in reports if r.get("societe")}))
    
    # Calculate metadata relationships for cascading filters
    metadata_relations = {
        'directions': {}, # dir -> [poles]
        'poles': {} # pole -> [societes]
    }
    for r in reports:
        d = r.get('direction')
        p = r.get('pole')
        s = r.get('societe')
        if d:
            if d not in metadata_relations['directions']: metadata_relations['directions'][d] = set()
            if p: metadata_relations['directions'][d].add(p)
        if p:
            if p not in metadata_relations['poles']: metadata_relations['poles'][p] = set()
            if s: metadata_relations['poles'][p].add(s)
    
    # Convert sets to sorted lists for JSON serialization if needed, or just pass as is
    for d in metadata_relations['directions']:
        metadata_relations['directions'][d] = sorted(list(metadata_relations['directions'][d]))
    for p in metadata_relations['poles']:
        metadata_relations['poles'][p] = sorted(list(metadata_relations['poles'][p]))

    # Apply Filters
    if query:
        reports = [r for r in reports if query.lower() in r.get("Name", "").lower()]
    if f_direction:
        reports = [r for r in reports if r.get("direction") == f_direction]
    if f_pole:
        reports = [r for r in reports if r.get("pole") == f_pole]
    if f_societe:
        reports = [r for r in reports if r.get("societe") == f_societe]
    if f_type:
        reports = [r for r in reports if r.get("report_type") == f_type]
    if f_consolide:
        reports = [r for r in reports if r.get("is_consolide")]

    active_filters_count = sum([
        1 if f_direction else 0,
        1 if f_pole else 0,
        1 if f_societe else 0,
        1 if f_type else 0,
        1 if f_consolide else 0,
    ])

    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = permissions_getter(request.user)

    reports.sort(key=lambda x: x.get("Name", "").lower())

    paginator = Paginator(reports, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # Calculate page range for pagination UI
    try:
        page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1)
    except:
        page_range = []

    server_urls = get_active_pbirs_server_urls()

    return render(
        request,
        "powerbi_report/report_list.html",
        {
            "reports": page_obj,
            "page_range": page_range,
            "notifications": notifications,
            "unread": unread,
            "permissions": permissions,
            "query": query,
            "server_urls": server_urls,
            "f_direction": f_direction,
            "f_pole": f_pole,
            "f_societe": f_societe,
            "f_type": f_type,
            "f_consolide": f_consolide,
            "active_filters_count": active_filters_count,
            "all_directions": all_directions,
            "all_poles": all_poles,
            "all_societes": all_societes,
            "metadata_relations": metadata_relations,
        },
    )
