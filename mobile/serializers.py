"""Plain-dict serializers for the mobile JSON API."""
import re
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from users.models import CustomUser, UserHistory

from .catalog import describe_location, is_mobile_admin, view_access

REPORT_VIEW_PREFIX = 'Rapport consulté'
NEW_NOTIFICATION_DAYS = 7
_ACCESS_WORDS = re.compile(r'\b(acc[eè]s|access|permissions?|r[oô]les?)\b', re.IGNORECASE)
_REPORT_WORDS = re.compile(r'\b(rapports?|reports?)\b', re.IGNORECASE)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def user_description(user: CustomUser) -> str:
    parts = [part for part in (user.direction, user.societe) if part]
    if parts:
        return ' · '.join(parts)
    return user.role.name.capitalize() if user.role else ''


def serialize_user_summary(user: CustomUser) -> dict:
    return {
        'id': user.pk,
        'name': user.get_full_name() or user.username,
        'initials': ''.join(char for char in user.get_initials() if char.isalnum()),
        'description': user_description(user),
        'company': user.societe or '',
        'photo_url': reverse('mobile:user_photo', args=[user.pk]) if user.profile_image else None,
        'avatar_color': user.get_avatar_color(),
    }


def serialize_me(user: CustomUser) -> dict:
    return {
        **serialize_user_summary(user),
        'username': user.username,
        'email': user.email,
        'ad2000': user.ad2000 or '',
        'role': user.role.name if user.role else '',
        'is_admin': is_mobile_admin(user),
        'direction': user.direction or '',
        'pole': user.pole or '',
        'photo_url': reverse('mobile:me_photo') if user.profile_image else None,
        'permissions': view_access(user),
    }


def notification_kind(message: str) -> tuple[str, str]:
    """Notifications only store free text; infer the legacy type ("Accès" / "Rapport")."""
    if _ACCESS_WORDS.search(message):
        return 'access', 'Accès'
    if _REPORT_WORDS.search(message):
        return 'report', 'Rapport'
    return 'info', 'Notification'


def serialize_notification(notification: Notification) -> dict:
    kind, title = notification_kind(notification.message)
    return {
        'id': notification.pk,
        'title': title,
        'kind': kind,
        'message': notification.message,
        'created_at': _iso(notification.created_at),
        'is_read': notification.is_read,
        'is_new': notification.created_at >= timezone.now() - timedelta(days=NEW_NOTIFICATION_DAYS),
    }


def serialize_history(entry: UserHistory) -> dict:
    report = entry.report
    if report is not None:
        report_name, location = report.name, describe_location(report)
    else:
        # Web consultations only store "Rapport consulté : <name>".
        report_name = entry.action.split(':', 1)[1].strip() if ':' in entry.action else entry.action
        report_name = report_name.removesuffix('(mobile)').strip()
        location = ''
    return {
        'id': entry.pk,
        'report_id': entry.report_id,
        'report_name': report_name,
        'location': location,
        'opened_at': _iso(entry.timestamp),
        'duration_seconds': entry.duration_seconds,
        'source': entry.source,
    }
