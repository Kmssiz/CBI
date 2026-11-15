"""Merge the old-platform table (table 1) and the user's modifier workbook (table 2).

Usage:
  python tools/merge_tables.py <prefill.json> <metadata_entry_modifier.xlsm> <out.xlsx>

Table 1 = reports matched to the old platform, Type == Dashboard (from prefill.json).
Table 2 = rows of the modifier workbook that carry metadata (plus its Name/Description).
Rule per field: table 2 wins (it is manual work); an empty table-2 field is filled from
table 1; a real disagreement keeps table 2 and is listed on the "Conflicts" sheet.
New name = the workbook's `Name` column when filled, else the current name.
"""
import collections
import json
import sys

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L

import build_metadata_workbook as B

FIELDS = (("Type", "type"), ("Consolidé", "cons"), ("Société(s)", "soc"), ("Direction(s)", "dir"), ("Module(s)", "mod"))
ALL = "[Toutes les sociétés]"


def split(v):
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def poles_of(soc_cell: str) -> str:
    out = set()
    for t in split(soc_cell):
        if t.startswith("[Toutes"):
            out |= set(B.POLES)
        elif t.startswith("[Pôle ") and t.endswith("]"):
            out.add(t[len("[Pôle "):-1])
        elif t in B.SOCIETES:
            out.add(B.SOCIETES[t])
    return ", ".join(p for p in B.POLES if p in out)


def validate(row) -> list:
    bad = []
    for t in split(row["Société(s)"]):
        if not (t == ALL or (t.startswith("[Pôle ") and t[6:-1] in B.POLES) or t in B.SOCIETES):
            bad.append("société " + t)
    bad += ["direction " + d for d in split(row["Direction(s)"]) if d not in B.DIRECTIONS]
    bad += ["module " + m for m in split(row["Module(s)"]) if m not in B.MODULES]
    if row["Type"] and row["Type"] not in B.TYPES.values():
        bad.append("type " + row["Type"])
    return bad


def main(prefill_json, modifier_xlsm, out_xlsx):
    F = json.load(open(prefill_json, encoding="utf-8"))
    t1 = {f["pbirs_id"]: f for f in F
          if f["matched_old"] and f["fields"] and f["fields"].get("Type", {}).get("value") == "Dashboard"}
    ws = load_workbook(modifier_xlsm, read_only=True)["Reports"]
    rows = list(ws.iter_rows(values_only=True))
    keys = ("name", "path", "status", "missing", "type", "cons", "soc", "dir", "mod", "pole", "id", "newname", "desc")
    M = [dict(zip(keys, r[:13])) for r in rows[1:]]
    has = lambda m: any(m[k] for _, k in FIELDS)
    t2 = {m["id"]: m for m in M if has(m)}
    cur_name = {m["id"]: m["name"] for m in M}
    path_of = {m["id"]: m["path"] for m in M}
    mod_by_id = {m["id"]: m for m in M}

    merged, conflicts, problems = [], [], []
    for pid in sorted(set(t1) | set(t2), key=lambda i: (path_of[i] or "").lower()):
        a = t1.get(pid)            # table 1
        b = t2.get(pid)            # table 2
        base = mod_by_id[pid]
        vals, notes = {}, []
        for label, k in FIELDS:
            v1 = a["fields"].get(label, {}).get("value") if a else None
            v2 = b[k] if b else None
            v1 = (v1 or "").strip() if v1 else ""
            v2 = (str(v2).strip() if v2 else "")
            if v2 and v1 and sorted(split(v1)) != sorted(split(v2)):
                vals[label] = v2
                conflicts.append([path_of[pid], label, v2, v1])
                notes.append(f"{label}: kept table 2")
            elif v2:
                vals[label] = v2
            elif v1:
                vals[label] = v1
                if b:
                    notes.append(f"{label}: filled from table 1")
            else:
                vals[label] = ""
        newname = (base["newname"] or "").strip()
        row = {
            "Path": path_of[pid], "PBIRS ID": pid, "Current name": cur_name[pid],
            "New name (app)": newname or cur_name[pid],
            "Renamed": "Yes" if newname and newname != (cur_name[pid] or "").strip() else "No",
            "Description": (base["desc"] or "").strip(),
            **vals,
            "Source": "Both" if a and b else ("Table 1 (old platform)" if a else "Table 2 (workbook)"),
            "Notes": "; ".join(notes),
        }
        cons = row["Consolidé"] == "Oui"
        miss = [x for x in ("Type", "Société(s)", "Direction(s)", "Module(s)")
                if not row[x] and not (cons and x in ("Société(s)", "Direction(s)"))]
        row["Status"] = "Given" if not miss else ("Partial" if any(row[x] for x in ("Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)")) else "Not given")
        row["Missing fields"] = ", ".join(miss)
        row["Pôle(s) déduit(s)"] = poles_of(row["Société(s)"])
        bad = validate(row)
        if bad:
            problems.append([row["Path"], "; ".join(bad)])
        merged.append(row)

    dup = collections.Counter(r["New name (app)"] for r in merged)
    dup_rows = [[n, c] for n, c in dup.items() if c > 1]

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Merged"
    cols = ["Path", "PBIRS ID", "Current name", "New name (app)", "Renamed", "Description", "Type", "Consolidé",
            "Société(s)", "Direction(s)", "Module(s)", "Pôle(s) déduit(s)", "Status", "Missing fields", "Source", "Notes"]
    ws1.append(cols)
    fills = {"Given": "C6EFCE", "Partial": "FFEB9C", "Not given": "FFC7CE"}
    for r in merged:
        ws1.append([r[c] for c in cols])
        ws1.cell(ws1.max_row, cols.index("Status") + 1).fill = PatternFill("solid", fgColor=fills[r["Status"]])
        if r["Renamed"] == "Yes":
            ws1.cell(ws1.max_row, cols.index("New name (app)") + 1).fill = PatternFill("solid", fgColor="DDEBF7")
    for c in ws1[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    widths = dict(zip(cols, (70, 38, 38, 42, 9, 36, 14, 10, 44, 38, 24, 30, 11, 22, 22, 36)))
    for i, c in enumerate(cols, start=1):
        ws1.column_dimensions[L(i)].width = widths[c]
        for cell in ws1[L(i)][1:]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws1.freeze_panes = "B2"
    ws1.auto_filter.ref = ws1.dimensions

    ws2 = wb.create_sheet("Conflicts")
    ws2.append(["Path", "Field", "Kept (table 2 / your workbook)", "Table 1 (old platform) said"])
    for r in conflicts:
        ws2.append(r)
    ws3 = wb.create_sheet("Checks")
    ws3.append(["Check", "Result"])
    st = collections.Counter(r["Status"] for r in merged)
    src = collections.Counter(r["Source"] for r in merged)
    for line in [("Rows in merged table", len(merged)), ("From table 1 only", src["Table 1 (old platform)"]),
                 ("From table 2 only", src["Table 2 (workbook)"]), ("In both", src["Both"]),
                 ("Conflicts (kept table 2)", len(conflicts)), ("Reports to rename (app only)", sum(1 for r in merged if r["Renamed"] == "Yes")),
                 ("Status Given / Partial / Not given", f"{st['Given']} / {st['Partial']} / {st['Not given']}"),
                 ("Rows with an option name unknown to the platform", len(problems)),
                 ("New names used by more than one report", len(dup_rows))]:
        ws3.append(list(line))
    ws3.append([])
    ws3.append(["Unknown option names", "Path"])
    for p in problems:
        ws3.append([p[1], p[0]])
    ws3.append([])
    ws3.append(["Duplicate new names", "Count"])
    for d in dup_rows:
        ws3.append(d)
    for w in (ws2, ws3):
        for c in w[1]:
            c.font = Font(bold=True)
        for col, wd in zip("ABCD", (70, 40, 40, 40)):
            w.column_dimensions[col].width = wd
    wb.save(out_xlsx)
    print(json.dumps({"rows": len(merged), "sources": dict(src), "status": dict(st), "conflicts": len(conflicts),
                      "renamed": sum(1 for r in merged if r["Renamed"] == "Yes"), "unknown_option_rows": len(problems),
                      "duplicate_new_names": len(dup_rows)}, ensure_ascii=False))
    return problems, dup_rows


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(*sys.argv[1:4])
