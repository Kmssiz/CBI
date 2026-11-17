"""Which section of the "Tableau de bord" menu the current page belongs to."""
from django.urls import reverse

from .pagination import PAGE_SIZES

# key -> (label, URL name of the section's own page)
SECTIONS = {
    "direction": ("Direction", "powerbi_report:custom_direction"),
    "pole": ("Pôle", "powerbi_report:custom_pole"),
    "consolide": ("Consolidé", "powerbi_report:custom_consolide"),
    "biblio": ("Bibliothèque", "powerbi_report:custom_biblio"),
    "module": ("Module", "powerbi_report:custom_module"),
    "anomalie": ("Anomalie", "powerbi_report:custom_anomalie"),
}

# Pages shared with the admin area that are also reachable from a section ("Tous les rapports",
# an opened report): they carry ?context=<section>.
SHARED_PAGES = ("report_list_flat", "embed_report")


def nav_section(request):
    """
    nav_section / nav_section_label / nav_section_url: the section to keep highlighted in the menu.
    A page inside /powerbi/custom/<section>/ belongs to it; so do "Tous les rapports" and the report
    pages opened from it (?context=<section>). Empty everywhere else.
    """
    key = next((k for k in SECTIONS if f"/custom/{k}" in request.path), "")
    if not key:
        match = getattr(request, "resolver_match", None)
        context = request.GET.get("context", "")
        if match and match.url_name in SHARED_PAGES and context in SECTIONS:
            key = context
    if not key:
        return {"nav_section": ""}
    label, url_name = SECTIONS[key]
    return {"nav_section": key, "nav_section_label": label, "nav_section_url": reverse(url_name)}


def table_page_sizes(request):
    """The table lengths offered by the "Lignes" picker (templates/_page_size.html)."""
    return {"page_size_choices": PAGE_SIZES}
