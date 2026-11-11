"""Export every report (name, PBIRS path, metadata status and values) to CSV.

Standalone script: sets Django up itself, needs no deployment and no extra
package (stdlib only). The CSV is UTF-8 with BOM so Excel shows accents.

Usage:
    python export_report_metadata.py [output.csv]        (default: report_metadata.csv)

On the server (Docker), without rebuilding the image:
    docker cp tools/export_report_metadata.py PBI:/tmp/export_report_metadata.py
    docker exec PBI python /tmp/export_report_metadata.py /tmp/report_metadata.csv
    docker cp PBI:/tmp/report_metadata.csv .

Then build the formatted .xlsx on your PC (needs openpyxl):
    python tools/csv_to_xlsx.py report_metadata.csv
"""
import csv
import os
import sys

# Make the project importable whether the script sits in the repo or in /tmp.
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from powerbi_report.models import ReportRef  # noqa: E402

HEADERS = [
    "Report name",
    "Path",
    "Metadata status",
    "Missing fields",
    "Type",
    "Consolidé",
    "Pôle(s)",
    "Société(s)",
    "Direction(s)",
    "Module(s)",
    "Last edited by",
    "PBIRS ID",
]


def _names(manager) -> str:
    return ", ".join(sorted(o.name for o in manager.all()))


def build_rows() -> list:
    rows = []
    qs = (
        ReportRef.objects.select_related("modified_by")
        .prefetch_related("poles", "directions", "societes", "modules")
        .order_by("path", "name")
    )
    for r in qs:
        report_type = r.get_report_type_display() if r.report_type else ""
        poles = _names(r.poles)
        societes = _names(r.societes)
        directions = _names(r.directions)
        modules = _names(r.modules)

        given = bool(report_type or poles or societes or directions or modules or r.is_consolide)

        # Pôle is derived from Société, so it is not listed as a separate gap.
        missing = []
        if not report_type:
            missing.append("Type")
        if not societes and not r.is_consolide:
            missing.append("Société")
        if not directions and not r.is_consolide:
            missing.append("Direction")
        if not modules:
            missing.append("Module")

        if not given:
            status = "Not given"
        elif missing:
            status = "Partial"
        else:
            status = "Given"

        rows.append([
            r.name,
            r.path,
            status,
            ", ".join(missing),
            report_type,
            "Yes" if r.is_consolide else "No",
            poles,
            societes,
            directions,
            modules,
            r.modified_by.get_username() if r.modified_by else "",
            r.pbirs_id,
        ])
    return rows


def main() -> None:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "report_metadata.csv"
    rows = build_rows()
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADERS)
        w.writerows(rows)
    counts = {s: sum(1 for r in rows if r[2] == s) for s in ("Given", "Partial", "Not given")}
    print(f"Wrote {out_path}: {len(rows)} reports {counts}")


if __name__ == "__main__":
    main()
