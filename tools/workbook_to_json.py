"""Read the filled data-entry workbook and write the JSON the importer consumes.

Usage: python tools/workbook_to_json.py metadata_entry.xlsx [changes.json]

Only the entry columns are read (formula columns are ignored). The hidden
Baseline sheet is carried along so the importer can detect conflicts.
"""
import json
import sys
import unicodedata

from openpyxl import load_workbook

SEP = "|"
TYPES = {"Anomalie": "anomalie", "Bibliothèque": "bibliotheque", "Dashboard": "dashboard"}


def norm(v) -> str:
    return unicodedata.normalize("NFC", str(v if v is not None else "")).strip()


def main(src: str, dst: str = "changes.json") -> None:
    wb = load_workbook(src)  # formulas stay as text, which is fine: we skip those columns
    ws = wb["Reports"]
    header = [norm(c.value) for c in ws[1]]
    col = {h: i for i, h in enumerate(header)}

    def group(prefix):
        return [i for h, i in col.items() if h.startswith(prefix)]

    cells_layout = "Société(s)" in col  # macro layout: one comma-separated cell per field
    if not cells_layout:
        pole_cols, soc_cols = group("Pôle entier"), group("Société")
        dir_cols, mod_cols = group("Direction"), group("Module")

    base = {}
    for row in wb["Baseline"].iter_rows(min_row=2, values_only=True):
        pid, typ, cons, socs, dirs, mods = (norm(x) for x in row[:6])
        base[pid] = {
            "type": typ or None, "consolide": cons == "1",
            "societes": [x for x in socs.split(SEP) if x],
            "directions": [x for x in dirs.split(SEP) if x],
            "modules": [x for x in mods.split(SEP) if x],
        }

    warnings, rows, seen = [], [], set()
    for r in ws.iter_rows(min_row=2, values_only=True):
        pid = norm(r[col["PBIRS ID"]])
        if not pid:
            continue
        if pid in seen:
            warnings.append(f"Duplicate PBIRS ID {pid} ({norm(r[col['Report name']])}): both rows ignored")
            continue
        seen.add(pid)
        if pid not in base:
            warnings.append(f"{pid} ({norm(r[col['Report name']])}) has no baseline: skipped")
            continue

        def vals(cols):
            out = []
            for i in cols:
                v = norm(r[i])
                if v and v not in out:
                    out.append(v)
            return out

        def split_cell(name):
            out = []
            for v in norm(r[col[name]]).split(","):
                v = v.strip()
                if v and v not in out:
                    out.append(v)
            return out

        typ = norm(r[col["Type"]])
        if typ and typ not in TYPES:
            warnings.append(f"{norm(r[col['Report name']])}: unknown type {typ!r}")
        if cells_layout:
            tokens = split_cell("Société(s)")
            all_soc = any(t.startswith("[Toutes") for t in tokens)
            poles = [t[len("[Pôle "):-1] for t in tokens if t.startswith("[Pôle ") and t.endswith("]")]
            socs = [t for t in tokens if not t.startswith("[")]
            dirs, mods = split_cell("Direction(s)"), split_cell("Module(s)")
        else:
            all_soc = norm(r[col["Toutes les sociétés"]]) == "Oui"
            poles, socs, dirs, mods = vals(pole_cols), vals(soc_cols), vals(dir_cols), vals(mod_cols)
        rows.append({
            "pbirs_id": pid,
            "name": norm(r[col["Report name"]]),
            "baseline": base[pid],
            "selection": {
                "type": TYPES.get(typ),
                "consolide": norm(r[col["Consolidé"]]) == "Oui",
                "all_societes": all_soc, "poles": poles, "societes": socs,
                "directions": dirs, "modules": mods,
            },
        })

    with open(dst, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows}, fh, ensure_ascii=False, indent=1)
    print(f"Wrote {dst}: {len(rows)} rows")
    for w in warnings:
        print("WARNING:", w)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
