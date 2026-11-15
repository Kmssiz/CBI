"""Pre-fill the metadata workbook from the OLD platform's extraction + folder paths.

Usage:
  python tools/prefill_metadata.py <old_extraction.csv> <metadata_entry.xlsm> <out_prefill.json> <out_review.xlsx>

Sources, from most to least reliable (every filled cell records which one):
  OLD  (high)   the old navigation tree: a PBIRS report listed under several
                Pôle > Société > Direction (or Consolidé > Direction) nodes gets the
                UNION of those places.
  PATH (medium) the PBIRS folder path itself (/CBI/Pôle X/SOCIÉTÉ/DIRECTION/..,
                /BI/extraction/<theme>, /Anomalie/<theme>).
  GUESS (low)   keyword rules on the report name (modules, default "all sociétés"
                for library/anomaly reports).
Values already typed in the workbook are never overwritten.
"""
import collections
import csv
import json
import re
import sys
import unicodedata
import urllib.parse

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

import build_metadata_workbook as B

ALL = "[Toutes les sociétés]"
HIGH, MED, LOW = "OLD", "PATH", "GUESS"


def n(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9/]+", " ", s.lower()).strip()


# ----------------------------------------------------------------- mappings
SOC_MAP = {  # old société (normalised) -> new société, None = no counterpart yet
    "greenvitro": "GREEN VITRO", "green vitro": "GREEN VITRO", "sodea": "SODEA", "btph": "BTPH", "hfcm": "HFCM",
    "alpostone": None, "granittam": "GRANITTAM", "sech": "SECH", "tamstones": "TAMSTONES", "alumix": "ALUMIX",
    "hasnaoui beton": "HASNAOUI BETON", "hgp": "GYPSUM", "htf": "HTF", "mdm": "MDM", "puma": "GRUPO PUMAL",
    "strugal": "STRUGAL", "tekna": "TEKNA", "el medina center": "EL MADINA CENTER",
    "etablissement hospitalier prive hasnaoui": "EHPH", "ehph": "EHPH", "gamma": "GAMMA", "giryad": "GIRYAD",
    "gsh": "GSHA", "hasnaoui spi": "SPI", "hl": "HL", "hta": "HTA", "le phare": "LE PHARE", "le pahre": "LE PHARE",
    "ryad mall": "RYAD MALL",
}


def map_dir(name: str):
    t = n(name)
    for key, new in (("achat", "Achat et Approvisionnement"), ("commerc", "Commerce"),
                     ("financ", "Finance et Comptabilité"), ("comptab", "Finance et Comptabilité"),
                     ("generale", "Générale"), ("administration", "Générale"), ("logisti", "Logistique"),
                     ("ressource", "Ressource Humaine"), ("technique", "Technique"),
                     ("maintenance", "Maintenance"), ("production", "Production")):
        if key in t:
            return new
    return None  # qualite, controleurs de gestion, marketing, comex: no equivalent yet


MODULE_RULES = [  # (keywords in normalised name+path, module)  first match wins
    (("gediso", "taux de validation"), "GEDISO"),
    (("parc auto", "parc_auto", "parc automobile", "transport", "parc "), "Parc Auto"),
    (("assurance",), "Assurances"),
    (("immobilis", "immo "), "Immobilisation"),
    (("gmao",), "GMAO"),
    (("ressource humaine", "ressources humaines", "/rh", " rh", "saisie mentuel", "saisiementuelle", " paie "), "Ressource Humaine"),
    (("inventaire", "stock", "entrepot", "stock picking"), "Entrepot"),
    (("production", "gpao", "ordre de fabrication", "consommation", "mylabs"), "GPAO"),
    (("achat", "appro", "importation", "fournisseur global", "dette fournisseur"), "Achats"),
    (("grand livre", "creance", "dette", "encaissement", "paiement", "paiment", "tresor", "caisse", "bilan",
      "rapprochement", "tcr", "chiffre d", "comptab", "tva", "ecriture", "piece comptable", "facture", "banqua", "pay"), "Comptabilité"),
    (("vente", "client", "price list", "devis", "showroom", "prospection"), "Ventes"),
]
DIR_FROM_MODULE = {"Achats": "Achat et Approvisionnement", "Comptabilité": "Finance et Comptabilité",
                   "Ressource Humaine": "Ressource Humaine", "Ventes": "Commerce", "GPAO": "Production",
                   "GMAO": "Maintenance", "Parc Auto": "Logistique", "Entrepot": "Logistique",
                   "Immobilisation": "Finance et Comptabilité"}


def guess_module(text: str):
    t = " " + n(text) + " "
    for kws, mod in MODULE_RULES:
        if any(k in t for k in kws):
            return mod
    return None


THEME = {  # folder theme -> (direction, module, société)
    "achats": ("Achat et Approvisionnement", "Achats", None), "entrepot": ("Logistique", "Entrepot", None),
    "comptabilite": ("Finance et Comptabilité", "Comptabilité", None), "gediso": (None, "GEDISO", None),
    "gmao": ("Maintenance", "GMAO", None), "gpao": ("Production", "GPAO", None), "ventes": ("Commerce", "Ventes", None),
    "immobilisation": ("Finance et Comptabilité", "Immobilisation", None),
    "parc automobile": ("Logistique", "Parc Auto", None), "parc_automobile": ("Logistique", "Parc Auto", None),
    "ressources humaines": ("Ressource Humaine", "Ressource Humaine", None),
    "ehph hasnaoui": (None, None, "EHPH"),
    "achat": ("Achat et Approvisionnement", "Achats", None), "rh": ("Ressource Humaine", "Ressource Humaine", None),
    "vente": ("Commerce", "Ventes", None), "production": ("Production", "GPAO", None),
}
JUNK = ("test", "duplicate", "scrape", "test acces", "refresh status", "update status", "audit", "orga 1", "the dashboard")


def path_of(lien: str):
    l = lien.strip()
    if "ReportViewer.aspx" in l:
        return "/" + urllib.parse.unquote(l.split("ReportViewer.aspx?", 1)[1].split("&")[0]).strip("/")
    l = urllib.parse.unquote(l.split("?")[0])
    m = re.search(r"/reports/powerbi/(.*)$", l, re.I)
    return "/" + m.group(1).strip("/") if m else None


# ------------------------------------------------------------- old platform
def read_old(csv_path: str):
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig", newline="")))
    byid = {r["id"]: r for r in rows}
    kids = collections.defaultdict(list)
    for r in rows:
        kids[r["parent_id"]].append(r)

    def chain(r):
        out, seen = {}, set()
        cur = byid.get(r["parent_id"])
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            out.setdefault(cur["type"], cur)
            cur = byid.get(cur["parent_id"])
        return out

    by_path = collections.defaultdict(list)
    anomalies, nolink = [], []
    for r in rows:
        if r["type"] != "6":
            continue
        if not r["lien"]:
            nolink.append(r)
            continue
        c = chain(r)
        soc = c.get("3")
        if soc and r["ancetretype"] == "3" and n(r["ancetrenom"]) != n(soc["nom"]):
            anomalies.append((r["nom"], soc["nom"], r["ancetrenom"]))
        if not soc and "1" in c and r["ancetretype"] == "3":
            anomalies.append((r["nom"], "(Consolidé branch)", r["ancetrenom"]))
        p = path_of(r["lien"])
        by_path[n(p) if p else "?" + r["id"]].append(
            {"path": p, "name": r["nom"], "soc": soc["nom"] if soc else None, "dir": c["4"]["nom"] if "4" in c else None,
             "consolide": "1" in c and not soc, "mod5": c["5"]["nom"] if "5" in c else None})
    empty_dirs = [r for r in rows if r["type"] == "4" and not any(k["type"] == "6" for k in kids[r["id"]])]
    return rows, by_path, anomalies, nolink, empty_dirs


def from_old(nodes):
    socs, dirs, unmapped_soc, unmapped_dir = set(), set(), set(), set()
    consolide = False
    for nd in nodes:
        if nd["soc"]:
            new = SOC_MAP.get(n(nd["soc"]), "?")
            if new == "?" or new is None:
                unmapped_soc.add(nd["soc"])
            else:
                socs.add(new)
        if nd["consolide"]:
            consolide = True
        if nd["dir"]:
            d = map_dir(nd["dir"])
            (dirs.add(d) if d else unmapped_dir.add(nd["dir"]))
    n_soc_nodes = len({nd["soc"] for nd in nodes if nd["soc"]})
    return socs, dirs, consolide, unmapped_soc, unmapped_dir, n_soc_nodes


def soc_cell(socs: set) -> list:
    all_flag, poles, rest = B.compress_societes(set(socs))
    if all_flag:
        return [ALL]
    return [f"[Pôle {p}]" for p in poles] + rest


# --------------------------------------------------------------------- main
def derive(row, old_nodes):
    """row = (name, path). Returns dict field -> (value, tier, evidence)."""
    name, path = row
    segs = [s for s in path.strip("/").split("/")]
    segn = [n(s) for s in segs]
    low = n(path)
    out = {}
    notes = []

    def put(field, value, tier, why):
        if value in (None, "", [], set()):
            return
        if field not in out:
            out[field] = (value, tier, why)

    if any(j in low.split() or j in segn for j in JUNK) or segn[0] in ("bi",) and len(segn) > 1 and segn[1] in ("test sara",):
        return {"_skip": ("technical / test report", "", "")}

    # ---- type (path first)
    if segn[0] == "anomalie":
        ptype = ("Anomalie", MED, "folder /Anomalie")
    elif "extraction" in segn[:2] and "dashboard" not in n(name):
        ptype = ("Bibliothèque", MED, "folder extraction")
    elif segn[0] in ("cbi", "bi") or True:
        ptype = ("Dashboard", MED if segn[0] in ("cbi", "bi") else LOW, "default dashboard")
    put("Type", *ptype)

    # ---- old platform (high)
    if old_nodes:
        socs, dirs, cons, u_soc, u_dir, n_soc = from_old(old_nodes)
        if cons:
            put("Consolidé", "Oui", HIGH, "listed in the Consolidé branch")
        if n_soc >= 20:
            put("Société(s)", [ALL], HIGH, f"listed under {n_soc} sociétés in the old platform")
        elif socs:
            put("Société(s)", soc_cell(socs), HIGH, f"old platform: {n_soc} société node(s)")
        if dirs:
            put("Direction(s)", sorted(dirs), HIGH, "old platform direction folder(s)")
        if u_soc:
            notes.append("old société without equivalent: " + ", ".join(sorted(u_soc)))
        if u_dir:
            notes.append("old direction without equivalent: " + ", ".join(sorted(u_dir)))

    # ---- folder path (medium)
    if segn[0] == "cbi" and len(segn) >= 4 and segn[1].startswith("pole"):
        s = SOC_MAP.get(segn[2], "?")
        if s not in ("?", None):
            put("Société(s)", [s], MED, "PBIRS folder: " + segs[2])
        elif s is None:
            notes.append("société folder without equivalent: " + segs[2])
        d = map_dir(segs[3]) if len(segs) > 4 or True else None
        if d:
            put("Direction(s)", [d], MED, "PBIRS folder: " + segs[3])
    elif segn[0] == "cbi" and len(segn) >= 3 and (segn[1].startswith("direction generale") or segn[1] == "rapports communs"):
        d = map_dir(segs[2])
        if d:
            put("Direction(s)", [d], MED, "PBIRS folder: " + segs[2])
    elif len(segn) >= 3 and segn[0] in ("bi", "anomalie", "extraction"):
        theme_key = segn[2] if segn[0] == "bi" and segn[1] == "extraction" else segn[1]
        if segn[0] == "extraction":
            theme_key = segn[1]
        th = THEME.get(theme_key) or THEME.get(theme_key.replace(" ", "_"))
        if th:
            put("Direction(s)", [th[0]] if th[0] else None, MED, "folder theme: " + theme_key)
            put("Module(s)", [th[1]] if th[1] else None, MED, "folder theme: " + theme_key)
            put("Société(s)", [th[2]] if th[2] else None, MED, "folder theme: " + theme_key)
    if segn[0] == "bi" and len(segn) > 1 and segn[1] == "puma":
        put("Société(s)", ["GRUPO PUMAL"], MED, "folder /BI/puma")
    if segn[0] == "extraction" and len(segn) > 1 and segn[1] in ("dossiers medicaux",):
        put("Société(s)", ["EHPH"], MED, "medical records = EHPH")
    if segn[0] == "caisse gsh":
        put("Société(s)", ["GSHA"], MED, "folder Caisse GSH")
    if "ehph" in low or "ehph" in n(name):
        put("Société(s)", ["EHPH"], MED, "name/path mentions EHPH")

    # ---- refinement of directions coming from the old catch-all "Générale" (low)
    mod_name = guess_module(name)
    cur = out.get("Direction(s)")
    if cur and cur[1] in (HIGH, MED) and segn[0] in ("cbi", "bi"):
        dset = set(cur[0])
        if dset == {"Générale"} and mod_name in DIR_FROM_MODULE:
            out["Direction(s)"] = ([DIR_FROM_MODULE[mod_name]], LOW, f"old/path: Générale, refined by report name ({mod_name})")
        elif dset == {"Technique"} and mod_name in ("Entrepot", "Parc Auto"):
            out["Direction(s)"] = (["Logistique"], LOW, f"old/path: Technique, refined by report name ({mod_name})")
        elif "Générale" in dset and "Commerce" not in dset and re.search(r"chiffre d|ca", n(name)):
            out["Direction(s)"] = (sorted(dset | {"Commerce"}), LOW, "revenue report: Commerce added to " + cur[2])

    # ---- guesses (low): module from the report NAME only (folder names like "Pôle Production" mislead)
    put("Module(s)", [mod_name] if mod_name else None, LOW, "keywords in report name")
    if "Module(s)" not in out and "Direction(s)" in out:
        for dname, md in (("Finance et Comptabilité", "Comptabilité"), ("Achat et Approvisionnement", "Achats"),
                          ("Ressource Humaine", "Ressource Humaine"), ("Commerce", "Comptabilité"),
                          ("Technique", "GPAO"), ("Production", "GPAO")):
            if dname in out["Direction(s)"][0]:
                put("Module(s)", [md], LOW, "deduced from direction " + dname)
                break
    if "Direction(s)" not in out and mod_name in DIR_FROM_MODULE and segn[0] != "cbi":
        put("Direction(s)", [DIR_FROM_MODULE[mod_name]], LOW, "deduced from module " + mod_name)
    if "Société(s)" not in out and segn[0] in ("bi", "anomalie", "extraction") and segn[0] != "cbi":
        put("Société(s)", [ALL], LOW, "library/anomaly report: assumed all sociétés")
    if not out.get("Société(s)"):
        words = set(n(name).split())
        for tok, new in (("btph", "BTPH"), ("strugal", "STRUGAL"), ("tekna", "TEKNA"), ("puma", "GRUPO PUMAL"),
                         ("mdm", "MDM"), ("htf", "HTF"), ("gsh", "GSHA"), ("sodea", "SODEA"), ("gamma", "GAMMA")):
            if tok in words:
                put("Société(s)", [new], LOW, f"name contains '{tok}'")
                break
    out["_notes"] = (notes, "", "")
    return out


def main(old_csv, xlsm, out_json, out_xlsx):
    rows, by_path, anomalies, nolink, empty_dirs = read_old(old_csv)
    ws = load_workbook(xlsm, read_only=True)["Reports"]
    hdr = [c for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    ix = {h: i for i, h in enumerate(hdr)}
    X = list(ws.iter_rows(min_row=2, values_only=True))

    fills, matched_keys = [], set()
    for r in X:
        name, path, pid = r[ix["Report name"]], r[ix["Path"]], r[ix["PBIRS ID"]]
        old = by_path.get(n(path))
        if old:
            matched_keys.add(n(path))
        d = derive((name, path), old)
        existing = {k: r[ix[k]] for k in ("Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)") if r[ix[k]]}
        fills.append({"pbirs_id": pid, "name": name, "path": path, "matched_old": bool(old),
                      "existing": existing,
                      "fields": {k: {"value": (v[0] if not isinstance(v[0], list) else ", ".join(v[0])), "tier": v[1], "why": v[2]}
                                 for k, v in d.items() if not k.startswith("_")},
                      "skip": d.get("_skip", ("",))[0], "notes": d.get("_notes", ([],))[0]})
    json.dump(fills, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ------------------------------------------------------------ review file
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Prefill"
    heads = ["Path", "Old platform match", "Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)",
             "Evidence", "Already in workbook (kept)", "Notes"]
    ws1.append(heads)
    colr = {HIGH: "C6EFCE", MED: "FFEB9C", LOW: "F8CBAD"}
    for f in fills:
        evid = "; ".join(f"{k}: {v['tier']} ({v['why']})" for k, v in f["fields"].items())
        ws1.append([f["path"], "yes" if f["matched_old"] else "no"] +
                   [f["fields"].get(k, {}).get("value", "") for k in ("Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)")] +
                   [evid if not f["skip"] else "SKIPPED: " + f["skip"],
                    "; ".join(f"{k}={v}" for k, v in f["existing"].items()), " | ".join(f["notes"])])
        for j, k in enumerate(("Type", "Consolidé", "Société(s)", "Direction(s)", "Module(s)"), start=3):
            t = f["fields"].get(k, {}).get("tier")
            if t:
                ws1.cell(ws1.max_row, j).fill = PatternFill("solid", fgColor=colr[t])
    for c in ws1[1]:
        c.font = Font(bold=True)
    for col, w in zip("ABCDEFGHIJ", (70, 10, 14, 10, 40, 36, 20, 70, 40, 50)):
        ws1.column_dimensions[col].width = w
    ws1.freeze_panes = "B2"
    ws1.auto_filter.ref = ws1.dimensions

    ws2 = wb.create_sheet("Old links not found")
    ws2.append(["Old platform report", "Link path", "Under (société / direction)"])
    for k, nodes in by_path.items():
        if k not in matched_keys:
            ws2.append([nodes[0]["name"], nodes[0]["path"] or "(unparsed link)",
                        "; ".join(sorted({f"{x['soc'] or 'Consolidé'} / {x['dir']}" for x in nodes}))])
    ws3 = wb.create_sheet("Old platform oddities")
    ws3.append(["Kind", "Detail"])
    for r in nolink:
        ws3.append(["report node without a link", f"id={r['id']} name={r['nom']!r}"])
    for nm, soc, anc in anomalies:
        ws3.append(["ancestor field disagrees with real parent", f"{nm}: parent société={soc}, ancetrenom={anc}"])
    for r in empty_dirs:
        ws3.append(["empty folder (direction with no report)", f"{r['nom']} (under {r['ancetrenom']})"])
    for w in (wb["Old links not found"], ws3):
        for c in w[1]:
            c.font = Font(bold=True)
        w.column_dimensions["A"].width = 50
        w.column_dimensions["B"].width = 90
    wb["Old links not found"].column_dimensions["C"].width = 60
    wb.save(out_xlsx)

    tier = collections.Counter(v["tier"] for f in fills for v in f["fields"].values())
    filled_rows = sum(1 for f in fills if f["fields"])
    print(f"reports {len(fills)}; with any prefill {filled_rows}; skipped technical {sum(1 for f in fills if f['skip'])}")
    print("cells by confidence:", dict(tier))
    print("old links matched", len(matched_keys), "of", len(by_path), "| empty folders", len(empty_dirs),
          "| anomalies", len(anomalies), "| nolink", len(nolink))


if __name__ == "__main__":
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    main(*sys.argv[1:5])
