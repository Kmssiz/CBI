"""Records what administrators change (see ``AdminAuditLog``)."""
import logging

from .models import AdminAuditLog
from .net import client_ip

logger = logging.getLogger("users")

_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Housekeeping calls made by the pages themselves, not decisions of an administrator.
_IGNORED_PREFIXES = ("/mobile/", "/notifications/", "/static/", "/admin/jsi18n/")
_IGNORED_FRAGMENTS = ("keepalive", "mark_read", "mark-read", "tour")


class AdminAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception:  # an audit failure must never break the admin's action
            logger.exception("Admin audit entry could not be written")
        return response

    @staticmethod
    def _record(request, response) -> None:
        if request.method not in _CHANGING_METHODS:
            return
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or not user.is_admin:
            return
        path = request.path
        if path.startswith(_IGNORED_PREFIXES) or any(part in path.lower() for part in _IGNORED_FRAGMENTS):
            return
        match = getattr(request, "resolver_match", None)
        AdminAuditLog.objects.create(
            admin=user,
            admin_username=user.username[:150],
            method=request.method,
            path=path[:512],
            view_name=(match.view_name if match else "")[:200],
            status_code=response.status_code,
            ip_address=client_ip(request)[:64],
        )
