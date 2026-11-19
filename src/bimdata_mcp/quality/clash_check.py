"""Contrôle de conflits (clash) : disciplines, matrice, conversion en anomalies, feuille Volumes.

Le moteur géométrique est :mod:`bimdata_mcp.geometry.clash` ; ce module fait le lien avec le
registre de contrôles (``Issue``), la matrice de disciplines (règle Solibri n°245) et le gabarit
Excel de matrice.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..charte import Col, Contexte, Livrable
from ..geometry.clash import (
    GROUP_LABELS,
    Clash,
    ClashOptions,
    assign_groups,
    find_clashes,
    merge_exceptions,
)
from ..geometry.index import GeometryIndex
from ..model_view import Record, resolve_field
from ..selection import as_bool
from .engine import CheckResult
from .rules import RULES, Rule, _Run

DISCIPLINES = ("Architecture", "Structure", "Fluides")
FLUIDS_PREFIXES = ("IfcFlow", "IfcDistribution", "IfcEnergyConversionDevice", "IfcPipe", "IfcDuct", "IfcCable",
                   "IfcAirTerminal", "IfcLightFixture", "IfcSanitaryTerminal", "IfcFireSuppression", "IfcValve",
                   "IfcPump", "IfcFan", "IfcBoiler", "IfcChiller", "IfcUnitaryEquipment")
STRUCTURE_PREFIXES = ("IfcBeam", "IfcColumn", "IfcFooting", "IfcPile", "IfcReinforcing", "IfcTendon")
CLASH_TYPE_FR = {"duplicate": "doublon", "inside": "inclus", "overlap": "chevauchement"}
TYPES_FR = {"doublon": "duplicate", "inclus": "inside", "chevauchement": "overlap"}

RULES["clash"] = Rule("clash", "Conflits (intersection générale)", "Maillage", "majeur", "clash",
                      "Doublons, inclusions et chevauchements entre deux ensembles de composants.")

#: Matrice par défaut : Structure × Structure (doublons), Architecture × Structure, Fluides × Structure,
#: Fluides × Architecture, Fluides × Fluides (même système ignoré : raccords d'un même réseau).
DEFAULT_MATRIX = [
    {"id": "STR-STR", "a": "Structure", "b": "Structure", "types": ["duplicate"]},
    {"id": "ARC-STR", "a": "Architecture", "b": "Structure"},
    {"id": "FLU-STR", "a": "Fluides", "b": "Structure"},
    {"id": "FLU-ARC", "a": "Fluides", "b": "Architecture"},
    {"id": "FLU-FLU", "a": "Fluides", "b": "Fluides", "ignore_same_system": True},
]


def discipline_of(e: dict, rec: Record | None, prop: str | None = None) -> str:
    if prop and rec is not None:
        fv = resolve_field(rec, prop)
        if fv.found:
            v = str(fv.value).strip()
            for d in DISCIPLINES:
                if v.lower().startswith(d.lower()[:4]):
                    return d
            return v
    cls = e["ifc_class"]
    if cls.startswith(FLUIDS_PREFIXES):
        return "Fluides"
    if cls.startswith(STRUCTURE_PREFIXES):
        return "Structure"
    if cls.startswith(("IfcWall", "IfcSlab")) and rec is not None:
        if as_bool(resolve_field(rec, "*.LoadBearing").value):
            return "Structure"
    return "Architecture"


def disciplines(idx: GeometryIndex, records: dict[str, Record], prop: str | None = None) -> dict[str, str]:
    return {g: discipline_of(e, records.get(g), prop) for g, e in idx.elements.items() if e["ifc_class"] != "IfcSpace"}


def options_from(params: dict) -> ClashOptions:
    types = params.get("types") or ["duplicate", "inside", "overlap"]
    types = {TYPES_FR.get(str(t).lower(), str(t).lower()) for t in types}
    bad = types - {"duplicate", "inside", "overlap"}
    if bad:
        raise ValueError(f"Types de conflit inconnus : {sorted(bad)} (duplicate, inside, overlap).")
    return ClashOptions(
        tol_h=float(params.get("tol_h_m", 0.005) if params.get("tol_h_m") is not None else 0.005),
        tol_v=float(params.get("tol_v_m", 0.005) if params.get("tol_v_m") is not None else 0.005),
        tol_volume=None if params.get("tol_volume_m3") in (None, "") else float(params["tol_volume_m3"]),
        types=frozenset(types), ignore_same_system=bool(params.get("ignore_same_system", False)),
        ignore_same_layer=bool(params.get("ignore_same_layer", False)),
        exceptions=merge_exceptions(params.get("exceptions")),
        severity_rules=list(params.get("severity_rules") or []),
        contact_m=float(params.get("contact_m") or 0.0),
    )


def _message(idx: GeometryIndex, c: Clash) -> str:
    ea, eb = idx.elements[c.a], idx.elements[c.b]
    who = f"« {ea['name'] or ea['ifc_class']} » ({ea['ifc_class']}) × « {eb['name'] or eb['ifc_class']} » ({eb['ifc_class']})"
    if c.clash_type == "duplicate":
        return f"Doublon : {who} — même emprise et même volume"
    if c.clash_type == "inside":
        inner = c.a if c.inside_of == c.b else c.b
        return f"Inclus : « {idx.elements[inner]['name']} » entièrement dans « {idx.elements[c.inside_of]['name']} » ({who})"
    vol = f", volume {c.volume:.4f} m³" if c.volume is not None else ""
    return (f"Chevauchement : {who} — pénétration horizontale {c.depth_h:.3f} m, verticale {c.depth_v:.3f} m"
            f"{vol}" + (f", {c.orientation}" if c.orientation else ""))


def add_clashes(run: _Run, idx: GeometryIndex, clashes: list[Clash], opts: ClashOptions, *, cell: str | None = None,
                disc: dict | None = None) -> None:
    from ..geometry.clash import SEV_ORDER, connected_groups

    disc = disc or {}
    # doublons superposés : une ligne par groupe (triplet d'eau glacée = 1 ligne), les paires restent dans la mesure
    dups = [c for c in clashes if c.clash_type == "duplicate"]
    for grp in connected_groups([(c.a, c.b) for c in dups]):
        members = set(grp)
        cs = [c for c in dups if c.a in members]
        if len(cs) < 2:
            continue
        sev = min((c.severity for c in cs), key=lambda v: SEV_ORDER.get(v, 9))
        boxes = [idx.elements[g]["aabb"] for g in grp]
        bbox = [min(b[i] for b in boxes) for i in range(3)] + [max(b[i] for b in boxes) for i in range(3, 6)]
        e0 = idx.elements[grp[0]]
        run.add("clash", grp[0], f"Doublon : {len(grp)} éléments superposés (« {e0['name'] or e0['ifc_class']} » "
                f"{e0['ifc_class']}…) — même emprise et même volume, {len(cs)} paires", severity=sev,
                expected="un seul élément à cet emplacement",
                measure={"value": len(grp), "unit": "éléments", "n_elements": len(cs),
                         "paires": [[c.a, c.b] for c in cs]},
                bbox=bbox, elements=[{"uuid": grp[0], "role": "A", "discipline": disc.get(grp[0])}]
                + [{"uuid": g, "role": "B", "discipline": disc.get(g)} for g in grp[1:]])
        iss = run.issues[-1]
        iss.clash_type, iss.group, iss.cell, iss.method = CLASH_TYPE_FR["duplicate"], cs[0].group, cell, "Bbox"
        clashes = [c for c in clashes if c not in cs]
    for c in clashes:
        thr = {"value": opts.tol_h, "unit": "m", "source": f"tol_h_m={opts.tol_h} ; tol_v_m={opts.tol_v}"
               + (f" ; tol_volume_m3={opts.tol_volume}" if opts.tol_volume is not None else "")}
        run.add("clash", c.a, _message(idx, c), severity=c.severity, actual=c.depth, expected="aucune intersection",
                measure={"value": c.depth, "unit": "m", "depth_h_m": c.depth_h, "depth_v_m": c.depth_v,
                         "volume_m3": None if c.volume is None else round(c.volume, 6), "size": c.size,
                         "orientation": c.orientation, "angle_deg": c.angle_deg},
                threshold=thr, bbox=c.inter_aabb,
                elements=[{"uuid": c.a, "role": "A", "discipline": disc.get(c.a)},
                          {"uuid": c.b, "role": "B", "discipline": disc.get(c.b)}])
        iss = run.issues[-1]
        iss.clash_type = CLASH_TYPE_FR[c.clash_type]
        iss.group = c.group
        iss.cell = cell
        iss.method = "Bbox" if c.clash_type == "duplicate" else "Maillage"
        if c.inter_aabb:
            b = c.inter_aabb
            iss.where["center"] = [round((b[i] + b[i + 3]) / 2, 3) for i in range(3)]


def run_cells(idx: GeometryIndex, records: dict[str, Record], cells: list[dict], *,
              resolve, disc: dict[str, str], label_cells: bool = True) -> tuple[CheckResult, list[str], dict]:
    """Exécute des cases (``a``, ``b`` résolus par ``resolve``) et regroupe les anomalies dans un seul contrôle."""
    run = _Run(rules=[RULES["clash"]], records=records, idx=idx)
    for s in idx.ifc.get("storeys") or []:
        run.storey_label.setdefault(s["guid"], s["name"])
    notes: list[str] = []
    per_cell, all_clashes = [], []
    seen_pairs: set[tuple[str, str]] = set()
    for cell in cells:
        if not cell.get("active", True):
            continue
        cid = str(cell.get("id") or f"{cell.get('a')} × {cell.get('b')}")
        set_a, set_b = resolve(cell["a"]), resolve(cell["b"])
        opts = options_from(cell)
        clashes, stats = find_clashes(idx, set_a, set_b, opts, records=records, disciplines=disc)
        clashes = [c for c in clashes if tuple(sorted((c.a, c.b))) not in seen_pairs]  # une paire, une case
        seen_pairs |= {tuple(sorted((c.a, c.b))) for c in clashes}
        if cell.get("severity"):
            for c in clashes:
                c.severity = cell["severity"]
        assign_groups(idx, clashes)
        add_clashes(run, idx, clashes, opts, cell=cid if label_cells else None, disc=disc)
        for g in set_a + set_b:
            run.checked.add(g)
        run.applicable["clash"] += stats["paires_candidates"]
        no_geom = sum(1 for g in set(set_a) | set(set_b) if not (idx.elements.get(g) or {}).get("has_geometry"))
        per_cell.append({"case": cid, "set_a": len(set_a), "set_b": len(set_b), "conflits": len(clashes),
                         "par_type": dict(Counter(CLASH_TYPE_FR[c.clash_type] for c in clashes)),
                         "par_severite": dict(Counter(c.severity for c in clashes)),
                         "paires_candidates": stats["paires_candidates"], "sous_tolerance": stats["sous_tolerance"],
                         "exceptions": stats["exceptions"], "exclues": stats["exclues"], "sans_geometrie": no_geom})
        if no_geom:
            notes.append(f"{cid} : {no_geom} élément(s) sans géométrie non testés (voir S10).")
        if not set_a or not set_b:
            notes.append(f"{cid} : ensemble {'A' if not set_a else 'B'} vide — case sans objet.")
        all_clashes += clashes
    res = run.result()
    return res, notes, {"cases": per_cell, "clashes": all_clashes}


def volumes_rows(idx: GeometryIndex, clashes: list[Clash], scope: set[str]) -> list[list]:
    """Par type de construction : volume total (maillages fermés), volume intersecté, pourcentage."""
    total: dict[str, float] = defaultdict(float)
    n: Counter = Counter()
    for g in scope:
        e = idx.elements.get(g)
        if e and e.get("is_closed") and e.get("volume"):
            t = e.get("type_name") or e["ifc_class"]
            total[t] += e["volume"]
            n[t] += 1
    inter: dict[str, float] = defaultdict(float)
    k: Counter = Counter()
    for c in clashes:
        if c.volume is None:
            continue
        for g in (c.a, c.b):
            t = idx.elements[g].get("type_name") or idx.elements[g]["ifc_class"]
            inter[t] += c.volume
            k[t] += 1
    rows = []
    for t in sorted(set(total) | set(inter), key=lambda t: -inter.get(t, 0)):
        tv, iv = total.get(t, 0.0), inter.get(t, 0.0)
        rows.append([t, n.get(t, 0), round(tv, 3), k.get(t, 0), round(iv, 4), (iv / tv) if tv else None])
    return rows


VOLUME_COLS = [Col("Type de construction", "Type", width=46), Col("Éléments (fermés)", "Type", kind="integer"),
               Col("Volume total (m³)", "Volumes", kind="number"), Col("Conflits", "Volumes", kind="integer"),
               Col("Volume intersecté (m³)", "Volumes", kind="number"), Col("Part intersectée", "Volumes", kind="percent")]


# ── Gabarit et lecture de la matrice Excel ──────────────────────────────

MATRIX_COLS = [Col("Case", "Case", kind="edit", width=12), Col("Ensemble A", "Ensembles", kind="edit", width=30),
               Col("Ensemble B", "Ensembles", kind="edit", width=30),
               Col("Types", "Règle", kind="edit", width=30), Col("Tolérance horizontale (m)", "Règle", kind="edit"),
               Col("Tolérance verticale (m)", "Règle", kind="edit"), Col("Tolérance volume (m³)", "Règle", kind="edit"),
               Col("Sévérité imposée", "Règle", kind="edit", width=14),
               Col("Ignorer même système", "Règle", kind="edit", width=12),
               Col("Ignorer même calque", "Règle", kind="edit", width=12), Col("Active", "Règle", kind="edit", width=9)]


def write_matrix_template(path: Path, ctx: Contexte) -> None:
    lv = Livrable(path, ctx)
    lv.synthese(intro="Matrice de détection de conflits : une ligne par couple d'ensembles à confronter. Compléter les "
                      "colonnes jaunes, puis déposer le fichier et lancer run_clash_matrix(file_id=…).")
    rows = [[c["id"], c["a"], c["b"], ", ".join(CLASH_TYPE_FR[t] for t in c.get("types", ["duplicate", "inside",
                                                                                         "overlap"])),
             0.005, 0.005, None, None, "Oui" if c.get("ignore_same_system") else "Non", "Non", "Oui"]
            for c in DEFAULT_MATRIX]
    lv.feuille("Matrice", MATRIX_COLS, rows, role="saisie", freeze_cols=1,
               validations={7: ["bloquant", "majeur", "mineur", "info"], 8: ["Oui", "Non"], 9: ["Oui", "Non"],
                            10: ["Oui", "Non"]},
               description="Cases de la matrice (préremplies avec la matrice par défaut).")
    lv.lisez_moi("Gabarit de matrice de conflits (équivalent de la règle Solibri « Clash Detection Matrix »).", [
        ("Ensembles A et B", [
            "Discipline : « Architecture », « Structure » ou « Fluides » (ou « discipline:Fluides »).",
            "Sélection enregistrée : « selection:nom de la sélection ».",
            "Classes IFC : « classes:IfcDuctSegment,IfcPipeSegment » (sous-types inclus).",
            "Critères avancés : objet JSON de select_elements, ex. {\"ifc_classes\": [\"IfcWall\"], \"storeys\": [\"R+1\"]}."]),
        ("Règle", [
            "Types : doublon, inclus, chevauchement (séparés par des virgules ; vide = les trois).",
            "Tolérances : une intersection dont la pénétration horizontale (ou verticale) est sous la valeur n'est pas "
            "signalée ; 0 = clash dur. Tolérance de volume : facultative, fiable seulement pour les solides fermés.",
            "Sévérité imposée : vide = sévérité calculée (doublon et inclus bloquants, chevauchement selon la taille)."])])
    lv.meta({"format": "bimdata-mcp/clash-matrix/v1", "sheet": "Matrice", "header_rows": 2})
    lv.close()


def _yes(v: Any) -> bool:
    return str(v or "").strip().lower() in {"oui", "yes", "true", "1", "o", "x"}


def parse_set(text: Any) -> Any:
    """Ensemble d'une case : discipline, ``selection:nom``, ``classes:A,B`` ou critères JSON."""
    if isinstance(text, dict):
        return text
    s = str(text or "").strip()
    if not s:
        raise ValueError("Ensemble vide dans la matrice.")
    if s.startswith("{"):
        try:
            return json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Critères JSON invalides : {s[:60]} ({exc})") from exc
    head, sep, rest = s.partition(":")
    if sep and head.strip().lower() in ("discipline", "selection", "sélection", "classes"):
        key = {"sélection": "selection"}.get(head.strip().lower(), head.strip().lower())
        if key == "classes":
            return {"ifc_classes": [c.strip() for c in rest.split(",") if c.strip()]}
        return {key: rest.strip()}
    if s.capitalize() in DISCIPLINES:
        return {"discipline": s.capitalize()}
    return {"selection": s}


def read_matrix(path: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb["Matrice"] if "Matrice" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next((i for i, r in enumerate(rows[:6])
                  if r and any(str(c or "").strip().lower() == "ensemble a" for c in r)), None)
    if hdr_i is None:
        raise ValueError("Feuille « Matrice » : en-tête « Ensemble A » introuvable (utiliser clash_matrix_template).")
    hdr = [str(c or "").strip().lower() for c in rows[hdr_i]]

    def col(prefix: str) -> int | None:
        return next((i for i, h in enumerate(hdr) if h.startswith(prefix)), None)

    idx = {k: col(p) for k, p in (("id", "case"), ("a", "ensemble a"), ("b", "ensemble b"), ("types", "types"),
                                  ("tol_h_m", "tolérance horizontale"), ("tol_v_m", "tolérance verticale"),
                                  ("tol_volume_m3", "tolérance volume"), ("severity", "sévérité"),
                                  ("ignore_same_system", "ignorer même système"),
                                  ("ignore_same_layer", "ignorer même calque"), ("active", "active"))}
    cells = []
    for n, r in enumerate(rows[hdr_i + 1:], start=hdr_i + 2):
        def get(k, r=r):
            return r[idx[k]] if idx[k] is not None and idx[k] < len(r) else None

        if not get("a") and not get("b"):
            continue
        cell: dict[str, Any] = {"id": str(get("id") or f"L{n}"), "a": parse_set(get("a")), "b": parse_set(get("b"))}
        if get("types"):
            cell["types"] = [TYPES_FR.get(t.strip().lower(), t.strip().lower())
                             for t in str(get("types")).split(",") if t.strip()]
        for k in ("tol_h_m", "tol_v_m", "tol_volume_m3"):
            if get(k) not in (None, ""):
                try:
                    cell[k] = float(str(get(k)).replace(",", "."))
                except ValueError as exc:
                    raise ValueError(f"Ligne {n} : {k} invalide ({get(k)!r}).") from exc
        sev = str(get("severity") or "").strip().lower()
        if sev:
            if sev not in ("bloquant", "majeur", "mineur", "info"):
                raise ValueError(f"Ligne {n} : sévérité « {sev} » inconnue.")
            cell["severity"] = sev
        cell["ignore_same_system"] = _yes(get("ignore_same_system"))
        cell["ignore_same_layer"] = _yes(get("ignore_same_layer"))
        cell["active"] = get("active") in (None, "") or _yes(get("active"))
        cells.append(cell)
    if not cells:
        raise ValueError("Matrice vide : aucune ligne avec Ensemble A et Ensemble B.")
    return cells


__all__ = ["DEFAULT_MATRIX", "GROUP_LABELS", "disciplines", "run_cells", "read_matrix", "write_matrix_template",
           "volumes_rows", "VOLUME_COLS", "parse_set", "options_from"]
