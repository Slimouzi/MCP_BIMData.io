"""Rapport Excel d'un contrôle par règles (structure, géométrie…) — trame BIMData.

Synthèse (chiffres clés, résultat par règle, points d'attention) → une feuille par
règle en anomalie → Lisez-moi → _bimdata. Lecture seule : ces anomalies se
corrigent dans le logiciel auteur.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..charte import Col, Contexte, Livrable
from ..geometry.clash import GROUP_LABELS
from .engine import STATUS_LABELS, CheckResult
from .rules import RULES

ISSUE_COLS = [
    Col("N°", "Anomalie", width=9), Col("UUID", "Élément", width=24), Col("Classe IFC", "Élément", width=20),
    Col("Nom", "Élément", width=32), Col("Étage", "Élément", width=14), Col("Local", "Élément", width=12),
    Col("Criticité", "Constat", kind="severity", width=10), Col("Statut", "Constat", width=18),
    Col("Constat", "Constat", width=60), Col("Mesure", "Mesure", kind="number", width=10),
    Col("Unité", "Mesure", width=7), Col("Seuil", "Mesure", kind="number", width=9),
    Col("Source du seuil", "Mesure", width=20), Col("Éléments liés", "Contexte", width=30),
    Col("Méthode", "Contexte", width=10), Col("Centre (x ; y ; z)", "Contexte", width=30),
]


def _sheet_name(rule_id: str, label: str, used: set[str]) -> str:
    base = re.sub(r"[\[\]:*?/\\]", " ", f"{rule_id} {label}")[:31].strip()
    name, k = base, 2
    while name.lower() in used:
        name = f"{base[:28]} {k}"
        k += 1
    used.add(name.lower())
    return name


CLASH_COLS = [Col("Type de conflit", "Conflit", width=14), Col("Groupe", "Conflit", width=30),
              Col("Case", "Conflit", width=12), Col("UUID B", "Élément B", width=24),
              Col("Classe IFC B", "Élément B", width=20), Col("Nom B", "Élément B", width=32),
              Col("Pénétration H (m)", "Mesure", kind="number", width=10),
              Col("Pénétration V (m)", "Mesure", kind="number", width=10),
              Col("Volume (m³)", "Mesure", kind="number", width=10), Col("Orientation", "Mesure", width=14)]
GROUP_ORDER = ["doublons", "inclus", "similaires_plusieurs_etages", "etages_differents", "similaires_un_etage",
               "autres"]


def write_rules_report(path: Path, result: CheckResult, *, ctx: Contexte, check_id: str, notes: list[str],
                       intro: str, extra_meta: dict | None = None, extra_tables: list[tuple] | None = None,
                       extra_sheets: list[dict] | None = None, names: dict[str, tuple[str, str]] | None = None
                       ) -> dict:
    """``extra_tables`` : tableaux (titre, colonnes, lignes) sous la Synthèse ; ``extra_sheets`` : feuilles
    {nom, cols, rows, description} avant le Lisez-moi ; ``names`` : uuid → (classe, nom) pour l'élément B."""
    names = names or {}
    summ = result.summary()
    by_sev = summ["by_severity"]
    lv = Livrable(path, ctx)
    lv.synthese(
        kpis=[("Éléments contrôlés", summ["elements_controlled"], "integer"),
              ("Règles", summ["requirements"], "integer"), ("Anomalies", summ["non_conformities"], "integer"),
              ("Bloquantes", by_sev.get("bloquant", 0), "integer"), ("Majeures", by_sev.get("majeur", 0), "integer"),
              ("Mineures", by_sev.get("mineur", 0), "integer"), ("Information", by_sev.get("info", 0), "integer")],
        intro=intro,
    )

    def taux_state(i, c, v):
        if c != 7 or not isinstance(v, float):
            return None
        return "ok" if v >= 0.999 else ("warn" if v >= 0.8 else "ko")

    lv.tableau("Résultat par règle",
               [Col("Règle"), Col("Libellé"), Col("Méthode"), Col("Criticité", kind="severity"),
                Col("Contrôlés", kind="integer"), Col("Anomalies", kind="integer"), Col("Description"),
                Col("Taux", kind="percent")],
               [[p["req_id"], p["label"], p["field"], p["severity"], p["applicable"], p["non_conformes"],
                 p["ifc_classes"], p["taux"] if p["taux"] is not None else "—"] for p in result.per_requirement],
               states=taux_state)
    for title, cols, rows in extra_tables or []:
        lv.tableau(title, cols, rows)
    if notes:
        lv.tableau("Points d'attention", [Col("Note")], [[n] for n in notes])

    groups: dict[str, list] = defaultdict(list)
    for iss in result.issues:
        groups[iss.req_id].append(iss)
    used = {"synthèse", "lisez-moi", "_bimdata"}
    for p in result.per_requirement:
        items = groups.get(p["req_id"])
        if not items:
            continue
        rule = RULES.get(p["req_id"])
        is_clash = any(i.clash_type for i in items)
        if is_clash:  # catégories Solibri, puis volume décroissant
            items = sorted(items, key=lambda i: (GROUP_ORDER.index(i.group) if i.group in GROUP_ORDER else 9,
                                                 -((i.measure or {}).get("volume_m3") or 0)))
        rows = []
        for iss in items:
            m, t = iss.measure or {}, iss.threshold or {}
            linked = [f"{e.get('role')}: {e.get('uuid')}" for e in (iss.elements or []) if e.get("uuid") != iss.uuid]
            c = (iss.where or {}).get("center")
            rows.append([iss.issue_id, iss.uuid, iss.ifc_class, iss.name, iss.storey, (iss.where or {}).get("space"),
                         iss.severity, STATUS_LABELS.get(iss.status, iss.status), iss.message, m.get("value"),
                         m.get("unit") or t.get("unit"), t.get("value"), t.get("source"), " | ".join(linked),
                         iss.method, " ; ".join(f"{x:.3f}" for x in c) if c else ""])
            if is_clash:
                b = next((e.get("uuid") for e in iss.elements or [] if e.get("role") == "B"), "")
                bc, bn = names.get(b, ("", ""))
                rows[-1] += [iss.clash_type, GROUP_LABELS.get(iss.group, iss.group), iss.cell, b, bc, bn,
                             m.get("depth_h_m"), m.get("depth_v_m"), m.get("volume_m3"), m.get("orientation")]
        lv.feuille(_sheet_name(p["req_id"], p["label"], used), ISSUE_COLS + (CLASH_COLS if is_clash else []), rows,
                   freeze_cols=2,
                   description=f"{p['req_id']} — {p['label']} : {rule.description if rule else ''}")

    for sh in extra_sheets or []:
        lv.feuille(sh["name"], sh["cols"], sh["rows"], description=sh["description"], freeze_cols=1)

    lv.lisez_moi(
        "Rapport de contrôle par règles de la maquette BIMData : une feuille par règle en anomalie, avec la mesure, "
        "le seuil appliqué et la méthode (Données = propriétés et relations IFC ; Bbox = boîtes englobantes ; "
        "Maillage = calcul sur les triangles).",
        [("Lecture", [
            "Les seuils sont indicatifs et paramétrables : ce rapport est une aide à la décision, pas une attestation "
            "de conformité réglementaire.",
            "Ces anomalies se corrigent dans le logiciel auteur (structure spatiale, relations, géométrie), puis la "
            "maquette est republiée sur BIMData.",
            "Coordonnées en mètres, repère projet du fichier IFC.",
            "Smart View ou topics BCF : sur demande, à partir du check_id de ce contrôle."])],
    )
    lv.meta({"format": "bimdata-mcp/rules-report/v1", "check_id": check_id, **(extra_meta or {})})
    lv.close()
    return {"issues_written": len(result.issues), "sheets": [p["req_id"] for p in result.per_requirement
                                                             if groups.get(p["req_id"])]}
