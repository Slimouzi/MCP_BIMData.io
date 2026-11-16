"""Fixtures : maquette synthétique + faux client BIMData (lecture/écriture en mémoire).

Aucun appel réseau : ``make_client`` est remplacé par :class:`FakeClient`, qui
sert les routes lues par ``bimdata-read`` et applique les écritures sur sa
copie du modèle (ce qui permet de vérifier qu'un contrôle relancé après
correction voit bien la correction).
"""

from __future__ import annotations

import copy
import itertools
import os

import pytest

_ids = itertools.count(1000)


def _prop(name, value, vt="IfcLabel"):
    return {"id": next(_ids), "definition": {"name": name, "value_type": vt}, "value": value}


def _el(uuid, typ, name, *, object_type="Type A", psets=None, materials=(), longname=None):
    attrs = [_prop("Name", name), _prop("ObjectType", object_type), _prop("Description", None, "IfcText")]
    if longname is not None:
        attrs.append(_prop("LongName", longname))
    return {
        "uuid": uuid, "type": typ, "name": name, "object_type": object_type, "longname": longname,
        "attributes": {"id": next(_ids), "name": "", "type": "Attributes", "properties": attrs},
        "property_sets": [{"id": next(_ids), "name": pn, "type": "IfcPropertySet", "properties": props}
                          for pn, props in (psets or {}).items()],
        "classifications": [], "layers": [{"name": "A-WALL"}],
        "material_list": [{"material": {"name": m}} for m in materials],
    }


def build_model() -> dict:
    els = [
        _el("W1", "IfcWallStandardCase", "Mur de base:BA 200:123456", object_type="Mur de base:BA 200",
            materials=["Béton"], psets={"Pset_WallCommon": [_prop("IsExternal", True, "IfcBoolean"),
                                                           _prop("LoadBearing", True, "IfcBoolean"),
                                                           _prop("FireRating", "EI 60")],
                                        "Qto_WallBaseQuantities": [_prop("Length", 7.063, "IfcLengthMeasure"),
                                                                   _prop("NetVolume", 3.78, "IfcVolumeMeasure"),
                                                                   _prop("Width", 0.2, "IfcLengthMeasure")]}),
        _el("W2", "IfcWallStandardCase", "Cloison 72", object_type="Cloison 72", materials=["Plâtre"],
            psets={"Pset_WallCommon": [_prop("IsExternal", False, "IfcBoolean"), _prop("FireRating", "")],
                   "Qto_WallBaseQuantities": [_prop("Length", 4.5, "IfcLengthMeasure"),
                                              _prop("NetVolume", 0.32, "IfcVolumeMeasure"),
                                              _prop("Width", 0.072, "IfcLengthMeasure")]}),
        _el("W3", "IfcWall", " Mur  ext ", object_type="Mur ext", materials=[],
            psets={"Pset_WallCommon": [_prop("IsExternal", True, "IfcBoolean"), _prop("LoadBearing", True, "IfcBoolean")],
                   "Pset_Custom": [_prop("FireRating", "EI60")]}),
        _el("D1", "IfcDoor", "Porte 90", object_type="Porte 90",
            psets={"Pset_DoorCommon": [_prop("IsExternal", False, "IfcBoolean"), _prop("AcousticRating", "35")]}),
        _el("S1", "IfcSpace", "R01-001", longname="bureau", object_type="ESPACE"),
        _el("S2", "IfcSpace", "R01-001", longname="SALLE DE REUNION", object_type="ESPACE"),
        _el("S3", "IfcSpace", "R02-001", longname="Circulation", object_type="ESPACE"),
        _el("O1", "IfcOpeningElement", "Opening", object_type="Opening"),
    ]
    tree = [{"type": "project", "uuid": "P", "name": "Projet", "children": [
        {"type": "site", "uuid": "SITE", "name": "Site", "children": [
            {"type": "building", "uuid": "B1", "name": "BAT A", "children": [
                {"type": "storey", "uuid": "ST1", "name": "R+1", "children": [
                    {"type": "wall_standard_case", "uuid": "W1", "name": "x", "children": []},
                    {"type": "wall_standard_case", "uuid": "W2", "name": "x", "children": []},
                    {"type": "door", "uuid": "D1", "name": "x", "children": []},
                    {"type": "space", "uuid": "S1", "name": "R01-001", "children": []},
                    {"type": "space", "uuid": "S2", "name": "R01-001", "children": []},
                    {"type": "opening_element", "uuid": "O1", "name": "x", "children": []},
                ]},
                {"type": "storey", "uuid": "ST2", "name": "R+2", "children": [
                    {"type": "wall", "uuid": "W3", "name": "x", "children": []},
                    {"type": "space", "uuid": "S3", "name": "R02-001", "children": []},
                ]},
            ]}]}]}]
    return {
        "project": {"id": 2, "name": "Projet test"},
        "model": {"id": 3, "name": "Maquette test.ifc", "modified_date": "2026-11-01T00:00:00Z"},
        "sites": [{"uuid": "SITE", "name": "Site"}],
        "buildings": [{"uuid": "B1", "name": "BAT A"}],
        "storeys": [{"uuid": "ST1", "name": "R+1"}, {"uuid": "ST2", "name": "R+2"}],
        "spaces": [{"uuid": "S1", "name": "R01-001"}, {"uuid": "S2", "name": "R01-001"}, {"uuid": "S3", "name": "R02-001"}],
        "zones": [{"uuid": "Z1", "name": "ZONE A01", "spaces": [{"uuid": "S1"}, {"uuid": "S2"}], "zones": []}],
        "elements": els,
        "structure_tree": tree,
    }


class FakeClient:
    """Imite ``BIMDataClient`` : routes de lecture + écritures en mémoire."""

    instances: list = []

    def __init__(self, model: dict, **ids):
        self.m = model
        self.cloud_id, self.project_id, self.model_id = ids.get("cloud_id"), ids.get("project_id"), ids.get("model_id")
        self.topics: list[dict] = []
        self.writes: list[tuple] = []
        self.documents: list[dict] = []
        FakeClient.instances.append(self)

    # lecture (bimdata-read)
    def get_project(self): return copy.deepcopy(self.m["project"])
    def get_model(self): return copy.deepcopy(self.m["model"])
    def get_sites(self): return copy.deepcopy(self.m["sites"])
    def get_buildings(self): return copy.deepcopy(self.m["buildings"])
    def get_storeys(self): return copy.deepcopy(self.m["storeys"])
    def get_spaces(self): return copy.deepcopy(self.m["spaces"])
    def get_zones(self): return copy.deepcopy(self.m["zones"])
    def get_raw_elements(self): return copy.deepcopy(self.m["elements"])
    def get_structure_tree(self): return copy.deepcopy(self.m["structure_tree"])

    def _diag(self):
        from bimdata_read.client import RouteDiagnostic
        return RouteDiagnostic(status="ok_empty", items=[], route="x", access_verified=True)

    def get_systems_diagnostic(self): return self._diag()
    def get_element_types_diagnostic(self): return self._diag()

    # navigation / GED
    def list_clouds(self): return [{"id": 1, "name": "Espace"}]
    def list_projects(self, c): return [{"id": 2, "name": "Projet test"}]
    def list_models(self, c, p): return [{"id": 3, "name": "Maquette test.ifc", "type": "IFC"}]
    def list_documents(self): return self.documents
    def upload_document(self, path, name=None, parent_id=None):
        doc = {"id": len(self.documents) + 1, "name": name or path.name}
        self.documents.append(doc)
        return doc

    # écritures
    def _el(self, uuid):
        return next(e for e in self.m["elements"] if e["uuid"] == uuid)

    def get_element(self, uuid): return copy.deepcopy(self._el(uuid))
    def get_element_propertysets(self, uuid): return copy.deepcopy(self._el(uuid)["property_sets"])

    def _pset(self, uuid, pset_id):
        el = self._el(uuid)
        if el["attributes"]["id"] == pset_id:
            return el["attributes"]
        return next(p for p in el["property_sets"] if p["id"] == pset_id)

    def update_element_property(self, uuid, pset_id, prop_id, value):
        pset = self._pset(uuid, pset_id)
        prop = next(p for p in pset["properties"] if p["id"] == prop_id)
        prop["value"] = value
        if prop["definition"]["name"] == "Name":
            self._el(uuid)["name"] = value
        pset["id"] = next(_ids)  # l'API reconstruit un pset propre à l'élément
        self.writes.append(("patch", uuid, prop["definition"]["name"], value))
        self.m["model"]["modified_date"] = self.m["model"]["modified_date"]  # inchangé, comme l'API

    def create_element_property(self, uuid, pset_id, name, value, value_type):
        self._pset(uuid, pset_id)["properties"].append(_prop(name, value, value_type))
        self.writes.append(("create_prop", uuid, name, value))

    def write_element_propertyset(self, uuid, payload):
        self._el(uuid)["property_sets"].append({"id": next(_ids), "name": payload["name"], "properties": [
            _prop(p["definition"]["name"], p["value"], p["definition"]["value_type"]) for p in payload["properties"]]})
        self.writes.append(("create_pset", uuid, payload["name"]))

    def create_bcf_full_topic(self, payload):
        self.topics.append(payload)
        return {"guid": f"topic-{len(self.topics)}"}


@pytest.fixture()
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCP_AUTH_MODE", "none")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://testserver")
    monkeypatch.setenv("MCP_FILE_SECRET", "test-secret")
    from bimdata_mcp import bimdata, config, service

    config.get_settings.cache_clear()
    bimdata._mem.clear()
    bimdata._dirty.clear()
    source = os.getenv("BIMDATA_MCP_TEST_SNAPSHOT")
    if source:
        import gzip
        import json

        data = json.loads(gzip.decompress(open(source, "rb").read()))
        model = {k: data[k] for k in ("project", "model", "sites", "buildings", "storeys", "spaces", "zones",
                                      "elements", "structure_tree")}
    else:
        model = build_model()
    FakeClient.instances.clear()
    make = lambda target, identity: FakeClient(model, **target.to_dict())  # noqa: E731
    monkeypatch.setattr(service, "make_client", make)
    monkeypatch.setattr(service, "make_navigation_client", lambda identity: FakeClient(model))
    yield model
    config.get_settings.cache_clear()


@pytest.fixture()
def mcp_call(fake_env):
    """Appelle un outil MCP par le vrai client FastMCP (in-memory) et renvoie le dict résultat."""
    import asyncio

    from fastmcp import Client

    from bimdata_mcp.server import mcp

    def call(tool: str, **args):
        async def go():
            async with Client(mcp) as c:
                res = await c.call_tool(tool, args, raise_on_error=True)
                return res.structured_content if res.structured_content is not None else res.data
        return asyncio.run(go())

    return call
