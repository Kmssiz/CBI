"""
Learn each employee's job title, company and phone from Odoo, and spot NEW users.

A new user is someone who is new in BOTH systems: the directory account (LDAP, AD creation
date) was created recently AND the matching Odoo employee was hired recently
(settings.NEW_USER_MAX_AGE_DAYS). Odoo is what tells us who they are (job title, company,
phone); being old in either system means they are not new, however recently CBI saw them.
They are announced to the admins, who decide by hand what to grant. Nothing here ever grants
access.

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
from datetime import date, datetime, timedelta
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

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
    retired: int = 0            # waiting announcements withdrawn: not new in both systems after all
    unmatched_examples: list = field(default_factory=list)


def new_user_cutoff() -> datetime:
    return timezone.now() - timedelta(days=settings.NEW_USER_MAX_AGE_DAYS)


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def is_new_user(user, hire_date) -> bool:
    """New in the directory AND new in Odoo. An unknown date counts as not new: never guess."""
    cutoff = new_user_cutoff()
    hired = _as_date(hire_date)
    created = user.ldap_created_at or user.date_joined
    return bool(hired) and hired >= cutoff.date() and created >= cutoff


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

    not_new_ids = []
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
                'hire_date': _as_date(employee.hire_date),
            })
            # New in the directory AND in Odoo (and after the go-live cutoff): announce once, whatever happens later.
            if user.date_joined >= since and is_new_user(user, employee.hire_date):
                _, created = NewUserAlert.objects.get_or_create(user=user)
                summary.new_users += int(created)
            else:
                not_new_ids.append(user.pk)
        # Withdraw announcements nobody has read yet that turn out not to be new people (older rules
        # announced everyone who reached CBI recently). Read ones are history and stay.
        if not_new_ids:
            summary.retired, _ = NewUserAlert.objects.filter(user_id__in=not_new_ids, seen_at__isnull=True).delete()
    logger.info("Odoo sync: %s", summary)
    return summary
