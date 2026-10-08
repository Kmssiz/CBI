"""Report catalogue for the mobile app.

Maps the web portal's metadata (consolidé flag, pôle / direction / société /
module tags, report types) onto the legacy GSH-CBI navigation:
section → group (card on the home screen) → tab (direction) → reports.
Visibility reuses ``get_visible_report_ids`` so mobile and web always agree.
"""
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest
from django.urls import reverse

from powerbi_report.models import LOGO_OPTION_TYPES, PBIRSServer, ReportRef, UserReportPermission
from users.models import CustomUser, MobileFavorite

GENERAL_TAB = 'Général'
_CODE_STOPWORDS = {'a', 'au', 'aux', 'd', 'de', 'des', 'du', 'en', 'et', 'l', 'la', 'le', 'les', 'pour', 'sur'}

# (key, title, home layout). Order is the order of the home screen.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ('consolide', 'Consolidé', 'row'),
    ('pole', 'Pôle', 'row'),
    ('societe', 'Société', 'grid'),
    ('direction', 'Direction', 'row'),
    ('module', 'Modules', 'row'),
    ('anomalie', 'Anomalies', 'row'),
    ('biblio', 'Bibliothèque', 'row'),
)
SECTION_TITLES = {key: title for key, title, _ in SECTIONS}


def is_mobile_admin(user: CustomUser) -> bool:
    return bool(user.is_superuser or user.is_admin)


def view_access(user: CustomUser) -> dict[str, bool]:
    """Per-view gates, identical to the web portal's ``can_view_*`` checks."""
    admin = is_mobile_admin(user)
    return {
        'consolide': admin or user.can_view_consolide,
        'pole': admin or user.can_view_pole,
        'direction': admin or user.can_view_direction,
        'module': admin or user.can_view_module,
        'anomalie': admin or user.can_view_anomalie,
        'biblio': True,
    }


def user_can_open(user: CustomUser, report: ReportRef) -> bool:
    if is_mobile_admin(user):
        return True
    return UserReportPermission.objects.filter(user=user, report=report).exists()


class ReportAccess:
    """Which reports a user may open, loaded once (admins may open everything)."""

    def __init__(self, user: CustomUser) -> None:
        self._all = is_mobile_admin(user)
        self._ids = set() if self._all else set(
            UserReportPermission.objects.filter(user=user).values_list('report_id', flat=True)
        )

    def can_open(self, report_id: int | None) -> bool:
        return report_id is not None and (self._all or report_id in self._ids)


def metadata_logo_url(option) -> str | None:
    """Versioned URL (the file name changes on every upload) so the app can cache logos."""
    if not option.logo:
        return None
    version = option.logo.name.rsplit('/', 1)[-1].rsplit('.', 1)[0]
    return f"{reverse('mobile:metadata_logo', args=[option.pk])}?v={version}"


def make_code(name: str) -> str:
    """Short upper-case code used as tab label and asset key ("Direction Finance et Comptabilité" → "DFC")."""
    ascii_name = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode().strip()
    if not ascii_name:
        return ''
    words = [word for word in re.split(r"[\s'’\-_/.,()]+", ascii_name) if word]
    if len(words) == 1:
        return words[0][:8].upper()
    initials = ''.join(word[0] for word in words if word.casefold() not in _CODE_STOPWORDS)
    return (initials or words[0])[:8].upper()


def _names(options: Iterable) -> list[str]:
    return sorted({option.name for option in options if option.name})


def describe_location(report: ReportRef) -> str:
    """Breadcrumb such as "Consolidé / DFC" (legacy "Niveau / DIR" line)."""
    directions = _names(report.directions.all()) or ([report.direction] if report.direction else [])
    if report.is_consolide:
        head = 'Consolidé'
    elif societes := _names(report.societes.all()):
        head = societes[0]
    elif poles := _names(report.poles.all()):
        head = poles[0]
    elif modules := _names(report.modules.all()):
        return modules[0]
    elif report.report_type == 'anomalie':
        head = 'Anomalies'
    elif report.report_type == 'bibliotheque':
        head = 'Bibliothèque'
    else:
        head = report.societe or report.pole or ''
    tail = make_code(directions[0]) if directions else ''
    return ' / '.join(part for part in (head, tail) if part)


def _normalize_url(url: str | None) -> str:
    return (url or '').strip().rstrip('/')


class ServerRegistry:
    """Known PBIRS servers. The app only answers NTLM challenges for these hosts."""

    def __init__(self) -> None:
        self._by_url: dict[str, dict] = {}
        for server in PBIRSServer.objects.filter(is_active=True).order_by('id'):
            self._add(_normalize_url(server.base_url), server.pk, server.name)
        fallback = _normalize_url(getattr(settings, 'POWERBI_REPORT_SERVER_URL', ''))
        self.primary_url = next(iter(self._by_url), fallback)
        self._synthetic_id = 0

    def _add(self, url: str, server_id: int, name: str) -> dict | None:
        parts = urlsplit(url)
        if not url or not parts.hostname:
            return None
        return self._by_url.setdefault(url, {
            'id': server_id, 'name': name or parts.hostname, 'base_url': url,
            'host': parts.hostname.lower(), 'scheme': parts.scheme.lower(),
        })

    def for_report(self, report: ReportRef) -> dict | None:
        url = _normalize_url(report.server_url) or self.primary_url
        if url in self._by_url:
            return self._by_url[url]
        # A report synced from a server that is no longer registered (or the
        # settings fallback): expose it with a negative id.
        self._synthetic_id -= 1
        return self._add(url, self._synthetic_id, '')

    def as_list(self) -> list[dict]:
        return list(self._by_url.values())


def embed_url(report: ReportRef, server: dict | None) -> str:
    if not server:
        return ''
    path = quote((report.path or '').strip('/'), safe='/')
    return f"{server['base_url']}/Reports/powerbi/{path}?rs:embed=true"


def serialize_report(report: ReportRef, servers: ServerRegistry, favorite_ids: set[int]) -> dict:
    server = servers.for_report(report)
    return {
        'id': report.pk,
        'name': report.name,
        'description': report.description or '',
        'location': describe_location(report),
        'server_id': server['id'] if server else None,
        'embed_url': embed_url(report, server),
        # Power BI phone layout already extracted for this report (see mobile/mobile_layout.py).
        'has_mobile_layout': _has_mobile_layout(report),
        'modified_at': report.modified_at.isoformat() if report.modified_at else None,
        'favorite': report.pk in favorite_ids,
    }


def favorite_ids_for(user: CustomUser) -> set[int]:
    return set(MobileFavorite.objects.filter(user=user).values_list('report_id', flat=True))


def _has_mobile_layout(report: ReportRef) -> bool:
    try:
        return bool(report.mobile_layout.data.get('pages'))
    except ObjectDoesNotExist:
        return False


def with_metadata(queryset):
    return queryset.select_related('mobile_layout').prefetch_related(
        'poles', 'directions', 'societes__parent', 'modules',
    )


# --- catalogue -------------------------------------------------------------

@dataclass
class _Group:
    key: str
    name: str
    parent: str | None = None
    logo_url: str | None = None
    tabs: dict[str, dict] = field(default_factory=dict)

    def add(self, tab_key: str, tab_name: str, report_id: int) -> None:
        tab = self.tabs.setdefault(tab_key, {'key': tab_key, 'name': tab_name, 'code': make_code(tab_name), 'report_ids': []})
        if report_id not in tab['report_ids']:
            tab['report_ids'].append(report_id)

    def as_dict(self, names: dict[int, str]) -> dict:
        tabs = sorted(self.tabs.values(), key=lambda tab: (tab['name'] == GENERAL_TAB, tab['name'].casefold()))
        for tab in tabs:
            tab['report_ids'].sort(key=lambda report_id: names[report_id].casefold())
        data = {'key': self.key, 'name': self.name, 'code': make_code(self.name), 'tabs': tabs}
        if self.parent:
            data['parent'] = self.parent
        if self.logo_url:
            data['logo_url'] = self.logo_url
        return data


def _direction_tabs(report: ReportRef) -> list[tuple[str, str]]:
    options = sorted(report.directions.all(), key=lambda option: option.name.casefold())
    if options:
        return [(f'direction:{option.pk}', option.name) for option in options]
    if report.direction:
        return [(f'direction:{make_code(report.direction)}', report.direction)]
    return [('general', GENERAL_TAB)]


def _group_by_direction(group: _Group, reports: Iterable[ReportRef]) -> None:
    for report in reports:
        for tab_key, tab_name in _direction_tabs(report):
            group.add(tab_key, tab_name, report.pk)


def _group_by_option(reports: Iterable[ReportRef], attribute: str, prefix: str, split_directions: bool) -> list[_Group]:
    groups: dict[int, _Group] = {}
    for report in reports:
        for option in getattr(report, attribute).all():
            if option.pk not in groups:
                parent = option.parent.name if prefix == 'societe' and option.parent_id else None
                logo = metadata_logo_url(option) if prefix in LOGO_OPTION_TYPES else None
                groups[option.pk] = _Group(f'{prefix}:{option.pk}', option.name, parent, logo)
            group = groups[option.pk]
            if split_directions:
                _group_by_direction(group, [report])
            else:
                group.add('all', option.name, report.pk)
    return sorted(groups.values(), key=lambda group: group.name.casefold())


def _build_groups(key: str, reports: list[ReportRef]) -> list[_Group]:
    if key in ('consolide', 'anomalie', 'biblio'):
        group = _Group(key, SECTION_TITLES[key])
        _group_by_direction(group, reports)
        return [group]
    if key == 'pole':
        return _group_by_option([r for r in reports if not r.societes.all()], 'poles', 'pole', True)
    if key == 'societe':
        return _group_by_option(reports, 'societes', 'societe', True)
    if key == 'direction':
        return _group_by_option([r for r in reports if not r.societes.all()], 'directions', 'direction', False)
    return _group_by_option(reports, 'modules', 'module', False)


def build_catalog(request: HttpRequest) -> dict:
    """Everything the home / tabs / lists / favorites screens need (see docs/MOBILE_API.md)."""
    from powerbi_report.views import get_visible_report_ids

    user = request.user
    access = view_access(user)
    visible: dict[str, set[str]] = {
        view: (get_visible_report_ids(request, view_type=view) if allowed else set())
        for view, allowed in access.items()
    }
    # Société cards gather the société-tagged reports of the pôle and direction views, like the web.
    visible['societe'] = visible['pole'] | visible['direction']

    all_ids = set().union(*visible.values())
    reports_by_pbirs_id = {
        report.pbirs_id: report
        for report in with_metadata(
            ReportRef.objects.filter(pbirs_id__in=all_ids)
        )
    }
    reports_by_pk = {report.pk: report for report in reports_by_pbirs_id.values()}
    names = {pk: report.name for pk, report in reports_by_pk.items()}

    sections = []
    referenced_ids: set[int] = set()
    for key, title, layout in SECTIONS:
        reports = [reports_by_pbirs_id[pid] for pid in visible[key] if pid in reports_by_pbirs_id]
        if key == 'societe':
            reports = [report for report in reports if report.societes.all()]
        elif key == 'direction':
            # The web's direction view overlaps consolidé/pôle; only add what the home screen does not show yet.
            reports = [report for report in reports if report.pk not in referenced_ids]
        groups = [group.as_dict(names) for group in _build_groups(key, reports)]
        groups = [group for group in groups if group['tabs']]
        if not groups:
            continue
        sections.append({'key': key, 'title': title, 'layout': layout, 'groups': groups})
        referenced_ids.update(
            report_id for group in groups for tab in group['tabs'] for report_id in tab['report_ids']
        )

    servers = ServerRegistry()
    favorite_ids = favorite_ids_for(user)
    serialized = {
        str(pk): serialize_report(reports_by_pk[pk], servers, favorite_ids)
        for pk in sorted(referenced_ids)
    }
    return {
        'servers': servers.as_list(),
        'sections': sections,
        'reports': serialized,
        'favorite_ids': sorted(report_id for report_id in favorite_ids if str(report_id) in serialized),
    }
