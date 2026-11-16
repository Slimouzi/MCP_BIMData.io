# Repris de MCP_COBIe (mcp_cobie.cobie.bimdata, v0.3.0) sans modification fonctionnelle.
# Vendorisé car mcp-cobie épingle bim-core<0.4, incompatible avec bimdata-read 0.1.10.
"""Construction des 8 feuilles COBie depuis l'**API BIMData**.

C'est le flux principal du serveur : une maquette est désignée par son URL
viewer ou ses identifiants, les données sont lues par API, et le classeur est
produit. La génération depuis un IFC local existe encore (module ``ifc``) mais
elle est secondaire — elle suppose qu'on dispose déjà du fichier.

Le mapping vient du plugin de référence ``Plugin-CoBie``, porté ici sans
changer les colonnes : un classeur COBie est relu par des outils tiers, et
renommer un en-tête casse un usage aval sans rien faire échouer chez nous.

Toutes les lectures passent par ``bimdata-read``. Aucune requête HTTP n'est
écrite ici : le point d'accès BIMData de l'écosystème est ce paquet, et le
doubler recréerait la divergence qu'il existe pour éviter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

__all__ = ["build_sheets_from_bimdata", "SHEET_BUILD_ORDER"]

SHEET_BUILD_ORDER = (
    "Facility",
    "Floor",
    "Space",
    "Zone",
    "Type",
    "Component",
    "System",
    "Attribute",
)

EXT_SYSTEM = "BIMData"

#: Conteneurs spatiaux et objets non physiques : jamais des composants COBie.
_EXCLUDED_COMPONENT_TYPES = frozenset(
    {
        "ifcbuilding",
        "ifcbuildingstorey",
        "ifcspace",
        "ifcsite",
        "ifcproject",
        "ifcannotation",
        "ifcgrid",
        "ifcopeningelement",
        "ifcvirtualelement",
        "ifczone",
        "ifcsystem",
        "ifcgroup",
        "ifcdistributionport",
    }
)

_QTO_SPACE_PSETS = ("Qto_SpaceBaseQuantities", "BaseQuantities")
_PSET_MFR = "Pset_ManufacturerTypeInformation"
_PSET_WARRANTY = "Pset_Warranty"

#: Psets déjà restitués par une colonne dédiée : les répéter dans Attribute
#: doublerait l'information sans l'enrichir.
_ATTRIBUTE_SKIP_PSETS = frozenset(
    {
        _PSET_MFR,
        _PSET_WARRANTY,
        "Pset_ServiceLife",
        "Qto_SpaceBaseQuantities",
        "Pset_SpaceCommon",
        "Pset_BuildingStoreyCommon",
    }
)

_SPATIAL_TYPES = frozenset({"project", "site", "building", "storey", "space"})


# ── Helpers de lecture ────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()


def _pset_value(element: Mapping[str, Any], pset_names: str | Iterable[str], prop: str) -> Any:
    names = {pset_names} if isinstance(pset_names, str) else set(pset_names)
    for pset in element.get("property_sets") or []:
        if pset.get("name") not in names:
            continue
        for candidate in pset.get("properties") or []:
            if (candidate.get("definition") or {}).get("name") == prop:
                return candidate.get("value")
    return ""


def _attr_value(element: Mapping[str, Any], name: str) -> Any:
    for prop in (element.get("attributes") or {}).get("properties") or []:
        if (prop.get("definition") or {}).get("name") == name:
            return prop.get("value")
    return None


def _format_classifications(element: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for classification in element.get("classifications") or []:
        name = classification.get("name") or ""
        notation = classification.get("notation") or ""
        title = classification.get("title") or ""
        chunk = f"{name}: {notation}" if name else notation
        if title:
            chunk = f"{chunk} ({title})" if chunk else title
        if chunk:
            parts.append(chunk)
    return "; ".join(parts)


def _format_layers(element: Mapping[str, Any]) -> str:
    return ", ".join(
        layer.get("name") for layer in element.get("layers") or [] if layer.get("name")
    )


def _format_materials(element: Mapping[str, Any]) -> str:
    names = []
    for entry in element.get("material_list") or []:
        material = entry.get("material") or {}
        if material.get("name"):
            names.append(material["name"])
    return ", ".join(names)


def _format_base_quantities(element: Mapping[str, Any]) -> str:
    """Agrège les quantités de tous les ``IfcElementQuantity`` en ``Nom=valeur``.

    Les flottants sont arrondis à 1e-4 : une surface écrite avec quinze
    décimales n'est pas plus juste, et rend le classeur illisible.
    """
    items: list[str] = []
    for pset in element.get("property_sets") or []:
        name = pset.get("name") or ""
        is_qto = (
            pset.get("type") == "IfcElementQuantity"
            or name == "BaseQuantities"
            or name.startswith("Qto_")
        )
        if not is_qto:
            continue
        for prop in pset.get("properties") or []:
            prop_name = (prop.get("definition") or {}).get("name")
            value = prop.get("value")
            if not prop_name or value in (None, ""):
                continue
            if isinstance(value, float):
                value = round(value, 4)
            items.append(f"{prop_name}={value}")
    return "; ".join(items)


def _is_component(element: Mapping[str, Any]) -> bool:
    kind = (element.get("type") or "").lower()
    return bool(kind) and kind not in _EXCLUDED_COMPONENT_TYPES


def _first(value: Any, *fallbacks: Any) -> Any:
    for candidate in (value, *fallbacks):
        if candidate not in (None, ""):
            return candidate
    return ""


# ── Index spatial ─────────────────────────────────────────────────────


class SpatialIndex:
    """Rattachement élément → espace → étage, reconstruit depuis l'arbre.

    L'API ne dénormalise pas ces liens : ``/space`` ne porte pas son étage, et
    un élément ne porte pas l'espace qui le contient. Le seul endroit où
    l'information existe est ``structure_tree``, qu'on parcourt une fois.

    Un arbre absent n'est pas une panne : les colonnes de rattachement sont
    alors vides, ce qui est le contenu honnête. Refuser de produire le classeur
    priverait l'auditeur de tout le reste.
    """

    def __init__(self, structure_tree: Any, spaces: list[dict], elements: list[dict]) -> None:
        self.space_to_storey: dict[str, str] = {}
        self.element_to_storey: dict[str, str] = {}
        self.element_to_space: dict[str, str] = {}
        self.element_object_type: dict[str, str] = {}
        self._storeys_by_uuid: dict[str, dict] = {}
        self._spaces_by_uuid = {s["uuid"]: s for s in spaces if s.get("uuid")}
        self._walk(structure_tree, None, None)

    def _walk(self, node: Any, storey_uuid: str | None, space_uuid: str | None) -> None:
        if isinstance(node, list):
            for item in node:
                self._walk(item, storey_uuid, space_uuid)
            return
        if not isinstance(node, Mapping):
            return

        kind = (node.get("type") or "").lower()
        uuid = node.get("uuid")

        if kind == "storey":
            storey_uuid, space_uuid = uuid, None
        elif kind == "space":
            if storey_uuid and uuid:
                self.space_to_storey[uuid] = storey_uuid
            space_uuid = uuid
        elif kind and kind not in _SPATIAL_TYPES and uuid:
            if storey_uuid:
                self.element_to_storey[uuid] = storey_uuid
            if space_uuid:
                self.element_to_space[uuid] = space_uuid
            if node.get("object_type"):
                self.element_object_type[uuid] = node["object_type"]

        for child in node.get("children") or []:
            self._walk(child, storey_uuid, space_uuid)

    def bind_storeys(self, storeys: list[dict]) -> None:
        self._storeys_by_uuid = {s["uuid"]: s for s in storeys if s.get("uuid")}

    def storey_name_of_space(self, space_uuid: str | None) -> str:
        storey = self._storeys_by_uuid.get(self.space_to_storey.get(space_uuid or "", ""))
        return (storey or {}).get("name") or ""

    def storey_name_of_element(self, uuid: str | None) -> str:
        storey = self._storeys_by_uuid.get(self.element_to_storey.get(uuid or "", ""))
        return (storey or {}).get("name") or ""

    def space_name_of_element(self, uuid: str | None) -> str:
        space = self._spaces_by_uuid.get(self.element_to_space.get(uuid or "", ""))
        return (space or {}).get("name") or ""


# ── Builders, une fonction par feuille ────────────────────────────────


def _build_facility(buildings: list[dict], project: dict, model: dict) -> list[dict]:
    created_on = _now_iso()
    base = {
        "CreatedBy": "n/a",
        "CreatedOn": created_on,
        "Category": "Facility",
        "ProjectName": project.get("name") or "",
        "SiteName": "",
        "LinearUnits": "meters",
        "AreaUnits": "squaremeters",
        "VolumeUnits": "cubicmeters",
        "CurrencyUnit": "EUR",
        "AreaMeasurement": "GrossFloorArea",
        "ExtSystem": EXT_SYSTEM,
        "ProjectDescription": project.get("description") or "",
        "SiteDescription": "",
    }
    if not buildings:
        return [
            {
                **base,
                "Name": project.get("name") or "Facility",
                "ExtObject": "IfcProject",
                "ExtIdentifier": str(model.get("id") or ""),
                "Description": "",
                "Phase": "",
            }
        ]
    return [
        {
            **base,
            "Name": _first(building.get("name"), building.get("longname"), "Facility"),
            "ExtObject": "IfcBuilding",
            "ExtIdentifier": building.get("uuid") or "",
            "Description": building.get("description") or "",
            "Phase": building.get("phase") or "",
        }
        for building in buildings
    ]


def _build_floor(storeys: list[dict]) -> list[dict]:
    created_on = _now_iso()
    rows = []
    for storey in storeys:
        uuid = storey.get("uuid") or ""
        rows.append(
            {
                "Name": _first(storey.get("name"), storey.get("longname"), f"Floor_{uuid[:8]}"),
                "CreatedBy": "n/a",
                "CreatedOn": created_on,
                "Category": "Floor",
                "ExtSystem": EXT_SYSTEM,
                "ExtObject": "IfcBuildingStorey",
                "ExtIdentifier": uuid,
                "Description": _first(storey.get("description"), storey.get("longname")),
                "Elevation": _first(storey.get("elevation")),
                "Height": "",
            }
        )
    return rows


def _build_space(spaces: list[dict], index: SpatialIndex) -> list[dict]:
    created_on = _now_iso()
    rows = []
    for space in spaces:
        uuid = space.get("uuid") or ""
        rows.append(
            {
                "Name": _first(space.get("name"), space.get("longname"), f"Space_{uuid[:8]}"),
                "CreatedBy": "n/a",
                "CreatedOn": created_on,
                "Category": _first(_pset_value(space, "Pset_SpaceCommon", "Reference"), "Space"),
                "FloorName": index.storey_name_of_space(uuid),
                "Description": _first(space.get("description"), space.get("longname")),
                "ExtSystem": EXT_SYSTEM,
                "ExtObject": "IfcSpace",
                "ExtIdentifier": uuid,
                "RoomTag": _first(space.get("longname"), space.get("name")),
                "UsableHeight": _pset_value(space, "Pset_SpaceCommon", "UsableHeight"),
                "GrossArea": _pset_value(space, _QTO_SPACE_PSETS, "GrossFloorArea"),
                "NetArea": _pset_value(space, _QTO_SPACE_PSETS, "NetFloorArea"),
                "GrossFloorArea": _pset_value(space, _QTO_SPACE_PSETS, "GrossFloorArea"),
                "GrossPerimeter": _pset_value(space, _QTO_SPACE_PSETS, "GrossPerimeter"),
                "GrossVolume": _pset_value(space, _QTO_SPACE_PSETS, "GrossVolume"),
                "Height": _pset_value(space, _QTO_SPACE_PSETS, "Height"),
                "NetFloorArea": _pset_value(space, _QTO_SPACE_PSETS, "NetFloorArea"),
            }
        )
    return rows


def _named_list(items: Iterable[Any]) -> str:
    return ", ".join(
        str(item.get("name") or item.get("uuid") or "")
        for item in items
        if isinstance(item, Mapping) and (item.get("name") or item.get("uuid"))
    )


def _build_zone(zones: list[dict]) -> list[dict]:
    created_on = _now_iso()
    return [
        {
            "Name": _first(zone.get("name"), f"Zone_{(zone.get('uuid') or '')[:8]}"),
            "CreatedBy": "n/a",
            "CreatedOn": created_on,
            "Category": _first(zone.get("type"), "IfcZone"),
            "SpaceNames": _named_list(zone.get("spaces") or []),
            "ExtSystem": EXT_SYSTEM,
            "ExtObject": "IfcZone",
            "ExtIdentifier": zone.get("uuid") or "",
            "Description": _first(zone.get("description"), zone.get("longname")),
        }
        for zone in zones
    ]


def _build_system(systems: list[dict]) -> list[dict]:
    created_on = _now_iso()
    return [
        {
            "Name": _first(system.get("name"), f"System_{(system.get('uuid') or '')[:8]}"),
            "CreatedBy": "n/a",
            "CreatedOn": created_on,
            "Category": _first(system.get("type"), "IfcSystem"),
            "ComponentNames": _named_list(system.get("elements") or system.get("components") or []),
            "ExtSystem": EXT_SYSTEM,
            "ExtObject": _first(system.get("type"), "IfcSystem"),
            "ExtIdentifier": system.get("uuid") or "",
            "Description": _first(system.get("description"), system.get("longname")),
        }
        for system in systems
    ]


def _type_row(source: Mapping[str, Any], *, name: str, ext_identifier: str) -> dict:
    return {
        "Name": name,
        "CreatedBy": "n/a",
        "CreatedOn": _now_iso(),
        "Category": _first(source.get("object_type"), source.get("type")),
        "Description": _first(source.get("description"), source.get("longname")),
        "AssetType": "Fixed",
        "Manufacturer": _pset_value(source, _PSET_MFR, "Manufacturer"),
        "ModelNumber": _pset_value(source, _PSET_MFR, "ModelLabel"),
        "WarrantyGuarantorParts": _pset_value(source, _PSET_WARRANTY, "WarrantyIdentifier"),
        "WarrantyDurationParts": _pset_value(source, _PSET_WARRANTY, "WarrantyPeriod"),
        "WarrantyGuarantorLabor": "",
        "WarrantyDurationLabor": "",
        "WarrantyDurationUnit": "year",
        "ExtSystem": EXT_SYSTEM,
        "ExtObject": _first(source.get("type"), "IfcTypeObject"),
        "ExtIdentifier": ext_identifier,
        "ReplacementCost": "",
        "ExpectedLife": _pset_value(source, "Pset_ServiceLife", "ServiceLifeDuration"),
        "DurationUnit": "year",
        "NominalLength": "",
        "NominalWidth": "",
        "NominalHeight": "",
        "ModelReference": _pset_value(source, _PSET_MFR, "ModelLabel"),
        "Shape": "",
        "Color": "",
        "Finish": "",
        "Grade": "",
        "Material": _format_materials(source),
        "SustainabilityPerformance": "",
        "Classifications": _format_classifications(source),
        "Layers": _format_layers(source),
        "BaseQuantities": _format_base_quantities(source),
    }


def _build_type(element_types: list[dict], elements: list[dict]) -> list[dict]:
    """Feuille Type, depuis ``/element-type`` **ou**, à défaut, les composants.

    La route n'existe pas sur toutes les maquettes — ``bimdata-read`` renvoie
    alors une liste vide. Le repli déduit un type par ``object_type`` distinct :
    moins riche, mais une feuille Type vide rendrait le classeur inexploitable
    pour un gestionnaire de patrimoine, qui raisonne par type d'équipement.
    """
    seen: set[str] = set()
    rows: list[dict] = []

    if element_types:
        for entry in element_types:
            name = _first(entry.get("name"), f"Type_{(entry.get('uuid') or '')[:8]}")
            if name in seen:
                continue
            seen.add(name)
            rows.append(_type_row(entry, name=name, ext_identifier=entry.get("uuid") or ""))
        return rows

    for element in elements:
        if not _is_component(element):
            continue
        key = str(
            _first(element.get("object_type"), _attr_value(element, "ObjectType"))
            or element.get("type")
            or "UnknownType"
        ).strip()
        if key in seen:
            continue
        seen.add(key)
        rows.append(_type_row(element, name=key, ext_identifier=""))
    return rows


def _build_component(elements: list[dict], index: SpatialIndex) -> list[dict]:
    created_on = _now_iso()
    rows = []
    for element in elements:
        if not _is_component(element):
            continue
        uuid = element.get("uuid") or ""
        type_ref = element.get("element_type") or element.get("type_object")
        if isinstance(type_ref, Mapping):
            type_name = type_ref.get("name") or ""
        else:
            type_name = type_ref or ""
        type_name = _first(
            type_name, element.get("object_type"), _attr_value(element, "ObjectType")
        )
        rows.append(
            {
                "Name": _first(
                    element.get("name"),
                    _attr_value(element, "Name"),
                    f"{element.get('type') or 'Element'}_{uuid[:8]}",
                ),
                "CreatedBy": "n/a",
                "CreatedOn": created_on,
                "TypeName": type_name,
                "Space": index.space_name_of_element(uuid),
                "FloorName": index.storey_name_of_element(uuid),
                "Description": _first(
                    element.get("description"),
                    element.get("longname"),
                    _attr_value(element, "Description"),
                ),
                "ExtSystem": EXT_SYSTEM,
                "ExtObject": _first(element.get("type"), "IfcElement"),
                "ExtIdentifier": uuid,
                "SerialNumber": _pset_value(element, _PSET_MFR, "SerialNumber"),
                "InstallationDate": "",
                "WarrantyStartDate": "",
                "TagNumber": _pset_value(element, _PSET_MFR, "ArticleNumber"),
                "BarCode": "",
                "AssetIdentifier": uuid,
                "Classifications": _format_classifications(element),
                "Layers": _format_layers(element),
                "BaseQuantities": _format_base_quantities(element),
            }
        )
    return rows


def _attribute_rows(element: Mapping[str, Any], sheet_name: str, created_on: str) -> list[dict]:
    rows = []
    row_name = _first(element.get("name"), element.get("uuid"))
    for pset in element.get("property_sets") or []:
        pset_name = pset.get("name") or ""
        if pset_name in _ATTRIBUTE_SKIP_PSETS:
            continue
        for prop in pset.get("properties") or []:
            definition = prop.get("definition") or {}
            value = prop.get("value")
            if value in (None, ""):
                continue
            rows.append(
                {
                    "Name": f"{pset_name}.{definition.get('name') or ''}",
                    "CreatedBy": "n/a",
                    "CreatedOn": created_on,
                    "Category": pset_name,
                    "SheetName": sheet_name,
                    "RowName": row_name,
                    "Value": ", ".join(map(str, value)) if isinstance(value, list) else str(value),
                    "Unit": definition.get("unit") or "",
                    "ExtSystem": EXT_SYSTEM,
                    "ExtObject": element.get("type") or "",
                    "ExtIdentifier": element.get("uuid") or "",
                    "Description": definition.get("description") or "",
                    "AllowedValues": "",
                }
            )
    return rows


def _build_attribute(
    buildings: list[dict], storeys: list[dict], spaces: list[dict], elements: list[dict]
) -> list[dict]:
    created_on = _now_iso()
    rows: list[dict] = []
    for building in buildings:
        rows += _attribute_rows(building, "Facility", created_on)
    for storey in storeys:
        rows += _attribute_rows(storey, "Floor", created_on)
    for space in spaces:
        rows += _attribute_rows(space, "Space", created_on)
    for element in elements:
        if (element.get("type") or "").lower() not in _SPATIAL_TYPES | {""}:
            rows += _attribute_rows(element, "Component", created_on)
    return rows


# ── Orchestration ─────────────────────────────────────────────────────


def _safe(label: str, call, warnings: list[str], default):
    """Une route en échec dégrade la feuille concernée, pas l'export entier.

    Un classeur partiel dont les manques sont listés vaut mieux qu'une absence
    de classeur : l'auditeur voit ce qu'il a, et pourquoi il n'a pas le reste.
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 — l'origine importe peu, le manque est signalé
        warnings.append(f"{label} : {type(exc).__name__} — {exc}")
        return default


def build_sheets_from_bimdata(client) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    """Construit les 8 feuilles COBie depuis un client ``bimdata-read``.

    Returns:
        ``(sheets, report)`` — le second porte le nom de modèle, les compteurs
        par feuille et les routes en échec.
    """
    warnings: list[str] = []
    project = _safe("projet", client.get_project, warnings, {}) or {}
    model = _safe("modèle", client.get_model, warnings, {}) or {}
    buildings = _safe("bâtiments", client.get_buildings, warnings, []) or []
    storeys = _safe("étages", client.get_storeys, warnings, []) or []
    spaces = _safe("espaces", client.get_spaces, warnings, []) or []
    zones = _safe("zones", client.get_zones, warnings, []) or []
    elements = _safe("éléments", client.get_raw_elements, warnings, []) or []
    # Routes optionnelles : on veut la CAUSE d'un vide, pas seulement le vide.
    # Une feuille System absente peut venir d'une route inexistante, d'un
    # bâtiment sans systèmes, ou d'un accès rompu — trois situations que le
    # lecteur du classeur doit pouvoir distinguer.
    system_diag = _safe("systèmes", client.get_systems_diagnostic, warnings, None)
    type_diag = _safe("types d'éléments", client.get_element_types_diagnostic, warnings, None)
    systems = list(system_diag.items) if system_diag else []
    element_types = list(type_diag.items) if type_diag else []
    tree = _safe("arbre spatial", client.get_structure_tree, warnings, None)

    # ``/space`` ne renvoie pas les psets : on les reprend depuis les éléments,
    # sans quoi toutes les surfaces de la feuille Space seraient vides.
    elements_by_uuid = {e.get("uuid"): e for e in elements if e.get("uuid")}
    for space in spaces:
        source = elements_by_uuid.get(space.get("uuid"))
        if source and not space.get("property_sets"):
            space["property_sets"] = source.get("property_sets") or []

    index = SpatialIndex(tree, spaces, elements)
    index.bind_storeys(storeys)

    sheets = {
        "Facility": _build_facility(buildings, project, model),
        "Floor": _build_floor(storeys),
        "Space": _build_space(spaces, index),
        "Zone": _build_zone(zones),
        "Type": _build_type(element_types, elements),
        "Component": _build_component(elements, index),
        "System": _build_system(systems),
        "Attribute": _build_attribute(buildings, storeys, spaces, elements),
    }

    report = {
        "model_name": _first(model.get("name"), project.get("name"), "cobie_output"),
        "project_name": project.get("name") or "",
        "counts": {name: len(rows) for name, rows in sheets.items()},
        "type_source": "element_type" if element_types else "derived_from_components",
        "system_source": "system_route" if systems else "absent",
        "type_route_status": type_diag.status if type_diag else "unavailable",
        "system_route_status": system_diag.status if system_diag else "unavailable",
        "type_route": type_diag.to_dict(include_items=False) if type_diag else None,
        "system_route": system_diag.to_dict(include_items=False) if system_diag else None,
        "access_verified": bool(
            (type_diag and type_diag.access_verified)
            or (system_diag and system_diag.access_verified)
        ),
        "warnings": warnings,
    }
    return sheets, report
