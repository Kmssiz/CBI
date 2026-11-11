"""Build the metadata data-entry workbook from a server export.

Usage: python tools/build_metadata_workbook.py pbi.xlsx [metadata_entry.xlsx]

Input  : the export produced by tools/report_metadata.sql (one row per report).
Output : an .xlsx where every metadata cell is a dropdown fed by the platform's
         option lists, pre-filled with what is already in the database.

A hidden "Baseline" sheet keeps the values as they were at export time; the
importer uses it to detect reports that were edited on the platform meanwhile.
"""
import sys
import unicodedata
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter as L
from openpyxl.worksheet.datavalidation import DataValidation

# --------------------------------------------------------------------------
# Platform option lists (Métadonnées des Rapports page). The importer checks
# every name against the real database and refuses unknown ones.
# --------------------------------------------------------------------------
POLES = ["Agriculture", "Construction", "Pierre", "Production", "Service"]
SOCIETES = {  # société -> pôle
    "ALUMIX": "Production", "BTPH": "Construction", "CLUB AMARNAS": "Service",
    "EHPH": "Service", "EL MADINA CENTER": "Service", "GAMMA": "Service",
    "GIRYAD": "Service", "GRANITTAM": "Pierre", "GREEN VITRO": "Agriculture",
    "GRUPO PUMAL": "Production", "GSHA": "Service", "GYPSUM": "Production",
    "HASNAOUI BETON": "Production", "HFCM": "Construction", "HL": "Service",
    "HTA": "Service", "HTF": "Service", "LE PHARE": "Service", "MDM": "Production",
    "RYAD MALL": "Service", "SECH": "Pierre", "SODEA": "Agriculture",
    "SPI": "Service", "STRUGAL": "Production", "TAMSTONES": "Pierre",
    "TEKNA": "Production",
}
DIRECTIONS = [
    "Achat et Approvisionnement", "Commerce", "Finance et Comptabilité", "Générale",
    "Logistique", "Maintenance", "Production", "Ressource Humaine", "Technique",
]
MODULES = [
    "Achats", "Assurances", "Comptabilité", "Entrepot", "GEDISO", "GMAO", "GPAO",
    "Immobilisation", "Parc Auto", "Ressource Humaine", "Ventes",
]
TYPES = {"anomalie": "Anomalie", "bibliotheque": "Bibliothèque", "dashboard": "Dashboard"}

# Number of dropdown columns per field (a report can use that many values).
N_POLE_SHORTCUT, N_SOCIETE, N_DIRECTION, N_MODULE = 3, 8, 3, 3
SEP = "|"


def norm(s) -> str:
    return unicodedata.normalize("NFC", str(s or "")).strip()


def split_names(cell) -> list:
    return [norm(x) for x in str(cell or "").split(", ") if norm(x)]


def type_code(label: str):
    label = norm(label)
    for code, disp in TYPES.items():
        if label == disp:
            return code
    return None


def read_export(path: str) -> list:
    ws = load_workbook(path, read_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    # fixed column positions of tools/report_metadata.sql
    out = []
    for r in rows[1:]:
        out.append({
            "name": r[0], "path": r[1], "pbirs_id": r[11],
            "type": type_code(r[4]), "consolide": norm(r[5]) == "Yes",
            "societes": split_names(r[7]), "directions": split_names(r[8]),
            "modules": split_names(r[9]),
        })
    return out


def compress_societes(selected: set):
    """Return (all?, [poles], [societes]) describing the set with fewest cells."""
    if selected == set(SOCIETES):
        return True, [], []
    poles, rest = [], set(selected)
    for p in POLES:
        members = {s for s, pp in SOCIETES.items() if pp == p}
        if len(members) >= 2 and members <= selected:
            poles.append(p)
            rest -= members
    return False, poles, sorted(rest)


def main(src: str, dst: str = "metadata_entry.xlsx") -> None:
    reports = read_export(src)

    # --- every existing name must be a known platform option -----------------
    for r in reports:
        for s in r["societes"]:
            assert s in SOCIETES, f"Unknown société {s!r} in {r['name']}"
        for d in r["directions"]:
            assert d in DIRECTIONS, f"Unknown direction {d!r} in {r['name']}"
        for m in r["modules"]:
            assert m in MODULES, f"Unknown module {m!r} in {r['name']}"

    wb = Workbook()
    ws = wb.active
    ws.title = "Reports"
    lists = wb.create_sheet("Lists")
    base = wb.create_sheet("Baseline")
    guide = wb.create_sheet("Read me", 0)

    # ------------------------------- Lists ----------------------------------
    list_cols = {
        "Type": list(TYPES.values()), "Société": list(SOCIETES), "Pôle de la société": list(SOCIETES.values()),
        "Pôle": POLES, "Direction": DIRECTIONS, "Module": MODULES,
    }
    for c, (h, vals) in enumerate(list_cols.items(), start=1):
        lists.cell(1, c, h).font = Font(bold=True)
        for i, v in enumerate(vals, start=2):
            lists.cell(i, c, v)
        lists.column_dimensions[L(c)].width = 28
    lists.cell(1, 8, "Do not edit: the dropdowns read these lists.").font = Font(italic=True)

    def rng(col_name: str) -> str:
        c = list(list_cols).index(col_name) + 1
        return f"Lists!${L(c)}$2:${L(c)}${len(list_cols[col_name]) + 1}"

    # ------------------------------ Columns ---------------------------------
    cols = ["Report name", "Path", "Status", "Missing fields", "Type", "Consolidé", "Toutes les sociétés"]
    cols += [f"Pôle entier {i}" for i in range(1, N_POLE_SHORTCUT + 1)]
    cols += [f"Société {i}" for i in range(1, N_SOCIETE + 1)]
    cols += [f"Direction {i}" for i in range(1, N_DIRECTION + 1)]
    cols += [f"Module {i}" for i in range(1, N_MODULE + 1)]
    cols += ["Pôle(s) déduit(s)", "PBIRS ID"]
    ix = {name: i + 1 for i, name in enumerate(cols)}

    def first(prefix): return ix[f"{prefix} 1"]
    c_type, c_cons, c_all = ix["Type"], ix["Consolidé"], ix["Toutes les sociétés"]
    c_pole0, c_soc0 = first("Pôle entier"), first("Société")
    c_dir0, c_mod0 = first("Direction"), first("Module")
    c_stat, c_miss, c_ded, c_id = ix["Status"], ix["Missing fields"], ix["Pôle(s) déduit(s)"], ix["PBIRS ID"]
    last_entry = c_mod0 + N_MODULE - 1

    ws.append(cols)
    groups = {"info": "1F4E78", "calc": "595959", "entry": "2E75B6", "soc": "C55A11", "dir": "548235", "mod": "7030A0"}

    def group_of(name):
        if name in ("Report name", "Path", "PBIRS ID"): return "info"
        if name in ("Status", "Missing fields", "Pôle(s) déduit(s)"): return "calc"
        if name in ("Type", "Consolidé"): return "entry"
        if name.startswith(("Toutes", "Pôle entier", "Société")): return "soc"
        if name.startswith("Direction"): return "dir"
        return "mod"

    for i, name in enumerate(cols, start=1):
        cell = ws.cell(1, i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=groups[group_of(name)])
        cell.alignment = Alignment(wrap_text=True, vertical="center")

    # -------------------------------- Rows ----------------------------------
    for n, r in enumerate(reports, start=2):
        all_flag, poles, socs = compress_societes(set(r["societes"]))
        assert len(poles) <= N_POLE_SHORTCUT and len(socs) <= N_SOCIETE, f"Too many values: {r['name']}"
        assert len(r["directions"]) <= N_DIRECTION and len(r["modules"]) <= N_MODULE, f"Too many values: {r['name']}"
        ws.cell(n, ix["Report name"], r["name"])
        ws.cell(n, ix["Path"], r["path"])
        ws.cell(n, c_id, r["pbirs_id"])
        if r["type"]:
            ws.cell(n, c_type, TYPES[r["type"]])
        if r["consolide"]:
            ws.cell(n, c_cons, "Oui")
        if all_flag:
            ws.cell(n, c_all, "Oui")
        for i, v in enumerate(poles):
            ws.cell(n, c_pole0 + i, v)
        for i, v in enumerate(socs):
            ws.cell(n, c_soc0 + i, v)
        for i, v in enumerate(sorted(r["directions"])):
            ws.cell(n, c_dir0 + i, v)
        for i, v in enumerate(sorted(r["modules"])):
            ws.cell(n, c_mod0 + i, v)

        # Live status formulas
        rr = lambda a, b: f"{L(a)}{n}:{L(b)}{n}"
        soc_cells = rr(c_all, c_soc0 + N_SOCIETE - 1)
        dir_cells = rr(c_dir0, c_dir0 + N_DIRECTION - 1)
        mod_cells = rr(c_mod0, c_mod0 + N_MODULE - 1)
        t, cons = f"{L(c_type)}{n}", f"{L(c_cons)}{n}"
        given = f'OR({t}<>"",{cons}="Oui",COUNTA({soc_cells},{dir_cells},{mod_cells})>0)'
        ws.cell(n, c_miss).value = (
            f'=SUBSTITUTE(TRIM(IF({t}="","Type ","")'
            f'&IF(AND({cons}<>"Oui",COUNTA({soc_cells})=0),"Société ","")'
            f'&IF(AND({cons}<>"Oui",COUNTA({dir_cells})=0),"Direction ","")'
            f'&IF(COUNTA({mod_cells})=0,"Module ",""))," ",", ")'
        )
        ws.cell(n, c_stat).value = (
            f'=IF(NOT({given}),"Not given",IF({L(c_miss)}{n}<>"","Partial","Given"))'
        )
        parts = []
        for p in POLES:
            parts.append(
                f'IF(OR(${L(c_all)}{n}="Oui",COUNTIF({rr(c_pole0, c_pole0 + N_POLE_SHORTCUT - 1)},"{p}")>0,'
                f'SUMPRODUCT(COUNTIFS({rng("Société")},{rr(c_soc0, c_soc0 + N_SOCIETE - 1)},'
                f'{rng("Pôle de la société")},"{p}"))>0),"{p} ","")'
            )
        ws.cell(n, c_ded).value = '=SUBSTITUTE(TRIM(' + "&".join(parts) + ')," ",", ")'

        # Baseline snapshot (resolved sets, as the database held them at export)
        base.append([
            r["pbirs_id"], r["type"] or "", "1" if r["consolide"] else "0",
            SEP.join(sorted(r["societes"])), SEP.join(sorted(r["directions"])), SEP.join(sorted(r["modules"])),
        ])
    base.insert_rows(1)
    for i, h in enumerate(["pbirs_id", "type", "consolide", "societes", "directions", "modules"], start=1):
        base.cell(1, i, h)
    base.sheet_state = "hidden"

    last = len(reports) + 1

    # ----------------------------- Dropdowns --------------------------------
    def add_dv(formula, c_from, count=1, title="", msg=""):
        dv = DataValidation(type="list", formula1=formula, allow_blank=True, showErrorMessage=True,
                            errorStyle="stop", errorTitle=title or "Valeur invalide",
                            error=msg or "Choisissez une valeur dans la liste déroulante.")
        ws.add_data_validation(dv)
        dv.add(f"{L(c_from)}2:{L(c_from + count - 1)}{last}")

    add_dv(f"={rng('Type')}", c_type)
    add_dv('"Oui,Non"', c_cons)
    add_dv('"Oui"', c_all)
    add_dv(f"={rng('Pôle')}", c_pole0, N_POLE_SHORTCUT)
    add_dv(f"={rng('Société')}", c_soc0, N_SOCIETE)
    add_dv(f"={rng('Direction')}", c_dir0, N_DIRECTION)
    add_dv(f"={rng('Module')}", c_mod0, N_MODULE)

    # ------------------------ Conditional formatting ------------------------
    s = f"{L(c_stat)}2:{L(c_stat)}{last}"
    for label, color in (("Given", "C6EFCE"), ("Partial", "FFEB9C"), ("Not given", "FFC7CE")):
        ws.conditional_formatting.add(s, FormulaRule(formula=[f'{L(c_stat)}2="{label}"'],
                                                     fill=PatternFill("solid", bgColor=color, fgColor=color)))
    for c0, k in ((c_pole0, N_POLE_SHORTCUT), (c_soc0, N_SOCIETE), (c_dir0, N_DIRECTION), (c_mod0, N_MODULE)):
        a, b = L(c0), L(c0 + k - 1)
        ws.conditional_formatting.add(
            f"{a}2:{b}{last}",
            FormulaRule(formula=[f'AND({a}2<>"",COUNTIF(${a}2:${b}2,{a}2)>1)'],
                        fill=PatternFill("solid", bgColor="FF9999", fgColor="FF9999")),
        )
    grey = PatternFill("solid", fgColor="F2F2F2")
    for n in range(2, last + 1):
        for c in (ix["Report name"], ix["Path"], c_stat, c_miss, c_ded, c_id):
            ws.cell(n, c).fill = grey if c != c_stat else PatternFill(fill_type=None)

    # ------------------------------- Layout ---------------------------------
    widths = {"Report name": 40, "Path": 55, "Status": 11, "Missing fields": 26, "Type": 14, "Consolidé": 11,
              "Toutes les sociétés": 12, "Pôle(s) déduit(s)": 30, "PBIRS ID": 38}
    for name, i in ix.items():
        ws.column_dimensions[L(i)].width = widths.get(name, 20)
    ws.row_dimensions[1].height = 32
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{L(len(cols))}{last}"

    # ------------------------------- Read me --------------------------------
    for line in [
        "How to fill this workbook",
        "",
        "1. Work on the 'Reports' sheet. Every coloured column is a dropdown fed by the platform's lists.",
        "2. A report can have several values: use Direction 1/2/3, Module 1/2/3 and Société 1..%d (one per column)." % N_SOCIETE,
        "3. Shortcuts for sociétés: 'Toutes les sociétés' = Oui selects all of them; 'Pôle entier' selects every société of that pôle.",
        "4. The Pôle is never typed: like on the platform it follows the sociétés ('Pôle(s) déduit(s)' shows the result).",
        "5. Consolidé = Oui means Société and Direction are not required (same rule as the platform).",
        "6. Status / Missing fields / Pôle(s) déduit(s) are formulas: do not type in them. Red cells = same value twice.",
        "7. Do not change PBIRS ID, the Lists sheet or the hidden Baseline sheet.",
        "8. Rows left exactly as they were are ignored on import. A row cannot be cleared from Excel (use the platform).",
        "9. If a report is edited on the platform after this file was generated, the import refuses to overwrite it and reports a conflict.",
    ]:
        guide.append([line])
    guide["A1"].font = Font(bold=True, size=14)
    guide.column_dimensions["A"].width = 130

    wb.active = 1
    wb.save(dst)
    print(f"Wrote {dst}: {len(reports)} reports")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
