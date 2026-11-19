"""Petits fichiers IFC construits pour les tests géométriques (ifcopenshell.api).

Unités du fichier : millimètres (pour vérifier la conversion en mètres de l'index).
Chaque élément est une boîte extrudée posée à une position monde donnée en mètres.
"""

from __future__ import annotations

from pathlib import Path

import ifcopenshell
import ifcopenshell.api
import numpy as np

MM = 1000.0


class IfcBuilder:
    def __init__(self, schema: str = "IFC4"):
        f = self.f = ifcopenshell.api.run("project.create_file", version=schema)
        self.project = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name="Test")
        length = ifcopenshell.api.run("unit.add_si_unit", f, unit_type="LENGTHUNIT", prefix="MILLI")
        ifcopenshell.api.run("unit.assign_unit", f, units=[length])
        model = ifcopenshell.api.run("context.add_context", f, context_type="Model")
        self.body = ifcopenshell.api.run("context.add_context", f, context_type="Model", context_identifier="Body",
                                         target_view="MODEL_VIEW", parent=model)
        self.site = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcSite", name="Site")
        self.building = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuilding", name="Bât")
        ifcopenshell.api.run("aggregate.assign_object", f, products=[self.site], relating_object=self.project)
        ifcopenshell.api.run("aggregate.assign_object", f, products=[self.building], relating_object=self.site)
        self.storeys: dict[str, object] = {}

    def storey(self, name: str, elevation_m: float, long_name: str | None = None):
        f = self.f
        s = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=name)
        s.LongName = long_name
        s.Elevation = elevation_m * MM
        m = np.eye(4)
        m[2, 3] = elevation_m * MM
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=s, matrix=m, is_si=False)
        ifcopenshell.api.run("aggregate.assign_object", f, products=[s], relating_object=self.building)
        self.storeys[name] = s
        return s

    def box(self, ifc_class: str, name: str, origin_m, size_m, *, storey: str | None = None, rot_deg: float = 0.0,
            **attrs):
        """Boîte de dimensions ``size_m`` (x, y, z) dont le coin bas est à ``origin_m`` (monde, m)."""
        f = self.f
        el = ifcopenshell.api.run("root.create_entity", f, ifc_class=ifc_class, name=name)
        for k, v in attrs.items():
            setattr(el, k, v)
        a = np.radians(rot_deg)
        m = np.eye(4)
        m[:2, :2] = [[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]
        m[:3, 3] = np.asarray(origin_m, float) * MM
        ifcopenshell.api.run("geometry.edit_object_placement", f, product=el, matrix=m, is_si=False)
        sx, sy, sz = (v * MM for v in size_m)
        prof = f.createIfcRectangleProfileDef(
            "AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint((sx / 2, sy / 2))), sx, sy)
        solid = f.createIfcExtrudedAreaSolid(
            prof, f.createIfcAxis2Placement3D(f.createIfcCartesianPoint((0.0, 0.0, 0.0))),
            f.createIfcDirection((0.0, 0.0, 1.0)), sz)
        rep = f.createIfcShapeRepresentation(self.body, "Body", "SweptSolid", [solid])
        ifcopenshell.api.run("geometry.assign_representation", f, product=el, representation=rep)
        if storey:
            ifcopenshell.api.run("spatial.assign_container", f, products=[el], relating_structure=self.storeys[storey])
        return el

    def no_geometry(self, ifc_class: str, name: str, storey: str | None = None):
        el = ifcopenshell.api.run("root.create_entity", self.f, ifc_class=ifc_class, name=name)
        if storey:
            ifcopenshell.api.run("spatial.assign_container", self.f, products=[el],
                                 relating_structure=self.storeys[storey])
        return el

    def door_in_wall(self, wall, name: str, origin_m, size_m, *, storey: str, operation: str | None = None):
        """Porte + réservation dans ``wall`` (IfcRelVoidsElement / IfcRelFillsElement)."""
        f = self.f
        opening = self.box("IfcOpeningElement", f"Res {name}", origin_m, size_m)
        ifcopenshell.api.run("feature.add_feature", f, feature=opening, element=wall)
        door = self.box("IfcDoor", name, origin_m, size_m, storey=storey,
                        OverallHeight=size_m[2] * MM, OverallWidth=size_m[0] * MM)
        if operation:
            door.OperationType = operation
        ifcopenshell.api.run("feature.add_filling", f, opening=opening, element=door)
        return door

    def write(self, path: Path) -> Path:
        self.f.write(str(path))
        return path
