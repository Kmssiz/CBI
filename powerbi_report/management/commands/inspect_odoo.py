import re
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from powerbi_report.services.odoo_client import OdooError, connect, fetch_current_employees
from powerbi_report.services.odoo_sync import _match_users
from users.models import CustomUser


def _mask_id(value: str) -> str:
    """H0017549 -> H#######: enough to see the pattern, not the person."""
    return re.sub(r'\d', '#', value)


def _mask_name(value: str) -> str:
    return ' '.join(word[:1] + '*' * (len(word) - 1) for word in value.split())


class Command(BaseCommand):
    help = (
        "Read-only Odoo diagnostics. Default: list tables/views whose name matches --match. "
        "With --test: run the employee query and print counts, the most common job titles and "
        "companies, masked samples, and how many employees match a CBI user."
    )

    def add_arguments(self, parser):
        parser.add_argument('--match', default='employee', help="Word to look for in table/view names (default: employee)")
        parser.add_argument('--test', action='store_true', help="Run the employee query and report what it returns")

    def handle(self, *args, **options):
        if options['test']:
            return self._test()
        try:
            connection = connect()
        except OdooError as exc:
            raise CommandError(str(exc))
        with connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_name ILIKE %s AND table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name
                """,
                [f"%{options['match']}%"],
            )
            found = cursor.fetchall()
            if not found:
                self.stdout.write(f"Nothing matching {options['match']!r} is visible to this account.")
            for schema, name, kind in found:
                self.stdout.write(f"\n== {schema}.{name} ({kind}) ==")
                cursor.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    [schema, name],
                )
                self.stdout.write(", ".join(f"{c} [{t}]" for c, t in cursor.fetchall()))
        connection.close()

    def _test(self):
        try:
            employees = fetch_current_employees()
        except OdooError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f"Employees returned (active, with a name and a job title): {len(employees)}")
        with_company = sum(1 for e in employees if e.company)
        self.stdout.write(f"  with a company: {with_company}   with an id: {sum(1 for e in employees if e.employee_id)}")

        self.stdout.write("\nTop job titles:")
        for title, count in Counter(e.post_title for e in employees).most_common(15):
            self.stdout.write(f"  {count:4d}  {title}")
        self.stdout.write("\nCompanies:")
        for company, count in Counter(e.company or '(none)' for e in employees).most_common():
            self.stdout.write(f"  {count:4d}  {company}")

        self.stdout.write("\nSample (ids and names masked):")
        for e in employees[:5]:
            self.stdout.write(f"  {_mask_id(e.employee_id)} | {_mask_name(e.full_name)} | {e.post_title} | {e.company}")

        reasons = Counter(reason for _, _, reason in _match_users(employees))
        users = CustomUser.objects.filter(is_active=True).count()
        self.stdout.write(
            f"\nMatching with the {users} active CBI users: by id {reasons['id']}, by unique name {reasons['name']}, "
            f"ambiguous {reasons['ambiguous']}, not found {reasons['unmatched']}."
        )
