"""Set report DESCRIPTIONS in PBIRS (and mirror them in the app) from descriptions.json.

Run it yourself, inside the app container. Login: either YOUR PBIRS login (--user, it prompts for the
password; nothing is stored) or the app's service account (--service-account: the container already
holds LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD from .env, nothing to type). It only ever
touches the Description property: names and paths in PBIRS are never changed.

    docker cp tools/push_pbirs_descriptions.py PBI:/tmp/push_pbirs_descriptions.py
    docker cp descriptions.json PBI:/tmp/descriptions.json
    docker exec -it PBI python /tmp/push_pbirs_descriptions.py /tmp/descriptions.json --service-account
    docker exec -it PBI python /tmp/push_pbirs_descriptions.py /tmp/descriptions.json --service-account --apply

Without --apply it is a DRY RUN: it only READS each report's current description from PBIRS, writes
a backup file with the current values (so a change can be undone) and lists what would change.
With --apply it asks for confirmation, then PATCHes {"Description": ...} on
{server}/Reports/api/v2.0/PowerBIReports({id}) (the same call the app's "edit description" uses) and
copies the description into ReportRef.description like the app does.

Options: --server URL   force the PBIRS base URL (default: the report's own server, as the app does)
         --yes          skip the confirmation (--apply only)
         --no-auth      no login (only for tests against a fake server)
"""
import csv
import getpass
import json
import os
import sys
import time

sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(errors="replace")

import django  # noqa: E402

django.setup()

import requests  # noqa: E402
from django.conf import settings  # noqa: E402

from powerbi_report.models import ReportRef  # noqa: E402


def opt(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_, yes, no_auth = "--apply" in sys.argv, "--yes" in sys.argv, "--no-auth" in sys.argv
    server_opt, user = opt("--server"), opt("--user")
    args = [a for a in args if a not in (server_opt, user)]
    if not args:
        print(__doc__)
        return 2
    items = json.load(open(args[0], encoding="utf-8-sig"))

    auth = None
    if not no_auth:
        from requests_ntlm import HttpNtlmAuth
        if "--service-account" in sys.argv:
            # Same account and settings the app's permission sync already uses against PBIRS.
            svc_user, svc_pass = settings.LDAP_SERVICE_USERNAME, settings.LDAP_SERVICE_PASSWORD
            if not all([svc_user, svc_pass, settings.LDAP_DOMAIN]):
                print("ERROR: LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD / LDAP_DOMAIN are not configured.")
                return 2
            auth = HttpNtlmAuth(f"{settings.LDAP_DOMAIN}\\{svc_user}", svc_pass)
            print(f"Using the service account {settings.LDAP_DOMAIN}\\{svc_user} (password taken from the app's "
                  f"settings, never displayed)")
        else:
            if not user:
                print("ERROR: give --user <your login> (the password is asked next) or --service-account.")
                return 2
            auth = HttpNtlmAuth(f"{settings.LDAP_DOMAIN}\\{user}", getpass.getpass(f"PBIRS password for {user}: "))
    http = requests.Session()
    http.auth = auth

    def base_url(pid):
        return (server_opt or ReportRef.get_server_url(pid)).rstrip("/")

    plan, same_items, failed = [], [], []
    for it in items:                                  # READ-ONLY pass
        url = f"{base_url(it['pbirs_id'])}/Reports/api/v2.0/PowerBIReports({it['pbirs_id']})"
        try:
            r = http.get(url, timeout=30)
            r.raise_for_status()
            current = (r.json().get("Description") or "")
        except (requests.exceptions.RequestException, ValueError) as exc:
            failed.append((it, f"cannot read: {exc}"))
            continue
        if current.strip() == it["description"].strip():
            same_items.append(it)
        else:
            plan.append((it, url, current))

    backup = f"/tmp/pbirs_descriptions_backup_{time.strftime('%Y%m%d_%H%M%S')}.csv" if os.name != "nt" else \
        f"pbirs_descriptions_backup_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    with open(backup, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["pbirs_id", "path", "current_description_in_pbirs"])
        for it, _, current in plan:
            w.writerow([it["pbirs_id"], it["path"], current])
    print(f"Backup of the current descriptions: {backup}")

    for it, _, current in plan:
        print(f"WILL SET {it['path']}\n   now: {current!r}\n   new: {it['description']!r}")
    for it, why in failed:
        print(f"FAILED {it['path']}: {why}")
    print(f"\n{len(plan)} to change, {len(same_items)} already identical, {len(failed)} could not be read")

    if not apply_:
        print("Dry run: nothing written. Add --apply to write.")
        return 1 if failed else 0
    for it in same_items:   # PBIRS already has it: just make sure the app's local copy matches
        ReportRef.objects.filter(pbirs_id=it["pbirs_id"]).update(description=it["description"])
    if not plan:
        return 1 if failed else 0
    if not yes and input(f"Write {len(plan)} descriptions to PBIRS? Type YES: ").strip() != "YES":
        print("Cancelled, nothing written.")
        return 1

    done, errors = 0, []
    for it, url, _ in plan:                           # WRITE pass: Description only
        try:
            r = http.patch(url, json={"Description": it["description"]},
                           headers={"Content-Type": "application/json"}, timeout=30)
            r.raise_for_status()
        except requests.exceptions.RequestException as exc:
            errors.append((it, str(exc)))
            continue
        ReportRef.objects.filter(pbirs_id=it["pbirs_id"]).update(description=it["description"])
        done += 1
        print(f"OK {it['path']}")
    for it, why in errors:
        print(f"ERROR {it['path']}: {why}")
    print(f"\nWritten: {done}, errors: {len(errors)}. Undo with the backup file if needed.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
