"""
Learn each employee's job title, company and phone from Odoo, and spot NEW users.

A new user is someone who exists in BOTH systems: a CBI account (from LDAP) created after
the cutoff, and an Odoo employee matched to it. They are announced to the admins, who
decide by hand what to grant. Nothing here ever grants access.

Matching an employee to a CBI user:
  (1) employee_id == ad2000/username, else
  (2) a full-name match that is unambiguous on BOTH sides.
Homonyms are skipped and counted, never guessed: a wrong match would show someone
another person's job and could lead to granting them the wrong reports.
"""

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from powerbi_report.models import NewUserAlert, OdooProfile, OdooSettings
from powerbi_report.services.odoo_client import OdooEmployee, fetch_current_employees
from users.models import CustomUser

logger = logging.getLogger('powerbi_report')


def normalize_text(value: str) -> str:
    """Lower-case, accent-free, letters/digits only, single spaces."""
    decomposed = unicodedata.normalize('NFKD', value or '')
    ascii_only = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', ' ', ascii_only.lower()).strip()


def name_key(full_name: str) -> str:
    """Order-insensitive name key: 'DUPONT Jean' == 'Jean Dupont'."""
    return ' '.join(sorted(normalize_text(full_name).split()))


@dataclass
class SyncSummary:
    employees: int = 0
    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    new_users: int = 0          # newly announced to the admins by this run
    unmatched_examples: list = field(default_factory=list)


def _match_users(employees: list[OdooEmployee]):
    """Yield (employee, user | None, reason)."""
    users = list(CustomUser.objects.filter(is_active=True))
    by_identifier = {}
    for user in users:
        for identifier in (user.ad2000, user.username):
            if identifier and identifier.strip():
                by_identifier.setdefault(identifier.strip().lower(), user)
    by_name = defaultdict(list)
    for user in users:
        key = name_key(f"{user.first_name} {user.last_name}")
        if key:
            by_name[key].append(user)
    odoo_name_counts = Counter(name_key(e.full_name) for e in employees)

    for employee in employees:
        user = by_identifier.get(employee.employee_id.lower())
        if user is not None:
            yield employee, user, 'id'
            continue
        key = name_key(employee.full_name)
        candidates = by_name.get(key, [])
        if len(candidates) == 1 and odoo_name_counts[key] == 1:
            yield employee, candidates[0], 'name'
        elif candidates or odoo_name_counts[key] > 1:
            yield employee, None, 'ambiguous'
        else:
            yield employee, None, 'unmatched'


def sync_odoo_profiles(employees: list[OdooEmployee] | None = None) -> SyncSummary:
    employees = fetch_current_employees() if employees is None else employees
    summary = SyncSummary(employees=len(employees))
    since = OdooSettings.get().new_users_since

    with transaction.atomic():
        for employee, user, reason in _match_users(employees):
            if user is None:
                if reason == 'ambiguous':
                    summary.ambiguous += 1
                else:
                    summary.unmatched += 1
                if len(summary.unmatched_examples) < 8:
                    summary.unmatched_examples.append(employee.full_name)
                continue
            summary.matched += 1
            title_key, company_key = normalize_text(employee.post_title), normalize_text(employee.company)
            OdooProfile.objects.update_or_create(user=user, defaults={
                'employee_id': employee.employee_id[:100], 'title': employee.post_title[:200], 'title_key': title_key[:200],
                'company': employee.company[:200], 'company_key': company_key[:200], 'phone': employee.phone[:60],
            })
            # In both systems and arrived after the cutoff: announce once, whatever happens later.
            if user.date_joined >= since:
                _, created = NewUserAlert.objects.get_or_create(user=user)
                summary.new_users += int(created)
    logger.info("Odoo sync: %s", summary)
    return summary
