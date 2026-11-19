"""Audit des classifications : manquantes, incohérences et propositions."""

from __future__ import annotations

from bimdata_mcp.model_view import Record
from bimdata_mcp.quality.classification_audit import detect_system, run_classification_audit


def _r(uuid, cls, otype, *codes):
    return Record(uuid=uuid, ifc_class=cls, attrs={"Name": f"{otype}:{uuid}", "ObjectType": otype},
                  classifications=list(codes))


def _records():
    panel = "Family9:Electrical Panel"
    return [
        _r("E1", "IfcElectricDistributionBoard", panel, "Uniformat:D5010200"),
        _r("E2", "IfcElectricDistributionBoard", panel, "Uniformat:D5010200"),
        _r("E3", "IfcElectricDistributionBoard", panel),                                  # manquante
        _r("E4", "IfcElectricDistributionBoard", panel, "Uniformat:D5010300"),            # écart au type
        _r("E5", "IfcElectricDistributionBoard", panel, "Omniclass:23-33"),               # autre système seul
        _r("W1", "IfcWall", "Mur BA 20", "Uniformat:D5010200"),                           # incohérent classe
        _r("W2", "IfcWall", "Mur BA 20", "Uniformat:B2010"),
        _r("W3", "IfcWall", "Mur BA 20", "Uniformat:XYZ"),                                # format
        _r("D1", "IfcDoor", "Porte 90", "Uniformat:C1020", "Uniformat:B2030"),            # deux codes
        _r("P1", "IfcBuildingElementProxy", "Proxy", "Uniformat:D5010200"),               # non jugé
        _r("O1", "IfcOpeningElement", "Opening"),                                         # non physique
    ]


def _by(res):
    return {(i.uuid, i.req_id): i for i in res.issues}


def test_detect_system():
    assert detect_system(_records()) == "Uniformat"


def test_missing_and_inconsistencies_with_proposals():
    res, _ = run_classification_audit(_records(), "Uniformat")
    by = _by(res)
    assert set(by) == {("E3", "K01"), ("E5", "K01"), ("E4", "K05"), ("W1", "K04"), ("W3", "K03"), ("D1", "K02")}
    # manquante : code majoritaire du même type (2/3, E4 s'écarte) → proposé, confiance moyenne
    assert by[("E3", "K01")].proposal == "Uniformat:D5010200" and by[("E3", "K01")].confidence == 0.6
    assert by[("E5", "K01")].status == "autre_systeme"
    # écart au type : code majoritaire du type
    assert by[("E4", "K05")].proposal == "Uniformat:D5010200"
    # mur classé en électricité : incohérent, proposition = code cohérent du même type
    assert by[("W1", "K04")].status == "incoherent_classe" and by[("W1", "K04")].proposal == "Uniformat:B2010"
    assert "B20" in by[("W1", "K04")].expected
    assert by[("W3", "K03")].status == "format_invalide"
    assert by[("D1", "K02")].status == "codes_multiples"
    assert "P1" not in {i.uuid for i in res.issues} and "O1" not in res.checked_uuids
    assert all(i.writable for i in res.issues)


def test_non_uniformat_system_skips_family_checks():
    recs = [_r("A", "IfcWall", "T", "Omniclass:23-33"), _r("B", "IfcWall", "T", "Omniclass:23-33"),
            _r("C", "IfcWall", "T")]
    res, _ = run_classification_audit(recs, "Omniclass")
    assert [(i.uuid, i.req_id, i.proposal) for i in res.issues] == [("C", "K01", "Omniclass:23-33")]
    assert res.issues[0].confidence == 0.9  # type unanime → proposition fiable


def test_classification_routes_match_api():
    """Création au niveau projet (le niveau maquette refuse POST : 405), association au niveau maquette."""
    from bimdata_mcp.bimdata import BIMDataClient

    c = BIMDataClient(base_url="https://api.bimdata.io", cloud_id=1, project_id=2, model_id=3, api_key="k")
    calls = []
    c._post = lambda path, json, timeout=None: calls.append(("POST", path, json)) or {"id": 9}
    c._get = lambda path, params=None: calls.append(("GET", path)) or []
    c._delete = lambda path, timeout=None: calls.append(("DELETE", path))
    c.list_model_classifications()
    assert c.create_model_classification(name="Uniformat - 1998", notation="D4030", title="Fire")["id"] == 9
    c.assign_classification_elements([{"element_uuid": "U", "classification_id": 9}])
    c.remove_element_classification("U", 7)
    assert [x[:2] for x in calls] == [
        ("GET", "/cloud/1/project/2/classification"),
        ("POST", "/cloud/1/project/2/classification"),
        ("POST", "/cloud/1/project/2/model/3/classification-element"),
        ("DELETE", "/cloud/1/project/2/model/3/element/U/classification/7"),
    ]


def test_snapshot_classification_system_read_from_type():
    """Le snapshot bimdata-read porte le système dans « type » (et non « name »)."""
    from types import SimpleNamespace

    from bimdata_mcp.model_view import build_records

    el = {"uuid": "X", "type": "IfcWall", "name": "Mur", "attributes": {"properties": []}, "property_sets": [],
          "classifications": [{"id": 1, "notation": "D4090100", "type": "Uniformat - 1998",
                               "description": "Carbon Dioxide Systems"}]}
    snap = SimpleNamespace(elements=[el], structure_tree=[], zones=[], spaces=[], storeys=[], buildings=[],
                           sites=[], model={}, project={})
    rec = next(r for r in build_records(snap) if r.uuid == "X")
    assert rec.classifications == ["Uniformat - 1998:D4090100"]
