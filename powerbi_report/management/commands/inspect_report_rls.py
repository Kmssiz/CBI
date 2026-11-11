import getpass
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from requests import Session
from requests_ntlm import HttpNtlmAuth


class Command(BaseCommand):
    help = (
        "Read-only: print a report's RLS roles and role assignments exactly as PBIRS returns them. "
        "Does not use the CBI database. Nothing is written to PBIRS."
    )

    def add_arguments(self, parser):
        parser.add_argument('report', help="Report name (partial match, searched in PBIRS)")
        parser.add_argument('--server-url', help="PBIRS base URL (default: POWERBI_REPORT_SERVER_URL in .env)")
        parser.add_argument('--username', help="Your Windows username (without domain); asks for the password")
        parser.add_argument(
            '--service-account', action='store_true',
            help="Use LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD from .env instead of typing a password",
        )

    def handle(self, *args, **options):
        base = (options['server_url'] or settings.POWERBI_REPORT_SERVER_URL or '').strip().rstrip('/')
        if base.lower().endswith('/reports'):
            base = base[:-len('/reports')]  # the API path below already adds /Reports/api/v2.0
        if not base:
            raise CommandError("No PBIRS URL: pass --server-url http://your-pbirs-host")

        if options['service_account']:
            username, password = settings.LDAP_SERVICE_USERNAME, settings.LDAP_SERVICE_PASSWORD
            if not (username and password):
                raise CommandError("LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD are not set in .env")
        elif options['username']:
            username, password = options['username'], getpass.getpass("Password: ")
        else:
            raise CommandError("Pass --service-account or --username <windows user>")

        session = Session()
        session.auth = HttpNtlmAuth(f"{settings.LDAP_DOMAIN}\\{username}", password)
        session.headers['Accept'] = 'application/json'
        api = f"{base}/Reports/api/v2.0"

        term = options['report']
        found = session.get(f"{api}/PowerBIReports", params={'$filter': f"contains(Name,'{term}')"}, timeout=30)
        self.stdout.write(f"Search for {term!r}: HTTP {found.status_code}")
        if found.status_code != 200:
            raise CommandError(found.text[:300])
        reports = found.json().get('value', [])
        if len(reports) != 1:
            names = ", ".join(r.get('Name', '?') for r in reports[:10])
            raise CommandError(f"{len(reports)} reports match {term!r}: {names}")
        report = reports[0]
        self.stdout.write(f"Report: {report.get('Name')}  Id={report.get('Id')}  Path={report.get('Path')}")

        for suffix in ('DataModelRoles', 'DataModelRoleAssignments'):
            response = session.get(f"{api}/PowerBIReports({report['Id']})/{suffix}", timeout=30)
            self.stdout.write(f"\n== {suffix} (HTTP {response.status_code}) ==")
            try:
                self.stdout.write(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except ValueError:
                self.stdout.write(response.text[:500])
