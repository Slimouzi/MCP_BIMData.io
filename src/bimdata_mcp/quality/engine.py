"""Moteur de contrôle : applique des ``Requirement`` à la vue ``Record``.

Chaque couple (élément, exigence applicable) produit un **statut** ; les
statuts non conformes deviennent des **anomalies** porteuses, quand c'est
possible, d'une **proposition de correction** chiffrée en confiance.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from ..model_view import Record, normalize_text, resolve_field
from ..selection import _class_match, _num, as_bool
from .requirements import SEVERITY_ORDER, Requirement

STATUS_LABELS = {
    "conforme": "Conforme",
    "absent": "Propriété absente",
    "mauvais_pset": "Présente dans un autre Pset",
    "vide": "Valeur vide",
    "type_invalide": "Type de valeur invalide",
    "hors_liste": "Valeur hors liste autorisée",
    "format_invalide": "Format invalide",
    "interdit": "Motif interdit / nom générique",
    "trop_court": "Trop court",
    "trop_long": "Trop long",
    "hors_plage": "Hors plage",
    "doublon": "Doublon",
    "optionnel_absent": "Optionnel non renseigné",
}
NON_CONFORM = {k for k in STATUS_LABELS if k not in ("conforme", "optionnel_absent")}


@dataclass
class Issue:
    issue_id: str
    req_id: str
    req_label: str
    category: str
    severity: str
    uuid: str
    ifc_class: str
    name: str
    storey: str
    field: str
    location: str
    status: str
    actual: Any
    expected: str
    message: str
    writable: bool
    proposal: Any = None
    confidence: float = 0.0
    proposal_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckResult:
    n_elements: int
    n_requirements: int
    n_checks: int
    issues: list[Issue] = field(default_factory=list)
    per_requirement: list[dict] = field(default_factory=list)
    cells: dict[str, dict[str, tuple[Any, str]]] = field(default_factory=dict)  # uuid -> field -> (val, status)
    checked_uuids: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        nc = len(self.issues)
        by_sev = Counter(i.severity for i in self.issues)
        by_status = Counter(i.status for i in self.issues)
        n_with_issue = len({i.uuid for i in self.issues})
        return {
            "elements_controlled": self.n_elements,
            "requirements": self.n_requirements,
            "checks": self.n_checks,
            "non_conformities": nc,
            "conformity_rate": round(1 - nc / self.n_checks, 4) if self.n_checks else 1.0,
            "elements_with_issue": n_with_issue,
            "by_severity": dict(sorted(by_sev.items(), key=lambda kv: SEVERITY_ORDER.get(kv[0], 9))),
            "by_status": {STATUS_LABELS.get(k, k): v for k, v in by_status.most_common()},
            "proposals": sum(1 for i in self.issues if i.proposal not in (None, "")),
            "proposals_high_confidence": sum(1 for i in self.issues if i.confidence >= 0.8),
        }


def applicable(rec: Record, req: Requirement) -> bool:
    if req.ifc_classes:
        return _class_match(rec.ifc_class, req.ifc_classes, True)
    return rec.is_physical


def _type_ok(value: Any, vt: str | None) -> bool:
    if not vt or vt == "string":
        return True
    if isinstance(value, (list, tuple)):  # ex. IfcCompoundPlaneAngleMeasure (latitude en [°, ', ", µ"])
        return bool(value) and all(_type_ok(v, vt) for v in value)
    if vt == "boolean":
        return as_bool(value) is not None
    if vt == "integer":
        n = _num(value)
        return n is not None and float(n).is_integer()
    if vt == "number":
        return _num(value) is not None
    if vt == "date":
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}|^\d{2}/\d{2}/\d{4}", str(value)))
    return True


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _full(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern, value) is not None


def _candidates(text: str) -> list[tuple[str, str]]:
    """Transformations usuelles d'un nom, de la plus sûre à la plus forte."""
    t = text
    out = []
    a = re.sub(r"\s+", " ", t).strip()
    out.append((a, "espaces superflus supprimés"))
    out.append((a.upper(), "mise en majuscules"))
    out.append((_strip_accents(a), "accents supprimés"))
    out.append((_strip_accents(a).upper(), "majuscules sans accents"))
    for sep in ("-", "_", ""):
        out.append((re.sub(r"[\s_\-]+", sep, a), f"séparateurs remplacés par « {sep or 'rien'} »"))
        out.append((re.sub(r"[\s_\-]+", sep, _strip_accents(a)).upper(), f"majuscules, séparateur « {sep or 'rien'} »"))
    out.append((re.sub(r"[<>|\"\\*?\t\r\n]", "", a), "caractères interdits supprimés"))
    return out


def _best_allowed(value: str, allowed: list[str]) -> tuple[str | None, float, str]:
    nv = normalize_text(value)
    for a in allowed:
        if normalize_text(a) == nv:
            return a, 0.98, "écart de casse/accents uniquement"
    compact = re.sub(r"[\s_\-./]+", "", nv)
    for a in allowed:
        if re.sub(r"[\s_\-./]+", "", normalize_text(a)) == compact:
            return a, 0.95, "écart d'espaces / séparateurs uniquement"
    try:
        from rapidfuzz import fuzz, process

        best = process.extractOne(nv, {a: normalize_text(a) for a in allowed}, scorer=fuzz.WRatio)
    except Exception:  # noqa: BLE001
        best = None
    if best and best[1] >= 85:
        return best[2], round(best[1] / 100 * 0.85, 2), f"valeur autorisée la plus proche ({best[1]:.0f} %)"
    return None, 0.0, ""


def _propose(rec: Record, req: Requirement, status: str, actual: Any, other: tuple[str, ...]) -> tuple[Any, float, str]:
    if not req.writable:
        return None, 0.0, ""
    if status == "mauvais_pset" and other:
        src = other[0].split(":", 1)[1]
        val = rec.props.get(src)
        if val not in (None, ""):
            return val, 0.9, f"valeur reprise de {src}"
    if status == "type_invalide":
        if req.value_type in ("number", "integer"):
            n = _num(str(actual).replace(" ", ""))
            if n is not None:
                return (int(n) if req.value_type == "integer" else n), 0.9, "conversion numérique"
        if req.value_type == "boolean":
            b = as_bool(actual)
            if b is not None:
                return b, 0.9, "conversion booléenne"
    if status == "hors_liste" and req.allowed_values:
        v, c, why = _best_allowed(str(actual), req.allowed_values)
        if v is not None:
            return v, c, why
    if status in ("format_invalide", "interdit", "trop_long") and isinstance(actual, str):
        for cand, why in _candidates(actual):
            if cand == actual or not cand:
                continue
            if _value_status(rec, req, cand) == "conforme":
                return cand, 0.8 if status != "interdit" else 0.7, why
        if status == "interdit":
            stripped = re.sub(r":\d{4,}$", "", actual).strip()
            if stripped and stripped != actual and _value_status(rec, req, stripped) == "conforme":
                return stripped, 0.5, "identifiant logiciel retiré du nom"
    if status in ("vide", "absent"):
        f = req.field.lower()
        if f == "longname" and rec.attrs.get("Name"):
            return rec.attrs["Name"], 0.4, "repris de Name"
        if f == "name" and rec.attrs.get("LongName"):
            return rec.attrs["LongName"], 0.4, "repris de LongName"
        if f == "name" and rec.attrs.get("ObjectType"):
            return rec.attrs["ObjectType"], 0.3, "repris du type (ObjectType)"
        if req.allowed_values and len(req.allowed_values) == 1:
            return req.allowed_values[0], 0.6, "seule valeur autorisée"
    return None, 0.0, ""


def _value_status(rec: Record, req: Requirement, value: Any) -> str:
    text = "" if value is None else str(value)
    if not _type_ok(value, req.value_type):
        return "type_invalide"
    if req.allowed_values:
        allowed = {normalize_text(a) for a in req.allowed_values}
        vals = [v for v in re.split(r"\s*\|\s*", text)] if " | " in text else [text]
        if req.value_type == "boolean":
            ok = as_bool(value) is not None and any(as_bool(a) == as_bool(value) for a in req.allowed_values)
        else:
            ok = all(normalize_text(v) in allowed for v in vals)
        if not ok:
            return "hors_liste"
    if req.pattern and not _full(req.pattern, text):
        return "format_invalide"
    for fp in req.forbidden_patterns or []:
        if re.search(fp, text, re.IGNORECASE):
            return "interdit"
    if req.min_length is not None and len(text) < req.min_length:
        return "trop_court"
    if req.max_length is not None and len(text) > req.max_length:
        return "trop_long"
    if req.min_value is not None or req.max_value is not None:
        n = _num(value)
        if n is None or (req.min_value is not None and n < req.min_value) or (
            req.max_value is not None and n > req.max_value
        ):
            return "hors_plage"
    return "conforme"


def _expected(req: Requirement) -> str:
    parts = []
    if req.required:
        parts.append("obligatoire")
    if req.value_type:
        parts.append(f"type {req.value_type}")
    if req.allowed_values:
        av = req.allowed_values
        parts.append("valeurs : " + " ; ".join(av[:12]) + (" …" if len(av) > 12 else ""))
    if req.pattern:
        parts.append(f"format /{req.pattern}/")
    if req.forbidden_patterns:
        parts.append("sans motif interdit")
    if req.max_length:
        parts.append(f"≤ {req.max_length} car.")
    if req.unique_scope:
        parts.append(f"unique ({req.unique_scope})")
    if req.example:
        parts.append(f"ex. {req.example}")
    return ", ".join(parts)


def run_check(records: list[Record], requirements: list[Requirement], *, phase: str | None = None) -> CheckResult:
    reqs = [r for r in requirements if r.applies_to_phase(phase)]
    res = CheckResult(n_elements=0, n_requirements=len(reqs), n_checks=0)
    controlled: set[str] = set()
    counter = 0
    for req in reqs:
        stats = Counter()
        uniq: dict[tuple, list[Record]] = defaultdict(list)
        exp = _expected(req)
        for rec in records:
            if not applicable(rec, req):
                continue
            controlled.add(rec.uuid)
            fv = resolve_field(rec, req.field)
            actual = fv.value
            if not fv.found:
                if req.required:
                    status = "mauvais_pset" if (not fv.location and fv.other_locations) else (
                        "vide" if fv.location else "absent")
                else:
                    status = "optionnel_absent"
            else:
                status = _value_status(rec, req, actual)
                if req.unique_scope and status == "conforme":
                    scope = {"model": "", "building": rec.building, "storey": rec.storey}[req.unique_scope]
                    uniq[(scope, normalize_text(actual))].append(rec)
            stats[status] += 1
            res.n_checks += 1 if status != "optionnel_absent" else 0
            res.cells.setdefault(rec.uuid, {})[req.field] = (actual, status)
            if status in NON_CONFORM:
                counter += 1
                prop, conf, why = _propose(rec, req, status, actual, fv.other_locations)
                res.issues.append(Issue(
                    issue_id=f"I{counter:06d}", req_id=req.id, req_label=req.label or req.field,
                    category=req.category, severity=req.severity, uuid=rec.uuid,
                    ifc_class=rec.ifc_class, name=rec.name, storey=rec.storey, field=req.field,
                    location=fv.location or (fv.other_locations[0] if fv.other_locations else ""),
                    status=status, actual=actual, expected=exp,
                    message=STATUS_LABELS[status] + (f" (trouvée en {fv.other_locations[0].split(':', 1)[1]})"
                                                     if status == "mauvais_pset" else ""),
                    writable=req.writable, proposal=prop, confidence=conf, proposal_reason=why,
                ))
        for (_scope, _val), group in uniq.items():
            if len(group) < 2:
                continue
            for rec in group:
                stats["doublon"] += 1
                stats["conforme"] -= 1
                counter += 1
                res.cells[rec.uuid][req.field] = (res.cells[rec.uuid][req.field][0], "doublon")
                res.issues.append(Issue(
                    issue_id=f"I{counter:06d}", req_id=req.id, req_label=req.label or req.field,
                    category=req.category, severity=req.severity, uuid=rec.uuid, ifc_class=rec.ifc_class,
                    name=rec.name, storey=rec.storey, field=req.field, location=f"attr:{req.field}",
                    status="doublon", actual=resolve_field(rec, req.field).value, expected=exp,
                    message=f"Doublon : {len(group)} éléments portent cette valeur ({req.unique_scope})",
                    writable=req.writable,
                ))
        applicable_n = sum(v for k, v in stats.items() if k != "optionnel_absent")
        nc = sum(v for k, v in stats.items() if k in NON_CONFORM)
        res.per_requirement.append({
            "req_id": req.id, "label": req.label or req.field, "field": req.field,
            "ifc_classes": ", ".join(req.ifc_classes) or "(tous éléments physiques)",
            "severity": req.severity, "category": req.category, "applicable": applicable_n,
            "conformes": applicable_n - nc, "non_conformes": nc,
            "taux": round((applicable_n - nc) / applicable_n, 4) if applicable_n else None,
            "optionnels_absents": stats.get("optionnel_absent", 0),
        })
    res.n_elements = len(controlled)
    res.checked_uuids = sorted(controlled)
    res.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.req_id, i.ifc_class, i.name))
    return res
