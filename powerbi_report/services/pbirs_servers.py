"""Helpers for resolving PBIRS servers from the database at runtime."""

from django.conf import settings


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def get_active_pbirs_servers() -> list:
    from powerbi_report.models import PBIRSServer

    return list(PBIRSServer.objects.filter(is_active=True).order_by("id"))


def get_active_pbirs_server_urls() -> list[str]:
    urls = [
        _normalize_base_url(server.base_url)
        for server in get_active_pbirs_servers()
        if _normalize_base_url(server.base_url)
    ]
    if urls:
        return urls

    fallback = _normalize_base_url(getattr(settings, "POWERBI_REPORT_SERVER_URL", ""))
    return [fallback] if fallback else []


def get_primary_pbirs_server_url() -> str:
    urls = get_active_pbirs_server_urls()
    return urls[0] if urls else ""


def get_pbirs_server_name_map() -> dict[str, str]:
    return {
        _normalize_base_url(server.base_url): server.name
        for server in get_active_pbirs_servers()
        if _normalize_base_url(server.base_url)
    }
