"""
Manual row-level security (RLS) management for PBIRS reports.

Every report defines its own RLS roles (by company, direction, wilaya...), so
the application never guesses a role: an admin reads the roles of the report
from PBIRS and assigns users to them explicitly.

PBIRS replaces ALL assignments of a report on write, so every change here is
"read the full list, change only the targeted entries, write the full list".
A failed read raises and nothing is written.
"""

import logging
from dataclasses import dataclass, field

from django.conf import settings

from powerbi_report.models import ReportRef
from powerbi_report.services.pbirs_client import PBIRSClient, PBIRSClientError
from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url

logger = logging.getLogger('powerbi_report')


def short_name(value: str) -> str:
    """'DOMAIN\\user' -> 'user', lower-cased, for comparisons."""
    return (value or '').split('\\')[-1].strip().lower()


def qualified(identity: str) -> str:
    identity = (identity or '').strip()
    return identity if '\\' in identity else f"{settings.LDAP_DOMAIN}\\{identity}"


def _server_url(report: ReportRef) -> str:
    return report.server_url or get_primary_pbirs_server_url()


@dataclass
class RlsState:
    roles: list = field(default_factory=list)        # [{ModelRoleId, ModelRoleName}]
    assignments: list = field(default_factory=list)  # raw PBIRS entries, as read

    def role_by_id(self, role_id: str):
        return next((r for r in self.roles if r['ModelRoleId'] == role_id), None)

    def to_role_id(self, value: str):
        """An assignment may list roles by id or by name: resolve either to the id."""
        for role in self.roles:
            if value in (role['ModelRoleId'], role['ModelRoleName']):
                return role['ModelRoleId']
        return None

    @property
    def uses_role_names(self) -> bool:
        """
        True when existing assignments reference roles by name rather than id.
        Mirrors whatever PBIRS already returned; ids are the default when no
        assignment exists yet to learn from.
        """
        ids = {r['ModelRoleId'] for r in self.roles}
        names = {r['ModelRoleName'] for r in self.roles}
        for entry in self.assignments:
            for value in entry.get('DataModelRoles') or []:
                if value in ids:
                    return False
                if value in names:
                    return True
        return False

    def token(self, role: dict) -> str:
        return role['ModelRoleName'] if self.uses_role_names else role['ModelRoleId']

    def entry_role_ids(self, entry: dict) -> list:
        return [rid for rid in (self.to_role_id(v) for v in entry.get('DataModelRoles') or []) if rid]


def load_rls(client: PBIRSClient, report: ReportRef) -> RlsState:
    """Roles and assignments of a report. Raises PBIRSClientError on any failure."""
    url = _server_url(report)
    roles = client.get_data_model_roles_strict(report.pbirs_id, base_url=url)
    assignments = client.get_role_assignments_strict(report.pbirs_id, base_url=url) if roles else []
    return RlsState(roles=roles, assignments=assignments)


def assign_roles(client: PBIRSClient, report: ReportRef, identities: list, role_ids: list) -> int:
    """
    Give each identity each of the roles (in addition to what they already have).
    Returns the number of (user, role) pairs added. Roles unknown to the report
    are an error: nothing is written.
    """
    state = load_rls(client, report)
    wanted = []
    for role_id in role_ids:
        role = state.role_by_id(role_id)
        if role is None:
            raise PBIRSClientError(f"Role {role_id} does not exist on report {report.pbirs_id}")
        wanted.append(role)

    by_user = {short_name(a.get('GroupUserName')): a for a in state.assignments}
    assignments = state.assignments
    added = 0
    for identity in identities:
        key = short_name(identity)
        if not key:
            continue
        entry = by_user.get(key)
        if entry is None:
            entry = {"GroupUserName": qualified(identity), "DataModelRoles": []}
            assignments.append(entry)
            by_user[key] = entry
        have = set(state.entry_role_ids(entry))
        for role in wanted:
            if role['ModelRoleId'] not in have:
                entry.setdefault('DataModelRoles', []).append(state.token(role))
                have.add(role['ModelRoleId'])
                added += 1
    if added:
        client.set_role_assignments_strict(report.pbirs_id, assignments, base_url=_server_url(report))
    return added


def set_user_roles(client: PBIRSClient, report: ReportRef, identity: str, role_ids: list) -> bool:
    """
    Make `role_ids` the complete set of roles of one user on the report, like
    ticking and unticking roles in the PBIRS portal. An empty list removes the
    user from RLS. Everyone else's assignments are written back untouched.
    Returns False when nothing needed to change.
    """
    state = load_rls(client, report)
    wanted = []
    for role in state.roles:  # keeps the report's own order and ignores duplicates
        if role['ModelRoleId'] in role_ids:
            wanted.append(role)
    unknown = set(role_ids) - {r['ModelRoleId'] for r in state.roles}
    if unknown:
        raise PBIRSClientError(f"Unknown role(s) for report {report.pbirs_id}: {sorted(unknown)}")

    key = short_name(identity)
    entry = next((a for a in state.assignments if short_name(a.get('GroupUserName')) == key), None)
    current = set(state.entry_role_ids(entry)) if entry else set()
    if current == {r['ModelRoleId'] for r in wanted}:
        return False

    others = [a for a in state.assignments if a is not entry]
    if wanted:
        if entry is None:
            entry = {"GroupUserName": qualified(identity)}
        entry['DataModelRoles'] = [state.token(r) for r in wanted]
        others.append(entry)
    client.set_role_assignments_strict(report.pbirs_id, others, base_url=_server_url(report))
    return True


def remove_role(client: PBIRSClient, report: ReportRef, identity: str, role_id: str) -> bool:
    """Take one role away from one user, leaving everything else untouched."""
    state = load_rls(client, report)
    key = short_name(identity)
    kept, changed = [], False
    for entry in state.assignments:
        if short_name(entry.get('GroupUserName')) != key:
            kept.append(entry)
            continue
        remaining = [v for v in entry.get('DataModelRoles') or [] if state.to_role_id(v) != role_id]
        if len(remaining) != len(entry.get('DataModelRoles') or []):
            changed = True
        if remaining:
            entry['DataModelRoles'] = remaining
            kept.append(entry)
    if changed:
        client.set_role_assignments_strict(report.pbirs_id, kept, base_url=_server_url(report))
    return changed
