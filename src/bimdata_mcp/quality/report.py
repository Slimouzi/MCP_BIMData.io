"""Rapport Excel d'un contrôle, à la trame BIMData (cf. :mod:`bimdata_mcp.charte`).

Synthèse → Erreurs à corriger (colonnes jaunes à saisir, réimportable) → Données
contrôlées → Exigences → Lisez-moi → _bimdata.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..charte import Col, Contexte, Livrable
from ..model_view import Record
from .engine import STATUS_LABELS, CheckResult
from .requirements import Requirement

ISSUE_COLUMNS = [
    Col("N°", "Élément", width=9), Col("UUID", "Élément", width=24), Col("Type", "Élément", width=20),
    Col("Nom", "Élément", width=30), Col("Étage", "Élément", width=14),
    Col("Réf.", "Exigence", width=8), Col("Libellé", "Exigence", width=28), Col("Catégorie", "Exigence", width=12),
    Col("Criticité", "Exigence", kind="severity", width=10), Col("Champ", "Exigence", width=28),
    Col("Emplacement trouvé", "Constat", width=26), Col("Statut", "Constat", width=22),
    Col("Valeur actuelle", "Constat", width=24), Col("Attendu", "Constat", width=32),
    Col("Proposition", "Proposition", width=24), Col("Confiance", "Proposition", kind="percent", width=10),
    Col("Motif", "Proposition", width=24),
    Col("Mode de correction", "Correction à saisir", width=20),
    Col("Valeur corrigée", "Correction à saisir", kind="edit", width=26),
    Col("Appliquer (Oui/Non)", "Correction à saisir", kind="edit", width=12),
]
COL_STATUS, COL_VALUE, COL_APPLY = 11, 18, 19
MAX_DATA_COLUMNS = 250


def _group_of(field: str) -> tuple[str, str]:
    if field.startswith("*."):
        return "Tous Psets", field[2:]
    if "." in field:
        pset, prop = field.split(".", 1)
        return pset, prop
    if field in ("Building", "Storey", "Space", "Zone", "Classification", "Material", "Layer"):
        return "Localisation", field
    return "Attributs IFC", field


def write_check_report(
    path: Path, result: CheckResult, requirements: list[Requirement], records: dict[str, Record], *,
    check_id: str, ctx: Contexte, requirements_id: str | None, auto_fill_threshold: float = 0.8,
) -> dict:
    summ = result.summary()
    lv = Livrable(path, ctx)
    lv.synthese(
        kpis=[("Éléments contrôlés", summ["elements_controlled"], "integer"),
              ("Exigences", summ["requirements"], "integer"), ("Contrôles", summ["checks"], "integer"),
              ("Non-conformités", summ["non_conformities"], "integer"),
              ("Taux de conformité", summ["conformity_rate"], "percent"),
              ("Propositions", summ["proposals"], "integer")],
        intro="Contrôle des données de la maquette : chaque élément applicable est confronté à chaque exigence. "
              "Les non-conformités et leurs propositions de correction figurent dans « Erreurs à corriger ».",
    )

    def taux_state(i, c, v):
        if c != 8 or not isinstance(v, float):
            return None
        return "ok" if v >= 0.999 else ("warn" if v >= 0.8 else "ko")

    lv.tableau("Conformité par exigence",
               [Col("Réf."), Col("Libellé"), Col("Classes IFC"), Col("Champ"), Col("Criticité", kind="severity"),
                Col("Applicable", kind="integer"), Col("Conformes", kind="integer"),
                Col("Non conformes", kind="integer"), Col("Taux", kind="percent")],
               [[p["req_id"], p["label"], p["ifc_classes"], p["field"], p["severity"], p["applicable"],
                 p["conformes"], p["non_conformes"], p["taux"] if p["taux"] is not None else "—"]
                for p in sorted(result.per_requirement, key=lambda p: -p["non_conformes"])],
               states=taux_state, max_rows=150)
    lv.tableau("Répartition des anomalies par statut", [Col("Statut"), Col("Anomalies", kind="integer")],
               [[k, v] for k, v in summ["by_status"].items()] or [["Aucune anomalie", 0]])
    lv.tableau("Anomalies par classe IFC", [Col("Classe IFC"), Col("Anomalies", kind="integer")],
               [[k, v] for k, v in Counter(i.ifc_class for i in result.issues).most_common(30)] or [["—", 0]])

    # Erreurs à corriger
    rows, writable = [], []
    for iss in result.issues:
        prefill = iss.proposal if (iss.writable and iss.proposal not in (None, "")
                                   and iss.confidence >= auto_fill_threshold) else ""
        rows.append([iss.issue_id, iss.uuid, iss.ifc_class, iss.name, iss.storey, iss.req_id, iss.req_label,
                     iss.category, iss.severity, iss.field, iss.location.split(":", 1)[-1],
                     STATUS_LABELS.get(iss.status, iss.status), iss.actual, iss.expected, iss.proposal,
                     iss.confidence or None, iss.proposal_reason,
                     "Par ce fichier" if iss.writable else "Logiciel auteur",
                     prefill if iss.writable else "—", ("Oui" if prefill not in ("", None) else "") if iss.writable else "—"])
        writable.append(iss.writable)

    def issue_state(i, c, v):
        if c == COL_STATUS:
            return "ko"
        if c in (COL_VALUE, COL_APPLY) and not writable[i]:
            return "na"
        return None

    lv.feuille("Erreurs à corriger", ISSUE_COLUMNS, rows, role="saisie", freeze_cols=4, states=issue_state,
               validations={COL_APPLY: ["Oui", "Non"]},
               description="Une ligne par non-conformité, avec proposition ; colonnes jaunes à compléter puis "
                           "réimporter le fichier pour corriger la maquette.")

    # Données contrôlées
    fields: list[str] = []
    for req in requirements:
        if req.field not in fields:
            fields.append(req.field)
    truncated = len(fields) > MAX_DATA_COLUMNS
    fields = fields[:MAX_DATA_COLUMNS]
    cols = [Col("UUID", "Élément", width=24), Col("Type", "Élément", width=20), Col("Nom", "Élément", width=30),
            Col("Étage", "Élément", width=14), Col("Anomalies", "Élément", kind="integer", width=11)]
    cols += [Col(_group_of(f)[1], _group_of(f)[0]) for f in fields]
    n_issues = Counter(i.uuid for i in result.issues)
    data, cell_states = [], []
    for uuid in result.checked_uuids:
        rec = records.get(uuid)
        if rec is None:
            continue
        cells = result.cells.get(uuid, {})
        row = [rec.uuid, rec.ifc_class, rec.name, rec.storey, n_issues.get(uuid, 0)]
        sts = [None, None, None, None, "ko" if n_issues.get(uuid) else "ok"]
        for f in fields:
            if f not in cells:
                row.append(None)
                sts.append(None)
                continue
            val, status = cells[f]
            row.append(val if val not in (None, "") else (STATUS_LABELS[status] if status != "conforme" else ""))
            sts.append("ok" if status == "conforme" else ("warn" if status == "optionnel_absent" else "ko"))
        data.append(row)
        cell_states.append(sts)
    lv.feuille("Données contrôlées", cols, data, freeze_cols=3,
               states=lambda i, c, v: cell_states[i][c] if c < len(cell_states[i]) else None,
               description="Une ligne par élément contrôlé, une colonne par champ exigé (vert conforme, rouge non "
                           "conforme, orange optionnel absent).")

    # Exigences
    lv.feuille("Exigences",
               [Col("Réf.", "Exigence", width=8), Col("Libellé", "Exigence", width=30),
                Col("Catégorie", "Exigence", width=12), Col("Criticité", "Exigence", kind="severity", width=10),
                Col("Classes IFC", "Cible", width=26), Col("Champ", "Cible", width=30), Col("Phases", "Cible", width=22),
                Col("Obligatoire", "Règle", width=11), Col("Type", "Règle", width=9),
                Col("Valeurs autorisées", "Règle", width=30), Col("Format (regex)", "Règle", width=22),
                Col("Motifs interdits", "Règle", width=22), Col("Long. max", "Règle", kind="integer", width=9),
                Col("Unicité", "Règle", width=9), Col("Exemple", "Règle", width=16), Col("Source", "Origine", width=20)],
               [[q.id, q.label, q.category, q.severity, " ".join(q.ifc_classes) or "(tous éléments physiques)", q.field,
                 " ".join(q.phases) or "Toutes", "Oui" if q.required else "Non", q.value_type or "",
                 " ; ".join(q.allowed_values or []), q.pattern or "", " ; ".join(q.forbidden_patterns or []),
                 q.max_length, q.unique_scope or "", q.example or "", q.source or ""] for q in requirements],
               role="reference", description="Exigences appliquées lors de ce contrôle.")

    lv.lisez_moi(
        "Rapport de contrôle des données de la maquette BIMData : synthèse, erreurs à corriger avec propositions, "
        "données contrôlées élément par élément et rappel des exigences.",
        [("Corriger la maquette depuis ce fichier", [
            "1. Feuille « Erreurs à corriger » : renseigner « Valeur corrigée » et mettre « Oui » dans « Appliquer ».",
            f"   Les propositions de confiance ≥ {auto_fill_threshold:.0%} sont déjà reportées : les relire, les "
            "modifier ou passer « Appliquer » à « Non ».",
            "2. Réimporter le fichier (prepare_corrections_from_file) : un plan d'écriture est présenté, rien n'est "
            "écrit sans confirmation.",
            "3. Appliquer le plan, puis relancer le contrôle pour vérifier.",
            "Lignes « Logiciel auteur » : rattachement spatial, matériau, classification, doublons — à corriger dans "
            "le logiciel de modélisation.",
            "Pour vider une valeur, saisir <vide> dans « Valeur corrigée ».",
            "Ne pas modifier les colonnes UUID et Champ ni les deux lignes d'en-tête."])],
    )
    lv.meta({"format": "bimdata-mcp/check-report/v2", "check_id": check_id, "requirements_id": requirements_id or "",
             "phase": ctx.phase, "issue_sheet": "Erreurs à corriger", "header_rows": 2})
    lv.close()
    return {"issues_written": len(result.issues), "data_columns": len(fields), "data_columns_truncated": truncated}
