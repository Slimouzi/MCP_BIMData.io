"""Sélection d'éléments : critères déclaratifs évalués sur la vue ``Record``.

Les critères usuels reprennent ceux de ``bim_core.filters.ObjectFilter``
(classes IFC, rattachement spatial, nom, classification, matériau, calque,
présence/absence de propriété), étendus de **conditions** sur n'importe quel
champ (``Pset.Prop``, attribut, quantité…) et de motifs ``*`` sur les noms
d'étage / pièce / zone.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_view import Record, normalize_text, resolve_field

Op = Literal[
    "exists", "missing", "eq", "ne", "contains", "not_contains", "startswith", "endswith",
    "regex", "not_regex", "in", "not_in", "gt", "gte", "lt", "lte", "between",
]


class Condition(BaseModel):
    """Condition sur un champ (syntaxe de champ : cf. ``bimdata_field_syntax``)."""

    model_config = ConfigDict(extra="forbid")
    field: str = Field(..., description="Ex. 'Pset_WallCommon.FireRating', 'Name', 'Storey', '*.Reference'.")
    op: Op = "eq"
    value: Any = Field(None, description="Valeur de comparaison (liste pour in/not_in/between).")

    @model_validator(mode="after")
    def _check(self) -> Condition:
        if self.op in ("regex", "not_regex"):
            re.compile(str(self.value or ""))
        if self.op in ("in", "not_in", "between") and not isinstance(self.value, (list, tuple)):
            raise ValueError(f"op={self.op} attend une liste en value.")
        return self


class SelectionSpec(BaseModel):
    """Critères de sélection (ET entre critères, OU entre valeurs d'une liste)."""

    model_config = ConfigDict(extra="forbid")
    ifc_classes: list[str] | None = Field(None, description="Ex. ['IfcWall','IfcDoor']. Sous-types inclus (IfcWall ⇒ IfcWallStandardCase).")
    exclude_ifc_classes: list[str] | None = None
    include_subtypes: bool = True
    include_spatial: bool = Field(False, description="Inclure Site/Bâtiment/Étage/Espace/Zone même sans les nommer dans ifc_classes.")
    include_non_physical: bool = Field(False, description="Inclure ouvertures, grilles, groupes…")
    buildings: list[str] | None = Field(None, description="Noms de bâtiment (motifs * acceptés).")
    storeys: list[str] | None = Field(None, description="Noms d'étage (motifs * acceptés, ex. 'R+*').")
    spaces: list[str] | None = None
    zones: list[str] | None = None
    name_contains: str | None = Field(None, description="Sous-chaîne dans Name ou LongName (sans casse ni accents).")
    name_regex: str | None = Field(None, description="Regex (search) sur Name ou LongName.")
    uuids: list[str] | None = None
    classification_contains: list[str] | None = Field(None, description="Ex. ['B2010', 'Uniformat:'].")
    has_classification: bool | None = None
    material_contains: str | None = None
    layer_contains: str | None = None
    has_property: list[str] | None = Field(None, description="Champs qui doivent être renseignés.")
    missing_property: list[str] | None = Field(None, description="Champs absents ou vides.")
    conditions: list[Condition] | None = None
    conditions_match: Literal["all", "any"] = "all"

    @model_validator(mode="after")
    def _check(self) -> SelectionSpec:
        if self.name_regex:
            re.compile(self.name_regex)
        return self

    def is_empty(self) -> bool:
        return not any(v not in (None, [], False, "all", True) for k, v in self.model_dump().items()
                       if k not in ("include_subtypes", "conditions_match"))


_SUBTYPE_SUFFIXES = ("standardcase", "elementedcase")

#: Classes abstraites usuelles → sous-classes concrètes (IFC2x3 + IFC4).
_ABSTRACT = {
    "ifcbuildingelement": {
        "ifcwall", "ifcslab", "ifcroof", "ifcdoor", "ifcwindow", "ifccolumn", "ifcbeam", "ifcstair",
        "ifcstairflight", "ifcramp", "ifcrampflight", "ifcrailing", "ifccovering", "ifccurtainwall",
        "ifcplate", "ifcmember", "ifcfooting", "ifcpile", "ifcbuildingelementproxy", "ifcchimney",
        "ifcshadingdevice",
    },
    "ifcfurnishingelement": {"ifcfurnishingelement", "ifcfurniture", "ifcsystemfurnitureelement"},
    "ifcopeningelement": {"ifcopeningelement", "ifcopeningstandardcase"},
}
_DISTRIBUTION_PREFIXES = ("ifcflow", "ifcenergyconversiondevice", "ifcdistribution", "ifcairterminal",
                          "ifcduct", "ifcpipe", "ifccable", "ifclightfixture", "ifcsanitaryterminal",
                          "ifcvalve", "ifcpump", "ifcfan", "ifcboiler", "ifcchiller", "ifcsensor",
                          "ifcalarm", "ifcoutlet", "ifcswitchingdevice", "ifcfiresuppressionterminal")


def _norm_class(name: str) -> str:
    w = name.strip().lower()
    return w if w.startswith("ifc") else "ifc" + w


def _class_match(cls: str, wanted: list[str], subtypes: bool) -> bool:
    c = cls.lower()
    for w in wanted:
        wl = _norm_class(w)
        if c == wl:
            return True
        if not subtypes:
            continue
        if any(c == wl + suf for suf in _SUBTYPE_SUFFIXES):
            return True
        if wl in _ABSTRACT:
            base = c
            for suf in _SUBTYPE_SUFFIXES:
                if base.endswith(suf):
                    base = base[: -len(suf)]
            if base in _ABSTRACT[wl]:
                return True
        if wl in ("ifcdistributionelement", "ifcdistributionflowelement") and c.startswith(_DISTRIBUTION_PREFIXES):
            return True
        if wl == "ifcelement" and c not in ("ifcproject", "ifcsite", "ifcbuilding", "ifcbuildingstorey",
                                             "ifcspace", "ifczone"):
            return True
    return False


def _match_names(value: str, patterns: list[str]) -> bool:
    v = normalize_text(value)
    for p in patterns:
        pn = normalize_text(p)
        if ("*" in pn or "?" in pn) and fnmatch.fnmatchcase(v, pn):
            return True
        if v == pn:
            return True
    return False


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


_TRUE = {"true", "1", "yes", "oui", "vrai", "x"}
_FALSE = {"false", "0", "no", "non", "faux"}


def as_bool(x: Any) -> bool | None:
    if isinstance(x, bool):
        return x
    t = normalize_text(x)
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    return None


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        ba, bb = as_bool(a), as_bool(b)
        if ba is not None and bb is not None:
            return ba == bb
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return normalize_text(a) == normalize_text(b)


def eval_condition(rec: Record, cond: Condition) -> bool:
    fv = resolve_field(rec, cond.field)
    v, op = fv.value, cond.op
    if op == "exists":
        return fv.found
    if op == "missing":
        return not fv.found
    if not fv.found and op not in ("ne", "not_contains", "not_regex", "not_in"):
        return False
    if op == "eq":
        return _eq(v, cond.value)
    if op == "ne":
        return not _eq(v, cond.value)
    if op in ("contains", "not_contains"):
        hit = normalize_text(cond.value) in normalize_text(v)
        return hit if op == "contains" else not hit
    if op == "startswith":
        return normalize_text(v).startswith(normalize_text(cond.value))
    if op == "endswith":
        return normalize_text(v).endswith(normalize_text(cond.value))
    if op in ("regex", "not_regex"):
        hit = re.search(str(cond.value), str(v if v is not None else ""), re.IGNORECASE) is not None
        return hit if op == "regex" else not hit
    if op in ("in", "not_in"):
        hit = any(_eq(v, x) for x in cond.value)
        return hit if op == "in" else not hit
    n = _num(v)
    if n is None:
        return False
    if op == "between":
        lo, hi = (_num(cond.value[0]), _num(cond.value[1]))
        return lo is not None and hi is not None and lo <= n <= hi
    ref = _num(cond.value)
    if ref is None:
        return False
    return {"gt": n > ref, "gte": n >= ref, "lt": n < ref, "lte": n <= ref}[op]


def record_matches(rec: Record, spec: SelectionSpec) -> bool:
    explicit_classes = spec.ifc_classes or []
    if explicit_classes:
        if not _class_match(rec.ifc_class, explicit_classes, spec.include_subtypes):
            return False
    else:
        if rec.is_spatial and not spec.include_spatial:
            return False
        if not rec.is_spatial and not rec.is_physical and not spec.include_non_physical:
            return False
    if spec.exclude_ifc_classes and _class_match(rec.ifc_class, spec.exclude_ifc_classes, spec.include_subtypes):
        return False
    if spec.uuids and rec.uuid not in set(spec.uuids):
        return False
    if spec.buildings and not _match_names(rec.building, spec.buildings):
        return False
    if spec.storeys and not _match_names(rec.storey or (rec.name if rec.ifc_class == "IfcBuildingStorey" else ""), spec.storeys):
        return False
    if spec.spaces and not _match_names(rec.space or (rec.name if rec.ifc_class == "IfcSpace" else ""), spec.spaces):
        return False
    if spec.zones and not any(_match_names(z, spec.zones) for z in (rec.zone.split(" | ") if rec.zone else [""])):
        return False
    names = [str(rec.attrs.get("Name") or ""), str(rec.attrs.get("LongName") or "")]
    if spec.name_contains and not any(normalize_text(spec.name_contains) in normalize_text(n) for n in names):
        return False
    if spec.name_regex and not any(re.search(spec.name_regex, n, re.IGNORECASE) for n in names):
        return False
    if spec.has_classification is not None and bool(rec.classifications) != spec.has_classification:
        return False
    if spec.classification_contains:
        joined = normalize_text(" | ".join(rec.classifications))
        if not any(normalize_text(c) in joined for c in spec.classification_contains):
            return False
    if spec.material_contains and normalize_text(spec.material_contains) not in normalize_text(" | ".join(rec.materials)):
        return False
    if spec.layer_contains and normalize_text(spec.layer_contains) not in normalize_text(" | ".join(rec.layers)):
        return False
    for f in spec.has_property or []:
        if not resolve_field(rec, f).found:
            return False
    for f in spec.missing_property or []:
        if resolve_field(rec, f).found:
            return False
    if spec.conditions:
        results = (eval_condition(rec, c) for c in spec.conditions)
        if spec.conditions_match == "all" and not all(results):
            return False
        if spec.conditions_match == "any" and not any(results):
            return False
    return True


def select(records: list[Record], spec: SelectionSpec) -> list[Record]:
    return [r for r in records if record_matches(r, spec)]


def summarize(records: list[Record]) -> dict:
    return {
        "count": len(records),
        "by_ifc_class": dict(Counter(r.ifc_class for r in records).most_common(25)),
        "by_storey": dict(Counter(r.storey or "(sans étage)" for r in records).most_common(25)),
    }
