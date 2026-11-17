"""
Export every identity (AD group or user) holding a permission in PBIRS, with the
members of each group, to an Excel workbook.

PBIRS only accepts the caller's own NTLM credentials (no service account here), so the
password is prompted locally and never stored or logged:

    python manage.py export_pbirs_groups --username <your_ad_login>
"""

import getpass
import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from requests_ntlm import HttpNtlmAuth

from powerbi_report.services.ldap_group_members import get_group_members, is_ldap_group
from powerbi_report.services.pbirs_servers import get_active_pbirs_server_urls
from users.models import CustomUser

logger = logging.getLogger("powerbi_report")

API = "/Reports/api/v2.0"
# (collection, label shown in Excel)
ITEM_COLLECTIONS = [
    ("Folders", "Dossier"),
    ("PowerBIReports", "Rapport Power BI"),
    ("Reports", "Rapport paginé"),
]


def _short(identity: str) -> str:
    """'DOMAIN\\name' -> 'name'."""
    return (identity or "").split("\\")[-1].strip()


def _roles(policy: dict) -> list[str]:
    return [r.get("Name", "") for r in policy.get("Roles", []) if r.get("Name")]


class PBIRSExporter:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.auth = HttpNtlmAuth(f"{settings.LDAP_DOMAIN}\\{username}", password)
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, endpoint: str) -> dict:
        response = self.session.get(f"{self.base_url}{endpoint}", timeout=60)
        if response.status_code == 401:
            raise CommandError("PBIRS a refusé les identifiants (401).")
        response.raise_for_status()
        return response.json()

    def list_items(self, collection: str) -> list[dict]:
        items: list[dict] = []
        url: Optional[str] = f"{API}/{collection}"
        while url:
            data = self._get(url)
            items.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            url = next_link.replace(self.base_url, "") if next_link else None
        return items

    def policies(self, collection: str, item_id: str) -> Optional[list[dict]]:
        try:
            return self._get(f"{API}/{collection}({item_id})/Policies").get("Policies", [])
        except requests.RequestException as exc:
            logger.warning("Policies unavailable for %s %s: %s", collection, item_id, exc.__class__.__name__)
            return None

    def system_policies(self) -> list[dict]:
        try:
            return self._get(f"{API}/System/Policies").get("Policies", [])
        except requests.RequestException:
            return []


class Command(BaseCommand):
    help = "Export all PBIRS groups (and their members) holding permissions to an Excel file."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="Your AD login (without domain).")
        parser.add_argument(
            "--service-account",
            action="store_true",
            help="Use LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD from the settings (read-only calls).",
        )
        parser.add_argument(
            "--output",
            default=f"pbirs_groups_users_{datetime.now():%Y%m%d}.xlsx",
            help="Output .xlsx path (default: current folder).",
        )
        parser.add_argument("--workers", type=int, default=8, help="Parallel PBIRS requests.")

    def handle(self, *args, **options):
        if options["service_account"]:
            username = settings.LDAP_SERVICE_USERNAME
            password = settings.LDAP_SERVICE_PASSWORD
            if not username or not password:
                raise CommandError("LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD are not configured.")
        else:
            username = options["username"] or input("AD login (without domain): ").strip()
            password = os.environ.get("PBIRS_EXPORT_PASSWORD") or getpass.getpass("Password: ")
        if not username or not password:
            raise CommandError("Login and password are required.")

        servers = get_active_pbirs_server_urls()
        if not servers:
            raise CommandError("No active PBIRS server configured.")

        # (server, scope_type, path) -> {identity: roles}
        grants: list[dict] = []
        for base_url in servers:
            self.stdout.write(f"Reading PBIRS permissions from {base_url} ...")
            grants.extend(self._collect(PBIRSExporter(base_url, username, password), base_url, options["workers"]))
        password = ""

        self._write_workbook(grants, options["output"])
        self.stdout.write(self.style.SUCCESS(f"Written: {os.path.abspath(options['output'])}"))

    # ------------------------------------------------------------------ PBIRS
    def _collect(self, client: PBIRSExporter, base_url: str, workers: int) -> list[dict]:
        targets: list[tuple[str, str, str, str]] = []  # collection, label, id, path
        for collection, label in ITEM_COLLECTIONS:
            try:
                items = client.list_items(collection)
            except requests.RequestException as exc:
                self.stderr.write(f"  {collection}: skipped ({exc.__class__.__name__})")
                continue
            self.stdout.write(f"  {collection}: {len(items)}")
            targets.extend((collection, label, i.get("Id"), i.get("Path") or i.get("Name", "")) for i in items)

        def fetch(target):
            collection, label, item_id, path = target
            return target, client.policies(collection, item_id)

        grants: list[dict] = []
        failed = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for (collection, label, item_id, path), policies in pool.map(fetch, targets):
                if policies is None:
                    failed += 1
                    continue
                for policy in policies:
                    identity = policy.get("GroupUserName") or policy.get("UserName") or ""
                    grants.append(
                        {"server": base_url, "scope": label, "path": path,
                         "identity": identity, "roles": _roles(policy)}
                    )
        for policy in client.system_policies():
            grants.append(
                {"server": base_url, "scope": "Site (système)", "path": "/",
                 "identity": policy.get("GroupUserName") or "", "roles": _roles(policy)}
            )
        if failed:
            self.stderr.write(f"  WARNING: policies unreadable for {failed} items (no access?).")
        return grants

    # ------------------------------------------------------------------ Excel
    def _write_workbook(self, grants: list[dict], output: str) -> None:
        identities = sorted({g["identity"] for g in grants if g["identity"]}, key=str.casefold)
        self.stdout.write(f"{len(identities)} distinct identities; resolving groups via the LDAP API ...")

        kind: dict[str, str] = {}
        members: dict[str, list[str]] = {}

        def resolve(identity: str):
            try:
                name = _short(identity)
                if is_ldap_group(name):
                    return identity, "Groupe AD", sorted(get_group_members(name), key=str.casefold)
                if identity.upper().startswith(("BUILTIN\\", "NT AUTHORITY\\")):
                    return identity, "Groupe local / intégré", None
                return identity, "Utilisateur", None
            finally:
                connection.close()  # one DB connection per worker thread

        with ThreadPoolExecutor(max_workers=6) as pool:
            for done, (identity, k, ms) in enumerate(pool.map(resolve, identities), start=1):
                kind[identity] = k
                if ms is not None:
                    members[identity] = ms
                if done % 20 == 0 or done == len(identities):
                    self.stdout.write(f"  resolved {done}/{len(identities)}")

        all_members = {m for ms in members.values() for m in ms} | {
            _short(i) for i in identities if kind[i] == "Utilisateur"}
        known = {
            u.ad2000: u
            for u in CustomUser.objects.filter(ad2000__in=all_members).only(
                "ad2000", "first_name", "last_name", "email", "societe", "direction"
            )
        }

        by_identity: dict[str, list[dict]] = defaultdict(list)
        for g in grants:
            by_identity[g["identity"]].append(g)

        wb = Workbook()

        # --- Sheet 1: summary per group
        ws = wb.active
        ws.title = "Groupes"
        ws.append(["Groupe / identité", "Type", "Nb membres", "Nb éléments PBIRS", "Rôles distincts"])
        for identity in identities:
            gs = by_identity[identity]
            roles = sorted({r for g in gs for r in g["roles"]})
            ws.append([identity, kind[identity],
                       len(members[identity]) if identity in members else "",
                       len({(g["server"], g["path"]) for g in gs}), ", ".join(roles)])

        # --- Sheet 2: group -> users
        ws2 = wb.create_sheet("Membres des groupes")
        # The LDAP API returns display names (e.g. "Abdelkader BEN KHEDDA"), not logins.
        ws2.append(["Groupe", "Membre (tel que renvoyé par l'API LDAP)"])
        for identity in identities:
            if identity not in members:
                continue
            if not members[identity]:
                ws2.append([identity, "(aucun membre résolu)"])
            for m in members[identity]:
                ws2.append([identity, m])

        # --- Sheet 3: permissions per group/user on each item
        ws3 = wb.create_sheet("Permissions")
        ws3.append(["Groupe / identité", "Type", "Serveur", "Portée", "Chemin", "Rôles"])
        for g in sorted(grants, key=lambda x: (x["identity"].casefold(), x["path"].casefold())):
            ws3.append([g["identity"], kind.get(g["identity"], ""), g["server"], g["scope"], g["path"],
                        ", ".join(g["roles"])])

        # --- Sheet 4: individuals with a direct permission
        ws4 = wb.create_sheet("Utilisateurs directs")
        ws4.append(["Utilisateur", "Nom complet", "Nb éléments", "Rôles distincts"])
        for identity in identities:
            if kind[identity] != "Utilisateur":
                continue
            gs = by_identity[identity]
            u = known.get(_short(identity))
            ws4.append([identity, u.get_full_name() if u else "", len({(g["server"], g["path"]) for g in gs}),
                        ", ".join(sorted({r for g in gs for r in g["roles"]}))])

        header_fill = PatternFill("solid", fgColor="1F4E78")
        for sheet in wb.worksheets:
            for cell in sheet[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = header_fill
                cell.alignment = Alignment(vertical="center")
            sheet.freeze_panes = "A2"
            sheet.auto_filter.ref = sheet.dimensions
            for idx, col in enumerate(sheet.columns, start=1):
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                sheet.column_dimensions[get_column_letter(idx)].width = min(width + 2, 70)

        wb.save(output)
