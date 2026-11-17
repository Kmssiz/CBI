"""
Copy one user's access to another: report access, RLS roles, and (optionally) the menu sections they can see.

Two steps, so that nothing is written before an admin has seen what will happen:

  build_plan()  reads only. What would be granted, what the target already has, which RLS roles would be added.
  apply_plan()  writes, report by report, with the same rules as the existing "grant access" screens.

Rules (identical to the grant screens, so a copy can never give more than a normal grant would):
  * only DIRECT access is copied (a policy naming the source); access the source gets through a group
    comes from that group, the target gets it by being in the group;
  * the target receives the "Explorateur" role, plus the admin roles only if the target is an admin;
  * nothing is ever removed from the target, and a policy the target already has is left as it is;
  * a policy list that cannot be read is never written back (a failed read must not erase anyone's access).

PBIRS is reached through `client_factory()` (one client per call, so threads never share a session):
in the application it is `lambda: PBIRSClient(request)`.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from django.conf import settings

from powerbi_report.models import ReportRef, UserReportPermission
from powerbi_report.services import rls as rls_service
from powerbi_report.services.pbirs_client import PBIRSClientError

logger = logging.getLogger('powerbi_report')

EXPLORER_ROLE = "Explorateur"
ADMIN_EXTRA_ROLES = ["Gestionnaire de contenu", "Mes rapports", "Report Builder", "Serveur de publication"]

# The menu sections a user can see (and the slider), copied too. Turned ON only, never off.
# Bibliothèque is not listed: everybody sees it.
SECTION_FIELDS = [
    ("can_view_direction", "Direction"),
    ("can_view_pole", "Pôle"),
    ("can_view_consolide", "Consolidé"),
    ("can_view_anomalie", "Anomalie"),
    ("can_view_module", "Module"),
    ("can_use_slider", "Slider"),
]

DEFAULT_TIME_BUDGET = 240   # seconds: stay under the web server's request timeout; a re-run finishes the rest


@dataclass
class RlsItem:
    report: ReportRef
    role_ids: List[str]
    role_names: List[str]
    available: List[Tuple[str, str]] = field(default_factory=list)   # every (id, name) role the report defines


@dataclass
class CopyPlan:
    source: object
    target: object
    do_reports: bool = True
    do_rls: bool = True
    do_sections: bool = False
    roles: List[str] = field(default_factory=list)            # PBIRS roles a new policy will carry
    to_grant: List[ReportRef] = field(default_factory=list)
    already_have: int = 0
    group_only: int = 0                                       # source has them through a group: not copied
    rls: List[RlsItem] = field(default_factory=list)            # only the roles the target does not have yet
    rls_already: int = 0                                      # reports where the target already has those roles
    rls_unreadable: List[ReportRef] = field(default_factory=list)
    sections: List[Tuple[str, str]] = field(default_factory=list)   # (field, label) that would be switched on
    sections_already: List[str] = field(default_factory=list)       # labels both users already see
    source_synced_at: Optional[object] = None

    @property
    def elevated(self) -> bool:
        return len(self.roles) > 1

    @property
    def is_empty(self) -> bool:
        return not (self.to_grant or self.rls or self.sections)


@dataclass
class CopyResult:
    granted: List[ReportRef] = field(default_factory=list)
    already_present: List[ReportRef] = field(default_factory=list)   # the cache was behind: PBIRS already had them
    grant_failed: List[Tuple[ReportRef, str]] = field(default_factory=list)
    rls_done: List[Tuple[ReportRef, List[str]]] = field(default_factory=list)
    rls_failed: List[Tuple[ReportRef, str]] = field(default_factory=list)
    sections_set: List[str] = field(default_factory=list)
    not_processed: int = 0                                    # time budget reached: run the copy again


def roles_for(target) -> List[str]:
    """Same rule as the grant screens: admins also get the content-management roles."""
    names = [EXPLORER_ROLE]
    role = getattr(target, "role", None)
    if role and (role.name or "").lower() == settings.ADMIN_ROLE_NAME.lower():
        names += ADMIN_EXTRA_ROLES
    return names


def _identity(user) -> str:
    return (user.ad2000 or "").strip()


def _base_url(report: ReportRef):
    return ReportRef.get_server_url(report.pbirs_id) or None


def _roles_of(state, user) -> List[str]:
    key = rls_service.short_name(_identity(user))
    for entry in state.assignments:
        if rls_service.short_name(entry.get("GroupUserName")) == key:
            return [i for i in state.entry_role_ids(entry) if state.role_by_id(i)]
    return []


def _scan_rls(client_factory, source, target, report: ReportRef):
    """
    RLS roles to give `target` on `report`: (report, role ids to add, their names, already had how many).
    Raises PBIRSClientError when the report's RLS cannot be read.
    """
    state = rls_service.load_rls(client_factory(), report)
    available = [(r["ModelRoleId"], r["ModelRoleName"]) for r in state.roles]
    if not state.roles:
        return report, [], [], 0, available
    wanted = _roles_of(state, source)
    have = set(_roles_of(state, target))
    missing = [i for i in wanted if i not in have]
    return report, missing, [state.role_by_id(i)["ModelRoleName"] for i in missing], len(wanted) - len(missing), available


def build_plan(client_factory: Callable, source, target, *, do_reports=True, do_rls=True, do_sections=False,
               max_workers: int = 8) -> CopyPlan:
    """Read-only: what copying `source`'s access to `target` would do."""
    plan = CopyPlan(source=source, target=target, do_reports=do_reports, do_rls=do_rls, do_sections=do_sections,
                    roles=roles_for(target))

    source_perms = list(UserReportPermission.objects.filter(user=source).select_related("report"))
    direct = {p.report_id: p.report for p in source_perms if p.is_direct}
    through_group = {p.report_id: p.report for p in source_perms if not p.is_direct}
    plan.source_synced_at = max((p.synced_at for p in source_perms), default=None)
    target_has = set(UserReportPermission.objects.filter(user=target).values_list("report_id", flat=True))

    if do_reports:
        plan.to_grant = sorted((r for rid, r in direct.items() if rid not in target_has), key=lambda r: (r.name or "").lower())
        plan.already_have = sum(1 for rid in direct if rid in target_has)
        plan.group_only = sum(1 for rid in through_group if rid not in direct)

    if do_rls:
        # RLS only matters on reports the target will be able to open afterwards.
        reachable = {}
        for rid, report in {**through_group, **direct}.items():
            if rid in target_has or (do_reports and rid in direct):
                reachable[rid] = report
        reports = sorted(reachable.values(), key=lambda r: (r.name or "").lower())

        def scan(report):
            try:
                return _scan_rls(client_factory, source, target, report)
            except PBIRSClientError as exc:
                logger.warning("RLS of %s unreadable while planning a copy: %s", report.pbirs_id, exc)
                return report, None, None, 0, []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for report, ids, names, had, available in pool.map(scan, reports):
                if ids is None:
                    plan.rls_unreadable.append(report)
                elif ids:
                    plan.rls.append(RlsItem(report=report, role_ids=ids, role_names=names, available=available))
                elif had:
                    plan.rls_already += 1

    if do_sections:
        plan.sections = [(f, label) for f, label in SECTION_FIELDS if getattr(source, f) and not getattr(target, f)]
        plan.sections_already = [label for f, label in SECTION_FIELDS if getattr(source, f) and getattr(target, f)]
    return plan


def restrict_plan(plan: CopyPlan, skip_grant=(), skip_rls=(), role_picks=None) -> CopyPlan:
    """
    Apply what the admin chose on the preview.
      * unticking a report drops it, and its RLS roles too (they are locked to it), so an RLS line never
        lands on a report that is not being granted; reports the target already has keep their RLS;
      * role_picks {report pk: [role ids]} replaces the roles an RLS line gives. Only roles the report
        really defines are kept, and a line left with no role is dropped.
    """
    skip_grant = {int(x) for x in skip_grant}
    skip_rls = {int(x) for x in skip_rls} | skip_grant
    plan.to_grant = [r for r in plan.to_grant if r.pk not in skip_grant]
    kept = []
    for item in plan.rls:
        if item.report.pk in skip_rls:
            continue
        if role_picks and item.report.pk in role_picks:
            names = dict(item.available)
            ids = [rid for rid in dict.fromkeys(role_picks[item.report.pk]) if rid in names]
            if not ids:
                continue
            item.role_ids = ids
            item.role_names = [names[rid] for rid in ids]
        kept.append(item)
    plan.rls = kept
    return plan


def _has_policy(policies, identity: str) -> bool:
    key = rls_service.short_name(identity)
    return any(rls_service.short_name(p.get("GroupUserName") or p.get("UserName")) == key
               for p in policies if (p.get("GroupUserName") or p.get("UserName")))


def apply_plan(client_factory: Callable, plan: CopyPlan, *, time_budget: float = DEFAULT_TIME_BUDGET) -> CopyResult:
    """Write the plan to PBIRS and to the local cache. Never raises for one report's failure: it is reported."""
    result = CopyResult()
    deadline = time.monotonic() + time_budget
    target = plan.target
    identity = _identity(target)
    client = client_factory()
    granted_ids = set()
    roles = [{"Name": name} for name in plan.roles]

    # 1. report access
    for index, report in enumerate(plan.to_grant):
        if time.monotonic() > deadline:
            result.not_processed += len(plan.to_grant) - index
            break
        try:
            base_url = _base_url(report)
            policies = client.get_report_policies_strict(report.pbirs_id, base_url=base_url)
            newly_granted = not _has_policy(policies, identity)
            if newly_granted:
                policies.append({"GroupUserName": identity, "Roles": roles})
                client.set_report_policies_strict(report.pbirs_id, policies, base_url=base_url)
        except PBIRSClientError as exc:
            logger.error("Copy of access: %s not granted on %s: %s", identity, report.pbirs_id, exc)
            result.grant_failed.append((report, str(exc)))
            continue
        UserReportPermission.objects.update_or_create(user=target, report=report, defaults={"is_direct": True})
        granted_ids.add(report.pk)
        (result.granted if newly_granted else result.already_present).append(report)

    # 2. RLS roles, only where the target has (or has just been given) access
    have_access = granted_ids | set(UserReportPermission.objects.filter(user=target).values_list("report_id", flat=True))
    for index, item in enumerate(plan.rls):
        if item.report.pk not in have_access:
            continue
        if time.monotonic() > deadline:
            result.not_processed += len([i for i in plan.rls[index:] if i.report.pk in have_access])
            break
        try:
            added = rls_service.assign_roles(client_factory(), item.report, [identity], item.role_ids)
        except PBIRSClientError as exc:
            logger.error("Copy of RLS: %s not assigned on %s: %s", identity, item.report.pbirs_id, exc)
            result.rls_failed.append((item.report, str(exc)))
            continue
        if added:
            result.rls_done.append((item.report, item.role_names))

    # 3. menu sections (switched on only)
    if plan.sections:
        for field_name, _ in plan.sections:
            setattr(target, field_name, True)
        target.save(update_fields=[f for f, _ in plan.sections])
        result.sections_set = [label for _, label in plan.sections]
    return result
