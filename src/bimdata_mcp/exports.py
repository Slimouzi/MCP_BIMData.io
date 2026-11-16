"""Exports Excel : sélection d'éléments et COBie."""

from __future__ import annotations

import copy
from collections import Counter
from pathlib import Path
from typing import Any

from bim_core.model_snapshot import ModelSnapshot

from .charte import Col, Contexte, Livrable
from .cobie import build_sheets_from_bimdata, split_oversized_sheets
from .cobie.schema import COBIE_SHEET_ORDER, SHEET_HEADERS
from .model_view import Record, resolve_field

#: Libellés alignés sur le panneau « Propriétés » du viewer BIMData.
ATTRIBUTE_LABELS = {
    "GlobalId": "UUID", "IfcClass": "Type", "Name": "Nom", "LongName": "Nom long",
    "Description": "Description", "ObjectType": "Type d'objet", "PredefinedType": "PredefinedType", "Tag": "Tag",
}
ATTRIBUTE_ORDER = ["Name", "LongName", "Description", "ObjectType", "PredefinedType", "Tag"]
LOCATION_COLUMNS = [("Bâtiment", "Building"), ("Étage", "Storey"), ("Espace", "Space"), ("Zone", "Zone"),
                    ("Classification", "Classification"), ("Matériaux", "Material")]
GROUP_ATTRIBUTES = "Attributs IFC"
GROUP_LOCATION = "Localisation"
MAX_PROPERTY_COLUMNS = 600


def is_quantity_set(pset: str) -> bool:
    p = pset.lower()
    return p.startswith("qto_") or p.startswith("basequantities")


def build_columns(
    records: list[Record], *, properties: list[str] | str | None = "all", include_quantities: bool = True,
    include_location: bool = True, min_fill_rate: float = 0.0,
) -> list[tuple[str, str, str]]:
    """Colonnes ``(groupe, libellé, chemin)`` d'un export, dans l'ordre du viewer BIMData :
    attributs IFC → localisation → Psets → quantités (Qto)."""
    cols: list[tuple[str, str, str]] = [(GROUP_ATTRIBUTES, "UUID", "GlobalId"), (GROUP_ATTRIBUTES, "Type", "IfcClass")]
    present: dict[str, None] = {}
    for r in records:
        for k in r.attrs:
            present.setdefault(k, None)
    # Minimum toujours présent (panneau Propriétés BIMData) : Nom, Description, Type d'objet,
    # PredefinedType, Tag — même vides. Nom long et autres attributs : s'ils existent.
    always = {"Name", "Description", "ObjectType", "PredefinedType", "Tag"}
    ordered = [a for a in ATTRIBUTE_ORDER if a in present or a in always]
    ordered += sorted(a for a in present if a not in ATTRIBUTE_ORDER)
    cols += [(GROUP_ATTRIBUTES, ATTRIBUTE_LABELS.get(a, a), a) for a in ordered]
    if include_location:
        cols += [(GROUP_LOCATION, label, path) for label, path in LOCATION_COLUMNS]

    fill: dict[str, list[int]] = {}
    order: dict[str, None] = {}
    for r in records:
        for k, v in r.props.items():
            order.setdefault(k, None)
            f = fill.setdefault(k, [0, 0])
            f[0] += 1
            f[1] += 0 if v in (None, "") else 1
    n = max(len(records), 1)
    keys = list(order)
    wanted: set[str] | None = None
    if properties not in (None, "all", ["all"]):
        wanted = set()
        for p in properties:
            if p.endswith(".*"):
                wanted |= {k for k in keys if k.lower().startswith(p[:-1].lower())}
            else:
                wanted |= {k for k in keys if k.lower() == p.lower()} or {p}
    psets = [k for k in keys if not is_quantity_set(k.split(".", 1)[0])
             and (wanted is None and fill[k][1] / n >= min_fill_rate or wanted is not None and k in wanted)]
    qtos = [k for k in keys if is_quantity_set(k.split(".", 1)[0])
            and (include_quantities or (wanted is not None and k in wanted))]
    if wanted:
        psets += [p for p in sorted(wanted) if p not in order and "." in p]  # colonnes demandées absentes
    # Regrouper par Pset en gardant l'ordre de première apparition.
    def grouped(ks: list[str]) -> list[str]:
        by: dict[str, list[str]] = {}
        for k in ks:
            by.setdefault(k.split(".", 1)[0], []).append(k)
        return [k for g in by.values() for k in g]
    for k in (grouped(psets) + grouped(qtos))[:MAX_PROPERTY_COLUMNS]:
        pset, prop = k.split(".", 1)
        cols.append((pset, prop, k))
    return cols


def _sheet_name(base: str, used: set[str]) -> str:
    name = "".join(c for c in base if c not in '[]:*?/\\')[:31] or "Feuille"
    candidate, i = name, 2
    while candidate.lower() in used:
        candidate = f"{name[:28]}_{i}"
        i += 1
    used.add(candidate.lower())
    return candidate


def export_selection_xlsx(
    path: Path,
    records: list[Record],
    *,
    ctx: Contexte,
    selection_spec: dict | None,
    properties: list[str] | str | None = "all",
    include_quantities: bool = True,
    include_location: bool = True,
    layout: str = "par_classe",
    long_format: bool = False,
    min_fill_rate: float = 0.0,
) -> dict[str, Any]:
    """Export d'une sélection, à la trame BIMData : Synthèse → une feuille par classe IFC
    (groupes Attributs IFC / Localisation / Pset / Qto, comme le panneau Propriétés du viewer)
    → [Propriétés (liste)] → Lisez-moi → _bimdata."""
    if layout == "une_feuille":
        groups = {"Éléments": records}
    else:
        groups = {}
        for r in sorted(records, key=lambda r: (r.ifc_class, r.storey, r.name)):
            groups.setdefault(r.ifc_class or "Sans classe", []).append(r)
    plans = []
    used: set[str] = {"synthèse", "lisez-moi", "_bimdata", "propriétés (liste)"}
    for cls, recs in groups.items():
        cols = build_columns(recs, properties=properties, include_quantities=include_quantities,
                             include_location=include_location, min_fill_rate=min_fill_rate)
        plans.append((_sheet_name(cls, used), cls, recs, cols))
    n_props = len({p for *_, cols in plans for _, _, p in cols if "." in p})

    lv = Livrable(path, ctx)
    lv.synthese(kpis=[("Éléments", len(records), "integer"), ("Classes IFC", len(groups), "integer"),
                      ("Propriétés exportées", n_props, "integer"),
                      ("Étages", len({r.storey for r in records if r.storey}), "integer")],
                intro="Export des données de la sélection : attributs IFC, localisation, propriétés des Psets et "
                      "quantités (Qto), une feuille par classe IFC.")
    lv.tableau("Contenu du classeur", [Col("Feuille"), Col("Classe IFC"), Col("Éléments", kind="integer"),
                                       Col("Colonnes", kind="integer")],
               [[n, cls if layout != "une_feuille" else "(toutes)", len(recs), len(cols)]
                for n, cls, recs, cols in plans])
    lv.tableau("Répartition par étage", [Col("Étage"), Col("Éléments", kind="integer")],
               [[k, v] for k, v in Counter(r.storey or "(sans étage)" for r in records).most_common()])

    sheets_meta: dict[str, list[str]] = {}
    for name, cls, recs, cols in plans:
        lv.feuille(name, [Col(label, group) for group, label, _ in cols],
                   [[resolve_field(r, p).value for _, _, p in cols] for r in recs],
                   description=f"{len(recs)} élément(s) {cls} — attributs, localisation, Psets et Qto.",
                   freeze_cols=3)
        sheets_meta[name] = [p for _, _, p in cols]

    if long_format:
        rows = []
        for r in records:
            for key, val in r.props.items():
                pset, prop = key.split(".", 1)
                rows.append([r.uuid, r.ifc_class, r.name, pset, prop, val, r.prop_types.get(key, "")])
                if len(rows) >= 1_048_000:
                    break
        lv.feuille("Propriétés (liste)", [Col("UUID", "Élément"), Col("Type", "Élément"), Col("Nom", "Élément"),
                                          Col("Pset", "Propriété"), Col("Propriété", "Propriété"),
                                          Col("Valeur", "Propriété"), Col("Type de valeur", "Propriété")],
                   rows, description="Une ligne par propriété (format long, pour tableaux croisés).",
                   role="reference")

    lv.lisez_moi(
        "Export des données d'une sélection d'éléments de la maquette BIMData. La structure reprend le panneau "
        "Propriétés du viewer : Attributs IFC, puis chaque Pset, puis chaque jeu de quantités (Qto).",
        [("Corriger la maquette depuis ce fichier", [
            "Modifier les valeurs directement dans les feuilles par classe : seules les cellules différentes de la "
            "maquette produisent une correction.",
            "Ne pas modifier la colonne UUID ni les deux lignes d'en-tête.",
            "Lecture seule : UUID, Type, PredefinedType et groupe Localisation (à corriger dans le logiciel auteur).",
            "Ajouter une propriété : nouvelle colonne avec le Pset en ligne 1 et la propriété en ligne 2.",
            "Réimporter le fichier (prepare_corrections_from_file) : un plan d'écriture est présenté avant toute "
            "modification de la maquette."]),
         ("Critères de sélection", [str(selection_spec or "Maquette entière")])],
    )
    lv.meta({"format": "bimdata-mcp/selection-export/v2", "selection": ctx.perimetre, "spec": selection_spec or {},
             "header_rows": 2, "sheets": sheets_meta})
    lv.close()
    sheet_info = [{"sheet": n, "ifc_class": c, "rows": len(r), "columns": len(cols)} for n, c, r, cols in plans]
    return {"rows": len(records), "sheets": sheet_info, "layout": layout,
            "n_columns_max": max((s["columns"] for s in sheet_info), default=0)}


class SnapshotClient:
    """Façade ``bimdata-read`` servie depuis un snapshot déjà chargé.

    Le builder COBie lit un *client* ; lui donner celui-ci évite de retélécharger
    le modèle et permet de **restreindre** les composants à une sélection. Les
    routes absentes du snapshot (types, systèmes) sont déléguées au vrai client.
    """

    def __init__(self, snapshot: ModelSnapshot, real_client, uuids: set[str] | None = None):
        self._s = snapshot
        self._c = real_client
        self._uuids = uuids

    def get_project(self):
        return copy.deepcopy(self._s.project)

    def get_model(self):
        return copy.deepcopy(self._s.model)

    def get_buildings(self):
        return copy.deepcopy(self._s.buildings)

    def get_storeys(self):
        return copy.deepcopy(self._s.storeys)

    def get_spaces(self):
        spaces = copy.deepcopy(self._s.spaces)
        if self._uuids is not None:
            spaces = [sp for sp in spaces if sp.get("uuid") in self._uuids] or spaces
        return spaces

    def get_zones(self):
        return copy.deepcopy(self._s.zones)

    def get_raw_elements(self):
        els = self._s.elements
        if self._uuids is not None:
            keep_types = {"IfcSpace", "IfcBuildingStorey", "IfcBuilding", "IfcSite"}
            els = [e for e in els if e.get("uuid") in self._uuids or e.get("type") in keep_types]
        return copy.deepcopy(els)

    def get_structure_tree(self):
        return self._s.structure_tree

    def get_systems_diagnostic(self):
        return self._c.get_systems_diagnostic()

    def get_element_types_diagnostic(self):
        return self._c.get_element_types_diagnostic()


COBIE_DESCRIPTIONS = {
    "Facility": "Projet, site et bâtiment.", "Floor": "Niveaux (étages).", "Space": "Espaces (pièces).",
    "Zone": "Zones regroupant des espaces.", "Type": "Types d'équipements / d'ouvrages.",
    "Component": "Composants (occurrences).", "System": "Systèmes techniques.",
    "Attribute": "Propriétés des objets (une ligne par propriété).",
}


def export_cobie_xlsx(path: Path, snapshot: ModelSnapshot, real_client, uuids: set[str] | None, *,
                      ctx: Contexte) -> dict:
    """COBie à la trame BIMData. Les feuilles COBie gardent **leurs noms et colonnes normalisés**
    (en-tête sur une ligne, exigé par les outils COBie) ; Synthèse, Lisez-moi et style sont communs."""
    sheets, report = build_sheets_from_bimdata(SnapshotClient(snapshot, real_client, uuids))
    sheets, split_map = split_oversized_sheets(sheets)
    lv = Livrable(path, ctx)
    counts = report.get("counts", {})
    lv.synthese(kpis=[("Composants", counts.get("Component", 0), "integer"),
                      ("Types", counts.get("Type", 0), "integer"),
                      ("Espaces", counts.get("Space", 0), "integer"), ("Niveaux", counts.get("Floor", 0), "integer"),
                      ("Attributs", counts.get("Attribute", 0), "integer")],
                intro="Classeur COBie (Construction Operations Building information exchange) généré depuis la "
                      "maquette BIMData.")
    lv.tableau("Feuilles COBie", [Col("Feuille"), Col("Contenu"), Col("Lignes", kind="integer")],
               [[n, COBIE_DESCRIPTIONS.get(n.split("_")[0], ""), len(sheets.get(n, []))] for n in sheets])
    warn = list(report.get("warnings") or [])
    if report.get("type_source") != "element_type":
        warn.append("Feuille Type déduite des composants (route des types d'éléments non disponible).")
    if report.get("system_source") == "absent":
        warn.append("Aucun système (IfcSystem) dans la maquette : feuille System vide.")
    lv.tableau("Points d'attention", [Col("Message")], [[w] for w in warn] or [["Aucun."]])
    order = [n for base in COBIE_SHEET_ORDER for n in (split_map.get(base) or [base]) if n in sheets]
    order += [n for n in sheets if n not in order]
    for name in order:
        base = name.split("_")[0] if name.split("_")[0] in SHEET_HEADERS else name
        headers = SHEET_HEADERS.get(base) or (list(sheets[name][0].keys()) if sheets[name] else [])
        lv.feuille(name, [Col(h) for h in headers], [[row.get(h) for h in headers] for row in sheets[name]],
                   description=COBIE_DESCRIPTIONS.get(base, "Feuille COBie."), group_row=False)
    lv.lisez_moi(
        "Classeur COBie structuré en 8 feuilles (Facility, Floor, Space, Zone, Type, Component, System, "
        "Attribute), à la trame BIMData.",
        [("À savoir", ["Les noms de feuilles et de colonnes COBie sont normalisés : ne pas les renommer.",
                       "Les feuilles trop longues pour Excel sont scindées en Nom, Nom_2… sans perte de données."])])
    lv.meta({"format": "bimdata-mcp/cobie/v1", "counts": counts})
    lv.close()
    report["split_sheets"] = split_map
    report["overflows"] = []
    return report
