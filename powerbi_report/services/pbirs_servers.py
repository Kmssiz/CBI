"""Helpers for resolving PBIRS servers from the database at runtime."""

from django.conf import settings


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


SERVERS_CACHE_KEY = "pbirs:active_servers"
SERVERS_CACHE_SECONDS = 60


def get_active_pbirs_servers() -> list:
    """
    The active servers, cached for a minute: this is asked for dozens of times per page
    (every report row, every PBIRS call). Saving or deleting a server clears the cache.
    """
    from django.core.cache import cache
    from powerbi_report.models import PBIRSServer

    servers = cache.get(SERVERS_CACHE_KEY)
    if servers is None:
        servers = list(PBIRSServer.objects.filter(is_active=True).order_by("id"))
        cache.set(SERVERS_CACHE_KEY, servers, SERVERS_CACHE_SECONDS)
    return servers


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
