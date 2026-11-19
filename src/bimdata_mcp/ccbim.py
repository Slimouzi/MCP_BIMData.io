"""Audit spécifique selon le cahier des charges BIM (CCBIM) du projet.

Parcours :

1. ``read_ccbim`` : le document (PDF, Word, Excel, texte) est lu et découpé par page ; le texte et les passages
   utiles (nommage, propriétés, classification, niveaux, format IFC…) sont rendus à l'assistant ;
2. l'assistant en extrait les **attendus contrôlables** (exigences de données, contrôles de maquette) et ceux qui
   ne le sont pas, chacun avec sa source (page, article), puis ``set_ccbim_expectations`` les enregistre ;
3. ``run_audit("ccbim")`` présente les attendus comme hypothèses à valider, puis contrôle la maquette et produit
   un rapport PDF et un classeur Excel.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .quality.engine import CheckResult, Issue, run_check
from .quality.requirements import Requirement

KIND = "ccbim"
MAX_CHARS_PER_CALL = 45_000
SUPPORTED = (".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv")

#: Mots-clés des passages utiles à un contrôle de maquette.
KEYWORDS = re.compile(
    r"(?i)\b(nomm|codif|nomenclatur|propri[ée]t|pset|param[èe]tre|attribut|classif|uniformat|omniclass|uniclass|"
    r"niveau|[ée]tage|storey|ifc ?(2x3|4)|format|g[ée]or[ée]f|coordonn|lod|loi|log|niveau de d[ée]tail|"
    r"objet|maquette|local|pi[èe]ce|ifcspace|zone|mat[ée]riau|feu|fire|unit[ée]|nom de fichier|"
    r"livrable|maquette num[ée]rique|proxy|g[ée]n[ée]rique)")


class ModelChecks(BaseModel):
    """Contrôles au niveau de la maquette qu'un CCBIM fixe souvent. Laisser vide ce qu'il ne demande pas."""

    model_config = ConfigDict(extra="forbid")
    ifc_schema: str | None = Field(None, description="Version IFC exigée, ex. 'IFC4' ou 'IFC2X3'.")
    file_name_pattern: str | None = Field(None, description="Regex du nom de fichier IFC (convention de nommage).")
    expected_buildings: int | None = Field(None, ge=1, description="Nombre de bâtiments attendu.")
    storey_names: list[str] | None = Field(None, description="Noms de niveaux attendus (liste du CCBIM).")
    storey_name_pattern: str | None = Field(None, description="Regex du nom de niveau, ex. '^(SS\\d|RDC|R\\+\\d+)$'.")
    classification_system: str | None = Field(None, description="Système de classification exigé (ex. Uniformat).")
    require_spaces: bool | None = Field(None, description="Locaux (IfcSpace) exigés.")
    max_proxy_rate: float | None = Field(None, ge=0, le=1, description="Part maximale d'objets génériques (0,05 = 5 %).")


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[Requirement] = Field(default_factory=list)
    model_checks: ModelChecks = Field(default_factory=ModelChecks)
    not_checkable: list[dict[str, str]] = Field(
        default_factory=list, description="[{exigence, source, raison}] : attendus non contrôlables sur la maquette.")
    sources: dict[str, str] = Field(default_factory=dict, description="Source de chaque contrôle de maquette.")


# ── Lecture du document ─────────────────────────────────────────────────


def read_pages(path: Path) -> list[str]:
    """Texte du document, une entrée par page (PDF) ou par bloc d'environ 3 000 caractères."""
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ValueError(f"Format non pris en charge ({ext}) : PDF, Word (.docx), Excel, texte.")
    if ext == ".pdf":
        from pypdf import PdfReader

        pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        if not any(p.strip() for p in pages):
            raise ValueError("PDF sans texte lisible (document scanné ?) : fournir une version texte ou Word.")
        return pages
    if ext == ".docx":
        from docx import Document

        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return _chunks("\n".join(parts))
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            rows = [" | ".join("" if v is None else str(v) for v in r) for r in ws.iter_rows(values_only=True)]
            out += _chunks(f"[Feuille {ws.title}]\n" + "\n".join(r for r in rows if r.strip(" |")))
        return out
    return _chunks(path.read_text(errors="replace"))


def _chunks(text: str, size: int = 3000) -> list[str]:
    lines, out, cur = text.splitlines(), [], ""
    for ln in lines:
        if len(cur) + len(ln) > size and cur:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    return out + ([cur] if cur.strip() else [])


def key_passages(pages: list[str], limit: int = 60) -> list[dict]:
    out = []
    for n, p in enumerate(pages, start=1):
        for para in re.split(r"\n\s*\n|(?<=[.;:])\n", p):
            para = " ".join(para.split())
            if 25 <= len(para) <= 700 and KEYWORDS.search(para):
                out.append({"page": n, "texte": para})
    return out[:limit]


def page_slice(pages: list[str], start: int) -> tuple[list[dict], int | None]:
    out, size, k = [], 0, start
    while k < len(pages) and (size + len(pages[k]) <= MAX_CHARS_PER_CALL or not out):
        out.append({"page": k + 1, "texte": pages[k]})
        size += len(pages[k])
        k += 1
    return out, (k if k < len(pages) else None)


# ── Contrôle ────────────────────────────────────────────────────────────


def all_requirements(exp: Expectations) -> list[Requirement]:
    reqs = []
    for i, r in enumerate(exp.requirements, start=1):
        reqs.append(r if r.id else r.model_copy(update={"id": f"CC{i:02d}"}))
    sysname = exp.model_checks.classification_system
    if sysname:
        reqs.append(Requirement(
            id="CC-CL", label=f"Classification {sysname} renseignée", field="Classification",
            pattern=rf"(?is).*{re.escape(sysname)}.*", category="complétude", severity="majeur",
            source=exp.sources.get("classification_system")))
    if exp.model_checks.storey_name_pattern:
        reqs.append(Requirement(
            id="CC-NV", label="Nom de niveau conforme au CCBIM", field="Name", ifc_classes=["IfcBuildingStorey"],
            pattern=exp.model_checks.storey_name_pattern, category="nommage", severity="majeur",
            source=exp.sources.get("storey_name_pattern")))
    return reqs


def _norm(s: str) -> str:
    return re.sub(r"[\s_.-]", "", str(s or "")).upper()


def model_level(exp: Expectations, recs, snap) -> list[tuple[str, str, str, bool, str, str, str]]:
    """Contrôles de maquette : (id, libellé, sévérité, conforme, constaté, attendu, source)."""
    mc, src = exp.model_checks, exp.sources
    out = []
    model = snap.model or {}
    if mc.ifc_schema:
        got = str(model.get("version") or "")
        out.append(("CC-M1", "Version IFC exigée", "majeur", _norm(got).startswith(_norm(mc.ifc_schema)), got or "—",
                    mc.ifc_schema, src.get("ifc_schema", "")))
    if mc.file_name_pattern:
        name = str((model.get("document") or {}).get("file_name") or model.get("name") or "")
        ok = bool(re.fullmatch(mc.file_name_pattern, Path(name).stem) or re.fullmatch(mc.file_name_pattern, name))
        out.append(("CC-M2", "Nom du fichier IFC conforme à la convention", "mineur", ok, name or "—",
                    mc.file_name_pattern, src.get("file_name_pattern", "")))
    if mc.expected_buildings:
        n = sum(1 for r in recs if r.ifc_class == "IfcBuilding")
        out.append(("CC-M3", "Nombre de bâtiments", "majeur", n == mc.expected_buildings, str(n),
                    str(mc.expected_buildings), src.get("expected_buildings", "")))
    if mc.storey_names:
        have = {_norm(s.get("name")) for s in snap.storeys}
        missing = [s for s in mc.storey_names if _norm(s) not in have]
        out.append(("CC-M4", "Niveaux attendus présents", "majeur", not missing,
                    "manquants : " + ", ".join(missing) if missing else "tous présents",
                    ", ".join(mc.storey_names), src.get("storey_names", "")))
    if mc.require_spaces:
        n = sum(1 for r in recs if r.ifc_class == "IfcSpace")
        out.append(("CC-M5", "Locaux (IfcSpace) modélisés", "majeur", n > 0, str(n), "au moins un local",
                    src.get("require_spaces", "")))
    if mc.max_proxy_rate is not None:
        phys = [r for r in recs if r.is_physical]
        rate = sum(1 for r in phys if r.ifc_class == "IfcBuildingElementProxy") / (len(phys) or 1)
        out.append(("CC-M6", "Part d'objets génériques (IfcBuildingElementProxy)", "mineur",
                    rate <= mc.max_proxy_rate, f"{100 * rate:.1f} %".replace(".", ","),
                    f"≤ {100 * mc.max_proxy_rate:g} %".replace(".", ","), src.get("max_proxy_rate", "")))
    return out


def run(exp: Expectations, recs, snap) -> CheckResult:
    res = run_check(recs, all_requirements(exp))
    model_name = str((snap.model or {}).get("name") or "maquette")
    for k, (rid, label, sev, ok, got, want, source) in enumerate(model_level(exp, recs, snap), start=1):
        res.per_requirement.append({
            "req_id": rid, "label": label, "field": "(maquette)", "ifc_classes": "(maquette)", "severity": sev,
            "category": "conformité", "applicable": 1, "conformes": int(ok), "non_conformes": int(not ok),
            "taux": float(ok), "optionnels_absents": 0})
        res.n_requirements += 1
        res.n_checks += 1
        if not ok:
            res.issues.append(Issue(
                issue_id=f"M{k:03d}", req_id=rid, req_label=label, category="conformité", severity=sev, uuid="",
                ifc_class="IfcProject", name=model_name, storey="", field="(maquette)", location="", status="non_conforme",
                actual=got, expected=want, message=f"{label} : constaté {got} ; attendu {want}"
                + (f" (CCBIM {source})" if source else ""), writable=False))
    return res


def hypotheses(exp: Expectations) -> list[dict]:
    """Attendus présentés comme hypothèses du rapport (chapitre 3) et à la validation."""
    rows = []
    for r in all_requirements(exp):
        rule = r.pattern or (" / ".join(r.allowed_values) if r.allowed_values else
                             ("renseigné" if r.required else "facultatif"))
        rows.append({"cle": r.id, "parametre": f"{r.id} — {r.label or r.field}",
                     "valeur": f"{r.field} : {rule}"[:120], "defaut": None, "unite": "",
                     "plage": ", ".join(r.ifc_classes) or "tous éléments", "source": r.source or "CCBIM",
                     "modifie": False, "a_fournir": False})
    for f, v in exp.model_checks.model_dump(exclude_none=True).items():
        label = ModelChecks.model_fields[f].description or f
        rows.append({"cle": f, "parametre": label.split(",")[0].split(" ex.")[0], "valeur": v if not isinstance(v, list)
                     else ", ".join(v), "defaut": None, "unite": "", "plage": "maquette",
                     "source": exp.sources.get(f) or "CCBIM", "modifie": False, "a_fournir": False})
    return rows


def summary(exp: Expectations) -> dict[str, Any]:
    return {"exigences_donnees": len(exp.requirements),
            "controles_maquette": list(exp.model_checks.model_dump(exclude_none=True)),
            "non_controlables": len(exp.not_checkable),
            "par_classe": dict(Counter(c for r in exp.requirements for c in r.ifc_classes).most_common(15))}
