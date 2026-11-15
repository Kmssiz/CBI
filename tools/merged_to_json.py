"""Turn the merged table into the JSON files used by the importers.

Usage:
  python tools/merged_to_json.py <metadata_merged.xlsx> <metadata_entry_modifier.xlsm> <out_dir>

Writes in <out_dir>:
  changes.json       rows for tools/import_report_metadata.py (metadata + renames)
  descriptions.json  rows for tools/push_pbirs_descriptions.py (non-empty descriptions)

The baseline of each report (what the database held when the workbook was exported) is read from
the hidden "Baseline" sheet of the workbook, exactly as workbook_to_json.py does.
"""
import json
import os
import sys
import unicodedata

from openpyxl import load_workbook

SEP = "|"
TYPES = {"Anomalie": "anomalie", "Bibliothèque": "bibliotheque", "Dashboard": "dashboard"}


def norm(v) -> str:
    return unicodedata.normalize("NFC", str(v if v is not None else "")).strip()


def split(v):
    out = []
    for x in norm(v).split(","):
        x = x.strip()
        if x and x not in out:
            out.append(x)
    return out


def main(merged, workbook, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    wbk = load_workbook(workbook, read_only=True)
    base = {}
    for row in wbk["Baseline"].iter_rows(min_row=2, values_only=True):
        pid, typ, cons, socs, dirs, mods = (norm(x) for x in row[:6])
        base[pid] = {"type": typ or None, "consolide": cons == "1",
                     "societes": [x for x in socs.split(SEP) if x],
                     "directions": [x for x in dirs.split(SEP) if x],
                     "modules": [x for x in mods.split(SEP) if x]}

    ws = load_workbook(merged, read_only=True)["Merged"]
    rows = list(ws.iter_rows(values_only=True))
    ix = {norm(h): i for i, h in enumerate(rows[0])}
    out, descs, warnings = [], [], []
    for r in rows[1:]:
        pid = norm(r[ix["PBIRS ID"]])
        if pid not in base:
            warnings.append(f"{pid}: no baseline, skipped")
            continue
        tokens = split(r[ix["Société(s)"]])
        typ = norm(r[ix["Type"]])
        if typ and typ not in TYPES:
            warnings.append(f"{pid}: unknown type {typ!r}")
        row = {
            "pbirs_id": pid, "name": norm(r[ix["New name (app)"]]), "baseline": base[pid],
            "selection": {
                "type": TYPES.get(typ), "consolide": norm(r[ix["Consolidé"]]) == "Oui",
                "all_societes": any(t.startswith("[Toutes") for t in tokens),
                "poles": [t[len("[Pôle "):-1] for t in tokens if t.startswith("[Pôle ") and t.endswith("]")],
                "societes": [t for t in tokens if not t.startswith("[")],
                "directions": split(r[ix["Direction(s)"]]), "modules": split(r[ix["Module(s)"]]),
            },
        }
        if norm(r[ix["Renamed"]]) == "Yes":
            row["rename"] = {"from": norm(r[ix["Current name"]]), "to": norm(r[ix["New name (app)"]])}
        out.append(row)
        d = norm(r[ix["Description"]])
        if d:
            descs.append({"pbirs_id": pid, "name": norm(r[ix["Current name"]]), "path": norm(r[ix["Path"]]),
                          "description": str(r[ix["Description"]]).strip()})
    json.dump({"rows": out}, open(os.path.join(out_dir, "changes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(descs, open(os.path.join(out_dir, "descriptions.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"changes.json: {len(out)} rows ({sum(1 for r in out if 'rename' in r)} renames) | "
          f"descriptions.json: {len(descs)} descriptions")
    for w in warnings:
        print("WARNING:", w)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(*sys.argv[1:4])
