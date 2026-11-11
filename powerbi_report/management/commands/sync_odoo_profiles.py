from django.core.management.base import BaseCommand

from powerbi_report.services.odoo_client import OdooError, is_configured
from powerbi_report.services.odoo_sync import sync_odoo_profiles


class Command(BaseCommand):
    help = (
        "Read job title, company and phone from Odoo for every CBI user, and flag new users "
        "(in LDAP and in Odoo) for the admins. Nothing is granted."
    )

    def handle(self, *args, **options):
        if not is_configured():
            self.stdout.write("Odoo is not configured (ODOO_DB_* in .env): nothing to do.")
            return
        try:
            summary = sync_odoo_profiles()
        except OdooError as exc:
            self.stderr.write(f"Odoo sync failed: {exc}")
            return
        self.stdout.write(
            f"Odoo: {summary.employees} employees, {summary.matched} matched "
            f"({summary.ambiguous} ambiguous, {summary.unmatched} unmatched); "
            f"{summary.new_users} new user(s) flagged."
        )
