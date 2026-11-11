"""Inject report metadata from changes.json (made by workbook_to_json.py) into the DB.

Standalone, stdlib + Django only. DRY RUN by default; nothing is written
unless --apply is given.

    python import_report_metadata.py changes.json            # preview
    python import_report_metadata.py changes.json --apply    # write
    python import_report_metadata.py changes.json --apply --user someadmin

Safety rules
  * Rows are matched on pbirs_id.
  * A row is written only if it differs from its baseline (what the DB held when
    the workbook was generated) AND the DB still equals that baseline. If the
    report was edited on the platform meanwhile, it is reported as a CONFLICT
    and left alone.
  * Rows identical to their baseline are ignored; rows emptied in Excel are
    never cleared (clear them on the platform).
  * Unknown option names abort the whole run before anything is written.
  * Writes mirror powerbi_report.views.update_report_metadata_local: M2M sets,
    pôles derived from the sociétés' parent, legacy text fields rewritten.
"""
import json
import os
import sys

sys.path.insert(0, os.getcwd())
# Never crash on an odd character in a report name (e.g. a stray control char).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from powerbi_report.models import MetadataOption, ReportRef  # noqa: E402


def state_from_ref(ref) -> dict:
    return {
        "type": ref.report_type or None,
        "consolide": bool(ref.is_consolide),
        "societes": sorted(o.name for o in ref.societes.all()),
        "directions": sorted(o.name for o in ref.directions.all()),
        "modules": sorted(o.name for o in ref.modules.all()),
    }


def norm_state(s: dict) -> dict:
    return {
        "type": s.get("type") or None,
        "consolide": bool(s.get("consolide")),
        "societes": sorted(set(s.get("societes", []))),
        "directions": sorted(set(s.get("directions", []))),
        "modules": sorted(set(s.get("modules", []))),
    }


def is_blank(s: dict) -> bool:
    return not (s["type"] or s["consolide"] or s["societes"] or s["directions"] or s["modules"])


def describe(s: dict) -> str:
    return (f"type={s['type']} consolide={s['consolide']} soc={len(s['societes'])} "
            f"dir={s['directions']} mod={s['modules']}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_ = "--apply" in sys.argv
    user = None
    if "--user" in sys.argv:
        user = sys.argv[sys.argv.index("--user") + 1]
        args = [a for a in args if a != user]
    if not args:
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8-sig") as fh:
        rows = json.load(fh)["rows"]

    options = {(o.option_type, o.name): o for o in MetadataOption.objects.select_related("parent")}
    societe_names = [n for (t, n) in options if t == "societe"]
    pole_members = {}
    for (t, n), o in options.items():
        if t == "societe" and o.parent_id:
            pole_members.setdefault(o.parent.name, set()).add(n)

    modified_by = None
    if user:
        from users.models import CustomUser
        modified_by = CustomUser.objects.filter(username=user).first()
        if not modified_by:
            print(f"ERROR: user {user!r} not found")
            return 1

    # 1. Validate every name and expand selections ----------------------------
    errors, plan = [], []
    for row in rows:
        sel = row["selection"]
        label = f"{row['name']} [{row['pbirs_id']}]"
        for kind, names in (("pole", sel["poles"]), ("societe", sel["societes"]),
                            ("direction", sel["directions"]), ("module", sel["modules"])):
            for n in names:
                if (kind, n) not in options:
                    errors.append(f"{label}: unknown {kind} {n!r}")
        societes = set(sel["societes"])
        if sel["all_societes"]:
            societes |= set(societe_names)
        for p in sel["poles"]:
            societes |= pole_members.get(p, set())
        wanted = norm_state({"type": sel["type"], "consolide": sel["consolide"], "societes": list(societes),
                             "directions": sel["directions"], "modules": sel["modules"]})
        plan.append((row, wanted, norm_state(row["baseline"])))
    if errors:
        print("ABORTED, nothing written. Fix these first:")
        for e in errors:
            print("  -", e)
        return 1

    # 2. Classify -------------------------------------------------------------
    unchanged = cleared = missing = 0
    conflicts, todo = [], []
    refs = {r.pbirs_id: r for r in ReportRef.objects.prefetch_related("societes", "directions", "modules")}
    for row, wanted, base in plan:
        if wanted == base:
            unchanged += 1
            continue
        if is_blank(wanted):
            cleared += 1
            print(f"SKIP (emptied in Excel, not applied): {row['name']}")
            continue
        ref = refs.get(row["pbirs_id"])
        if ref is None:
            missing += 1
            print(f"SKIP (not in database): {row['name']} [{row['pbirs_id']}]")
            continue
        current = norm_state(state_from_ref(ref))
        if current == wanted:
            unchanged += 1  # already applied (e.g. the same file imported twice)
            continue
        if current != base:
            conflicts.append((row, base, current, wanted))
            continue
        todo.append((ref, row, current, wanted))

    # 3. Write (or preview) ---------------------------------------------------
    with transaction.atomic():
        for ref, row, current, wanted in todo:
            print(f"CHANGE {row['name']}\n   from: {describe(current)}\n   to:   {describe(wanted)}")
            if not apply_:
                continue
            ref.report_type = wanted["type"]
            ref.is_consolide = wanted["consolide"]
            if modified_by:
                ref.modified_by = modified_by
            ref.save()
            soc_objs = [options[("societe", n)] for n in wanted["societes"]]
            ref.societes.set(soc_objs)
            ref.poles.set({o.parent_id for o in soc_objs if o.parent_id})
            ref.directions.set([options[("direction", n)] for n in wanted["directions"]])
            ref.modules.set([options[("module", n)] for n in wanted["modules"]])
            # legacy text fields, same order as the view (.all() ordered by name)
            ref.societe = ", ".join(sorted(o.name for o in ref.societes.all())) or None
            ref.pole = ", ".join(sorted(o.name for o in ref.poles.all())) or None
            ref.direction = ", ".join(sorted(o.name for o in ref.directions.all())) or None
            ref.save()

    for row, base, current, wanted in conflicts:
        print(f"CONFLICT (edited on the platform since the export, NOT written): {row['name']}\n"
              f"   at export: {describe(base)}\n   now in DB: {describe(current)}\n   in Excel:  {describe(wanted)}")

    verb = "written" if apply_ else "would be written (dry run, use --apply)"
    print(f"\nSummary: {len(todo)} {verb}, {len(conflicts)} conflicts, {unchanged} unchanged/already applied, "
          f"{cleared} emptied-skipped, {missing} not found")
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
