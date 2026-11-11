"""Build the MACRO flavour of the data-entry workbook (multi-select dropdown cells).

Usage: python tools/build_metadata_workbook_macro.py pbi.xlsx [metadata_entry.xlsx]

Same data and safeguards as build_metadata_workbook.py, but one cell per field
(Société(s), Direction(s), Module(s)) holding a comma-separated list. The
companion VBA (tools/multiselect.vba) makes each dropdown pick ADD to the cell
(and a second pick of the same value REMOVE it). Without the macro the cell is
a plain single-value dropdown.

Shortcut entries in the Société(s) dropdown:
    [Toutes les sociétés]   every société
    [Pôle Service] ...      every société of that pôle
"""
import sys

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L
from openpyxl.worksheet.datavalidation import DataValidation

import build_metadata_workbook as B  # constants and helpers shared with the slot layout

ALL_TOKEN = "[Toutes les sociétés]"


def pole_token(p: str) -> str:
    return f"[Pôle {p}]"


def main(src: str, dst: str = "metadata_entry.xlsx") -> None:
    reports = B.read_export(src)
    for r in reports:
        for s in r["societes"]:
            assert s in B.SOCIETES, f"Unknown société {s!r} in {r['name']}"
        for d in r["directions"]:
            assert d in B.DIRECTIONS, f"Unknown direction {d!r} in {r['name']}"
        for m in r["modules"]:
            assert m in B.MODULES, f"Unknown module {m!r} in {r['name']}"

    wb = Workbook()
    ws = wb.active
    ws.title = "Reports"
    lists = wb.create_sheet("Lists")
    base = wb.create_sheet("Baseline")
    guide = wb.create_sheet("Read me", 0)

    # Lists: B/C are société -> pôle (used by the formula); F is the dropdown source
    soc_choices = [ALL_TOKEN] + [pole_token(p) for p in B.POLES] + list(B.SOCIETES)
    list_cols = {
        "Type": list(B.TYPES.values()), "Société": list(B.SOCIETES), "Pôle de la société": list(B.SOCIETES.values()),
        "Pôle": B.POLES, "Direction": B.DIRECTIONS, "Module": B.MODULES, "Société (choix)": soc_choices,
    }
    for c, (h, vals) in enumerate(list_cols.items(), start=1):
        lists.cell(1, c, h).font = Font(bold=True)
        for i, v in enumerate(vals, start=2):
            lists.cell(i, c, v)
        lists.column_dimensions[L(c)].width = 28
    lists.cell(1, 9, "Do not edit: the dropdowns read these lists.").font = Font(italic=True)

    def rng(name: str) -> str:
        c = list(list_cols).index(name) + 1
        return f"Lists!${L(c)}$2:${L(c)}${len(list_cols[name]) + 1}"

    cols = ["Report name", "Path", "Status", "Missing fields", "Type", "Consolidé",
            "Société(s)", "Direction(s)", "Module(s)", "Pôle(s) déduit(s)", "PBIRS ID"]
    ix = {n: i + 1 for i, n in enumerate(cols)}
    ws.append(cols)
    colors = {"Report name": "1F4E78", "Path": "1F4E78", "PBIRS ID": "1F4E78", "Status": "595959",
              "Missing fields": "595959", "Pôle(s) déduit(s)": "595959", "Type": "2E75B6", "Consolidé": "2E75B6",
              "Société(s)": "C55A11", "Direction(s)": "548235", "Module(s)": "7030A0"}
    for n, name in enumerate(cols, start=1):
        c = ws.cell(1, n)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=colors[name])
        c.alignment = Alignment(wrap_text=True, vertical="center")

    cT, cC, cS, cD, cM = (L(ix[k]) for k in ("Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)"))
    cSt, cMi, cPo, cId = (L(ix[k]) for k in ("Status", "Missing fields", "Pôle(s) déduit(s)", "PBIRS ID"))
    wrap = Alignment(wrap_text=True, vertical="top")

    for n, r in enumerate(reports, start=2):
        all_flag, poles, socs = B.compress_societes(set(r["societes"]))
        soc_cell = ([ALL_TOKEN] if all_flag else [pole_token(p) for p in poles] + socs)
        ws.cell(n, ix["Report name"], r["name"])
        ws.cell(n, ix["Path"], r["path"])
        ws.cell(n, ix["PBIRS ID"], r["pbirs_id"])
        if r["type"]:
            ws.cell(n, ix["Type"], B.TYPES[r["type"]])
        if r["consolide"]:
            ws.cell(n, ix["Consolidé"], "Oui")
        if soc_cell:
            ws.cell(n, ix["Société(s)"], ", ".join(soc_cell))
        if r["directions"]:
            ws.cell(n, ix["Direction(s)"], ", ".join(sorted(r["directions"])))
        if r["modules"]:
            ws.cell(n, ix["Module(s)"], ", ".join(sorted(r["modules"])))

        t, c_, s_, d_, m_ = (f"{col}{n}" for col in (cT, cC, cS, cD, cM))
        ws[f"{cMi}{n}"] = (
            f'=SUBSTITUTE(TRIM(IF({t}="","Type ","")&IF(AND({c_}<>"Oui",{s_}=""),"Société ","")'
            f'&IF(AND({c_}<>"Oui",{d_}=""),"Direction ","")&IF({m_}="","Module ",""))," ",", ")'
        )
        ws[f"{cSt}{n}"] = (
            f'=IF(NOT(OR({t}<>"",{c_}="Oui",{s_}<>"",{d_}<>"",{m_}<>"")),"Not given",'
            f'IF({cMi}{n}<>"","Partial","Given"))'
        )
        parts = []
        for p in B.POLES:
            parts.append(
                f'IF(OR(ISNUMBER(SEARCH("[Toutes",{s_})),ISNUMBER(SEARCH("{pole_token(p)}",{s_})),'
                f'SUMPRODUCT(ISNUMBER(SEARCH(", "&{rng("Société")}&",",", "&{s_}&","))'
                f'*({rng("Pôle de la société")}="{p}"))>0),"{p} ","")'
            )
        ws[f"{cPo}{n}"] = '=SUBSTITUTE(TRIM(' + "&".join(parts) + ')," ",", ")'

        base.append([r["pbirs_id"], r["type"] or "", "1" if r["consolide"] else "0",
                     B.SEP.join(sorted(r["societes"])), B.SEP.join(sorted(r["directions"])),
                     B.SEP.join(sorted(r["modules"]))])
        for col in (cS, cD, cM):
            ws[f"{col}{n}"].alignment = wrap
    base.insert_rows(1)
    for i, h in enumerate(["pbirs_id", "type", "consolide", "societes", "directions", "modules"], start=1):
        base.cell(1, i, h)
    base.sheet_state = "hidden"

    last = len(reports) + 1

    def add_dv(formula, col, msg):
        dv = DataValidation(type="list", formula1=formula, allow_blank=True, showErrorMessage=True,
                            errorStyle="stop", errorTitle="Valeur invalide", error=msg)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{last}")

    add_dv(f"={rng('Type')}", cT, "Choisissez une valeur dans la liste.")
    add_dv('"Oui,Non"', cC, "Choisissez Oui ou Non.")
    add_dv(f"={rng('Société (choix)')}", cS, "Choisissez dans la liste (un choix à la fois, il s'ajoute à la cellule).")
    add_dv(f"={rng('Direction')}", cD, "Choisissez dans la liste (un choix à la fois, il s'ajoute à la cellule).")
    add_dv(f"={rng('Module')}", cM, "Choisissez dans la liste (un choix à la fois, il s'ajoute à la cellule).")

    for label, color in (("Given", "C6EFCE"), ("Partial", "FFEB9C"), ("Not given", "FFC7CE")):
        ws.conditional_formatting.add(f"{cSt}2:{cSt}{last}", FormulaRule(
            formula=[f'{cSt}2="{label}"'], fill=PatternFill("solid", bgColor=color, fgColor=color)))
    grey = PatternFill("solid", fgColor="F2F2F2")
    for n in range(2, last + 1):
        for col in ("Report name", "Path", "Missing fields", "Pôle(s) déduit(s)", "PBIRS ID"):
            ws.cell(n, ix[col]).fill = grey

    widths = {"Report name": 40, "Path": 55, "Status": 11, "Missing fields": 26, "Type": 14, "Consolidé": 11,
              "Société(s)": 46, "Direction(s)": 34, "Module(s)": 26, "Pôle(s) déduit(s)": 30, "PBIRS ID": 38}
    for name, i in ix.items():
        ws.column_dimensions[L(i)].width = widths[name]
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{L(len(cols))}{last}"

    for line in [
        "How to fill this workbook (macro version, save as .xlsm and click 'Enable content')",
        "",
        "1. Work on the 'Reports' sheet. Type, Consolidé, Société(s), Direction(s), Module(s) are dropdowns.",
        "2. Société(s), Direction(s), Module(s) accept SEVERAL values. DOUBLE-CLICK the cell: a popup lists every option",
        "   with checkboxes, tick what you need and press OK. (The dropdown arrow also works: each pick is ADDED to the",
        "   cell, and picking a value already there REMOVES it.) Delete clears the cell.",
        "3. Shortcuts in Société(s): [Toutes les sociétés] = all of them; [Pôle X] = every société of that pôle.",
        "4. The Pôle is never typed: like on the platform it follows the sociétés ('Pôle(s) déduit(s)' shows the result).",
        "5. Consolidé = Oui means Société and Direction are not required (same rule as the platform).",
        "6. Status / Missing fields / Pôle(s) déduit(s) are formulas: do not type in them.",
        "7. Do not change PBIRS ID, the Lists sheet or the hidden Baseline sheet.",
        "8. Rows left as they were are ignored on import; a row cannot be cleared from Excel (use the platform).",
        "9. If a report is edited on the platform after this file was generated, the import reports a conflict.",
    ]:
        guide.append([line])
    guide["A1"].font = Font(bold=True, size=14)
    guide.column_dimensions["A"].width = 120
    wb.active = 1
    wb.save(dst)
    print(f"Wrote {dst}: {len(reports)} reports")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
