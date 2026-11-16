"""Vue « enregistrements » d'un ``ModelSnapshot`` : un dict plat par élément.

Toutes les fonctionnalités (sélection, export, contrôle, corrections) lisent la
maquette à travers cette vue, avec **une seule** syntaxe de champ :

=====================  ===================================================
Chemin                 Valeur
=====================  ===================================================
``GlobalId``           GUID IFC (``uuid`` BIMData)
``IfcClass``           classe IFC (``IfcWallStandardCase``…)
``Name`` ``LongName``  attributs IFC (idem ``Description``, ``ObjectType``,
``Tag``…              ``Tag``, ``PredefinedType`` et tout attribut présent)
``Building``           bâtiment de rattachement (idem ``Storey``, ``Space``,
                       ``Zone``)
``Classification``     classifications (``système:code``), séparées par « | »
``Material``           matériaux, séparés par « | » (idem ``Layer``)
``Pset.Prop``          propriété d'un Pset (``Pset_WallCommon.FireRating``) ou
                       quantité (``Qto_WallBaseQuantities.NetSideArea``)
``*.Prop`` / ``Prop``  propriété cherchée dans **tous** les Psets
=====================  ===================================================

L'index spatial est construit ici depuis ``structure_tree`` : l'arbre BIMData
type ses nœuds en *snake_case* (``storey``, ``space``, ``building``), forme que
``bim_query.views`` (qui attend ``IfcBuildingStorey``) ne reconnaît pas.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from bim_core.model_snapshot import ModelSnapshot

__all__ = [
    "Record",
    "FieldValue",
    "build_records",
    "resolve_field",
    "ATTRIBUTE_FIELDS",
    "SPECIAL_FIELDS",
    "SPATIAL_CLASSES",
    "normalize_text",
]

SPATIAL_CLASSES = frozenset({"IfcProject", "IfcSite", "IfcBuilding", "IfcBuildingStorey", "IfcSpace", "IfcZone"})
NON_PHYSICAL_CLASSES = frozenset(
    {"IfcOpeningElement", "IfcVirtualElement", "IfcGrid", "IfcAnnotation", "IfcGroup", "IfcSystem",
     "IfcDistributionPort", "IfcRelSpaceBoundary"}
)
ATTRIBUTE_FIELDS = ("Name", "LongName", "Description", "ObjectType", "Tag", "PredefinedType")
SPECIAL_FIELDS = ("GlobalId", "IfcClass", "Building", "Storey", "Space", "Zone", "Classification", "Material", "Layer", "TypeName")

_TREE_KIND = {
    "building": "building", "ifcbuilding": "building",
    "storey": "storey", "ifcbuildingstorey": "storey", "building_storey": "storey",
    "space": "space", "ifcspace": "space",
    "site": "site", "ifcsite": "site", "project": "project", "ifcproject": "project",
}


def normalize_text(value: Any) -> str:
    s = unicodedata.normalize("NFKD", str(value or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


@dataclass
class Record:
    uuid: str
    ifc_class: str
    attrs: dict[str, Any] = field(default_factory=dict)  # attributs IFC
    props: dict[str, Any] = field(default_factory=dict)  # "Pset.Prop" -> valeur
    prop_types: dict[str, str] = field(default_factory=dict)  # "Pset.Prop" -> value_type
    building: str = ""
    storey: str = ""
    space: str = ""
    zone: str = ""
    classifications: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    _by_prop: dict[str, list[str]] = field(default_factory=dict)  # prop normalisée -> clés

    @property
    def name(self) -> str:
        return str(self.attrs.get("Name") or "")

    @property
    def is_spatial(self) -> bool:
        return self.ifc_class in SPATIAL_CLASSES

    @property
    def is_physical(self) -> bool:
        return not self.is_spatial and self.ifc_class not in NON_PHYSICAL_CLASSES

    def psets(self) -> list[str]:
        return sorted({k.split(".", 1)[0] for k in self.props})


@dataclass(frozen=True)
class FieldValue:
    value: Any
    found: bool
    location: str  # "attr:Name" | "pset:Pset_X.Y" | "special:Storey" | ""
    other_locations: tuple[str, ...] = ()


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _spatial_index(snapshot: ModelSnapshot) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    def visit(node: dict, ctx: dict[str, str]) -> None:
        kind = _TREE_KIND.get(str(node.get("type") or "").lower())
        name = node.get("name") or node.get("longname") or ""
        if kind in ("building", "storey", "space"):
            ctx = {**ctx, kind: name}
            if kind == "building":
                ctx.pop("storey", None)
                ctx.pop("space", None)
        uuid = node.get("uuid")
        if uuid:
            out[uuid] = ctx
        for child in node.get("children") or []:
            visit(child, ctx)

    for root in snapshot.structure_tree or []:
        visit(root, {})
    return out


def _zone_index(snapshot: ModelSnapshot) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def visit(zone: dict) -> None:
        zname = zone.get("name") or zone.get("longname") or ""
        for sp in zone.get("spaces") or []:
            if sp.get("uuid"):
                out.setdefault(sp["uuid"], [])
                if zname and zname not in out[sp["uuid"]]:
                    out[sp["uuid"]].append(zname)
        for sub in zone.get("zones") or []:
            visit(sub)

    for z in snapshot.zones or []:
        visit(z)
    return out


def _unique(items) -> list[str]:
    seen: list[str] = []
    for it in items:
        s = str(it).strip() if it is not None else ""
        if s and s not in seen:
            seen.append(s)
    return seen


def _record_from_element(el: dict, spatial: dict, zones: dict, space_names: dict) -> Record:
    uuid = el.get("uuid") or ""
    rec = Record(uuid=uuid, ifc_class=el.get("type") or "")
    attrs: dict[str, Any] = {}
    for prop in (el.get("attributes") or {}).get("properties") or []:
        nm = (prop.get("definition") or {}).get("name")
        if nm:
            attrs[nm] = prop.get("value")
    for key, src in (("Name", "name"), ("Description", "description"), ("LongName", "longname"),
                     ("ObjectType", "object_type")):
        if _blank(attrs.get(key)) and not _blank(el.get(src)):
            attrs[key] = el.get(src)
    if _blank(attrs.get("LongName")) and not _blank(el.get("long_name")):
        attrs["LongName"] = el.get("long_name")
    rec.attrs = attrs
    for pset in el.get("property_sets") or []:
        pname = pset.get("name") or ""
        if not pname:
            continue
        for prop in pset.get("properties") or []:
            d = prop.get("definition") or {}
            pn = d.get("name")
            if not pn:
                continue
            key = f"{pname}.{pn}"
            val = prop.get("value")
            # Pset dupliqué (fréquent sur exports Revit) : on garde la 1re valeur non vide.
            if key in rec.props and not _blank(rec.props[key]):
                continue
            rec.props[key] = val
            if d.get("value_type"):
                rec.prop_types[key] = d["value_type"]
            rec._by_prop.setdefault(pn.lower(), [])
            if key not in rec._by_prop[pn.lower()]:
                rec._by_prop[pn.lower()].append(key)
    ctx = spatial.get(uuid, {})
    rec.building = ctx.get("building", "")
    rec.storey = ctx.get("storey", "")
    space = ctx.get("space", "")
    if rec.ifc_class == "IfcSpace":
        space = ""
    rec.space = space
    zone_space_uuid = uuid if rec.ifc_class == "IfcSpace" else space_names.get(space)
    rec.zone = " | ".join(zones.get(zone_space_uuid or "", []))
    cls = []
    for c in el.get("classifications") or []:
        if not isinstance(c, dict):
            continue
        code = c.get("notation") or c.get("identifier") or c.get("code")
        if not code:
            continue
        system = c.get("name") or c.get("source") or c.get("system") or ""
        cls.append(f"{system}:{code}" if system else str(code))
    rec.classifications = _unique(cls)
    mats = [((m or {}).get("material") or {}).get("name") for m in el.get("material_list") or []]
    mats += [m.get("name") if isinstance(m, dict) else m for m in el.get("materials") or []]
    rec.materials = _unique(mats)
    rec.layers = _unique((lay or {}).get("name") for lay in el.get("layers") or [] if isinstance(lay, dict))
    return rec


_RECORDS_ATTR = "_bimdata_mcp_records"


def build_records(snapshot: ModelSnapshot) -> list[Record]:
    """Construit (et mémorise sur le snapshot) la liste des enregistrements."""
    cached = getattr(snapshot, _RECORDS_ATTR, None)
    if cached is not None:
        return cached
    spatial = _spatial_index(snapshot)
    zones = _zone_index(snapshot)
    space_names: dict[str, str] = {}
    for sp in snapshot.spaces or []:
        if sp.get("name") and sp.get("uuid"):
            space_names.setdefault(sp["name"], sp["uuid"])
    records: list[Record] = []
    seen: set[str] = set()
    for el in snapshot.elements or []:
        if not el.get("uuid") or el["uuid"] in seen:
            continue
        seen.add(el["uuid"])
        records.append(_record_from_element(el, spatial, zones, space_names))
    # Conteneurs spatiaux absents de /element/raw (selon les modèles).
    for kind, items in (("IfcSite", snapshot.sites), ("IfcBuilding", snapshot.buildings),
                        ("IfcBuildingStorey", snapshot.storeys), ("IfcSpace", snapshot.spaces),
                        ("IfcZone", snapshot.zones)):
        for it in items or []:
            if it.get("uuid") and it["uuid"] not in seen:
                seen.add(it["uuid"])
                records.append(_record_from_element({**it, "type": kind}, spatial, zones, space_names))
    try:
        setattr(snapshot, _RECORDS_ATTR, records)
    except (AttributeError, TypeError):
        pass
    return records


def records_by_uuid(snapshot: ModelSnapshot) -> dict[str, Record]:
    attr = "_bimdata_mcp_records_by_uuid"
    cached = getattr(snapshot, attr, None)
    if cached is None:
        cached = {r.uuid: r for r in build_records(snapshot)}
        setattr(snapshot, attr, cached)
    return cached


def resolve_field(rec: Record, path: str) -> FieldValue:
    """Résout un chemin de champ (cf. docstring du module)."""
    p = (path or "").strip()
    low = p.lower()
    special = {
        "globalid": rec.uuid, "uuid": rec.uuid, "guid": rec.uuid,
        "ifcclass": rec.ifc_class, "classe ifc": rec.ifc_class, "type ifc": rec.ifc_class,
        "building": rec.building, "bâtiment": rec.building, "batiment": rec.building,
        "storey": rec.storey, "étage": rec.storey, "etage": rec.storey, "niveau": rec.storey,
        "space": rec.space, "espace": rec.space, "pièce": rec.space, "piece": rec.space,
        "zone": rec.zone,
        "classification": " | ".join(rec.classifications),
        "material": " | ".join(rec.materials), "matériau": " | ".join(rec.materials),
        "materiau": " | ".join(rec.materials),
        "layer": " | ".join(rec.layers), "calque": " | ".join(rec.layers),
    }
    if low in special:
        v = special[low]
        return FieldValue(v, not _blank(v), f"special:{p}")
    if low.startswith("attr:") or low.startswith("ifc:"):
        p = p.split(":", 1)[1]
        low = p.lower()
    # Attribut IFC (insensible à la casse).
    if "." not in p:
        for k, v in rec.attrs.items():
            if k.lower() == low:
                return FieldValue(v, not _blank(v), f"attr:{k}")
        if low == "typename":
            v = rec.attrs.get("ObjectType")
            return FieldValue(v, not _blank(v), "attr:ObjectType")
    # Pset.Prop exact (insensible à la casse).
    if "." in p and not p.startswith("*."):
        pset, prop = p.split(".", 1)
        for k, v in rec.props.items():
            kp, kn = k.split(".", 1)
            if kp.lower() == pset.lower() and kn.lower() == prop.lower():
                return FieldValue(v, not _blank(v), f"pset:{k}")
        others = tuple(f"pset:{k}" for k in rec._by_prop.get(prop.lower(), []))
        return FieldValue(None, False, "", others)
    prop = p[2:] if p.startswith("*.") else p
    keys = rec._by_prop.get(prop.lower(), [])
    for k in keys:
        if not _blank(rec.props[k]):
            return FieldValue(rec.props[k], True, f"pset:{k}", tuple(f"pset:{x}" for x in keys if x != k))
    if keys:
        return FieldValue(rec.props[keys[0]], False, f"pset:{keys[0]}")
    return FieldValue(None, False, "")


def property_catalog(records: list[Record], *, ifc_classes: set[str] | None = None) -> list[dict]:
    """Catalogue des champs disponibles avec taux de remplissage."""
    stats: dict[str, list[int]] = {}
    n = 0
    for r in records:
        if ifc_classes and r.ifc_class not in ifc_classes:
            continue
        n += 1
        for k, v in r.attrs.items():
            s = stats.setdefault(k, [0, 0])
            s[0] += 1
            s[1] += 0 if _blank(v) else 1
        for k, v in r.props.items():
            s = stats.setdefault(k, [0, 0])
            s[0] += 1
            s[1] += 0 if _blank(v) else 1
    out = [
        {"field": k, "present_on": c, "filled_on": f, "fill_rate": round(f / n, 3) if n else 0.0}
        for k, (c, f) in stats.items()
    ]
    out.sort(key=lambda d: (-d["present_on"], d["field"]))
    return out
