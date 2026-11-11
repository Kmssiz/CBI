"""Convert the CSV from export_report_metadata.py into a formatted .xlsx.

Usage: python tools/csv_to_xlsx.py report_metadata.csv|.xlsx [output.xlsx]
Requires: pip install openpyxl
"""
import csv
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

FILLS = {
    "Given": "C6EFCE",
    "Partial": "FFEB9C",
    "Not given": "FFC7CE",
}


def main(src: str, dst: str | None = None) -> None:
    src_path = Path(src)
    dst_path = Path(dst) if dst else src_path.with_suffix(".xlsx")
    if src_path.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook

        rows = [
            ["" if v is None else str(v) for v in r]
            for r in load_workbook(src_path, read_only=True).worksheets[0].iter_rows(values_only=True)
        ]
    else:
        with src_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))

    wb = Workbook()
    ws = wb.active
    ws.title = "Reports"
    for row in rows:
        ws.append(row)

    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    status_col = rows[0].index("Metadata status") + 1
    for r in range(2, ws.max_row + 1):
        c = ws.cell(row=r, column=status_col)
        if c.value in FILLS:
            c.fill = PatternFill("solid", fgColor=FILLS[c.value])

    for i, col in enumerate(ws.columns, start=1):
        longest = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 60)

    # Small summary sheet
    summary = wb.create_sheet("Summary")
    statuses = [r[status_col - 1] for r in rows[1:]]
    summary.append(["Status", "Reports"])
    for label in ("Given", "Partial", "Not given"):
        summary.append([label, statuses.count(label)])
    summary.append(["Total", len(statuses)])
    for cell in summary[1]:
        cell.font = Font(bold=True)
    summary.column_dimensions["A"].width = 16

    # Status per top-level PBIRS folder (first path segment)
    path_col = rows[0].index("Path")
    by_folder = {}
    for r in rows[1:]:
        top = r[path_col].strip("/").split("/")[0] or "(root)"
        by_folder.setdefault(top, {"Given": 0, "Partial": 0, "Not given": 0})[r[status_col - 1]] += 1
    summary.append([])
    summary.append(["Top-level folder", "Given", "Partial", "Not given", "Total"])
    for cell in summary[summary.max_row]:
        cell.font = Font(bold=True)
    for top, c in sorted(by_folder.items()):
        summary.append([top, c["Given"], c["Partial"], c["Not given"], sum(c.values())])
    summary.column_dimensions["A"].width = 32

    wb.save(dst_path)
    print(f"Wrote {dst_path} ({len(rows) - 1} reports)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
