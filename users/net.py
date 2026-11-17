"""Who is really calling: the caller's address when the application sits behind a reverse proxy."""
import ipaddress

from django.conf import settings


def _trusted_networks():
    networks = []
    for item in getattr(settings, "TRUSTED_PROXY_IPS", []) or []:
        item = str(item).strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def client_ip(request) -> str:
    """
    The caller's address, for throttling and for the admin audit trail.

    ``X-Forwarded-For`` is only believed when the direct peer is one of ``TRUSTED_PROXY_IPS`` (the
    reverse proxy in front of the application): anyone else could write that header. Within it the last
    entry that is not itself a trusted proxy is used, since earlier entries can be forged by the client.
    ``MOBILE_LOGIN_CLIENT_IP_HEADER`` names a header the proxy overwrites, and wins when set.
    """
    configured = getattr(settings, "MOBILE_LOGIN_CLIENT_IP_HEADER", "")
    if configured:
        return request.META.get(configured, "").split(",", 1)[0].strip() or "unknown"
    peer = request.META.get("REMOTE_ADDR", "")
    networks = _trusted_networks()
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if networks and forwarded and _is_trusted(peer, networks):
        for candidate in reversed([part.strip() for part in forwarded.split(",")]):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not _is_trusted(candidate, networks):
                return candidate
    return peer or "unknown"
