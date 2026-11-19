"""Lot 3 : distances, dégagements, accessibilité PMR, incendie (IFC construits, unités : mm)."""

from __future__ import annotations

import ifcopenshell.api
import numpy as np
import pytest

pytest.importorskip("ifcopenshell")

from bimdata_mcp.geometry.index import get_or_build  # noqa: E402
from bimdata_mcp.model_view import Record  # noqa: E402
from bimdata_mcp.quality.lot3 import (  # noqa: E402
    parse_rating,
    rating_ok,
    run_accessibility_rules,
    run_clearance_rules,
    run_distance_rules,
    run_fire_rules,
)
from tests.ifc_factory import IfcBuilder  # noqa: E402

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _space(b, name, origin, size, storey="RDC"):
    sp = b.box("IfcSpace", name, origin, size)
    sp.LongName = name
    ifcopenshell.api.run("aggregate.assign_object", b.f, products=[sp], relating_object=b.storeys[storey])
    return sp


def _model(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    g = {}
    # Porte battante dans un mur ; un poteau à 0,5 m côté +Y (ouverture), une armoire à 0,5 m côté −Y.
    wall = b.box("IfcWall", "Mur", (0, 0, 0), (6, 0.2, 3), storey="RDC")
    g["wall"] = wall.GlobalId
    g["door"] = b.door_in_wall(wall, "Porte battante", (2.0, 0.0, 0), (0.9, 0.2, 2.1), storey="RDC",
                               operation="SINGLE_SWING_LEFT").GlobalId
    g["col"] = b.box("IfcColumn", "Poteau", (2.3, 0.7, 0), (0.3, 0.3, 3), storey="RDC").GlobalId
    g["cab"] = b.box("IfcFurnishingElement", "Armoire", (2.2, -0.8, 0), (0.5, 0.3, 1.8), storey="RDC").GlobalId
    g["door2"] = b.door_in_wall(wall, "Porte inconnue", (4.5, 0.0, 0), (0.9, 0.2, 2.1), storey="RDC").GlobalId
    g["cab2"] = b.box("IfcFurnishingElement", "Meuble", (4.7, -0.6, 0), (0.5, 0.3, 1.0), storey="RDC").GlobalId
    # Distances : prise à 0,30 m d'un lavabo ; luminaire au-dessus d'une table.
    g["sink"] = b.box("IfcSanitaryTerminal", "Lavabo", (10, 0, 0.8), (0.5, 0.4, 0.2), storey="RDC").GlobalId
    g["outlet"] = b.box("IfcOutlet", "Prise", (10.8, 0, 1.0), (0.08, 0.05, 0.08), storey="RDC").GlobalId
    g["table"] = b.box("IfcFurnishingElement", "Table", (20, 0, 0), (1, 1, 0.75), storey="RDC").GlobalId
    g["lamp"] = b.box("IfcLightFixture", "Suspension", (20.3, 0.3, 2.0), (0.3, 0.3, 0.1), storey="RDC").GlobalId
    # Locaux : un séjour 4 × 4 libre, un WC 1,2 × 2,5, un local 3 × 3 avec un bloc central.
    g["sejour"] = _space(b, "SEJOUR", (30, 0, 0), (4, 4, 2.6)).GlobalId
    g["wc"] = _space(b, "WC", (40, 0, 0), (1.2, 2.5, 2.6)).GlobalId
    g["bloc_sp"] = _space(b, "CELLIER", (50, 0, 0), (3, 3, 2.6)).GlobalId
    g["bloc"] = b.box("IfcFurnishingElement", "Bloc", (51, 1, 0), (1, 1, 1), storey="RDC").GlobalId
    # Deux locaux de compartiments différents de part et d'autre d'un mur.
    g["l1"] = _space(b, "Logement A", (60, 0, 0), (4, 4, 2.6)).GlobalId
    g["sep"] = b.box("IfcWall", "Mur séparatif", (60, 4, 0), (4, 0.2, 2.6), storey="RDC").GlobalId
    g["l2"] = _space(b, "Logement B", (60, 4.2, 0), (4, 4, 2.6)).GlobalId
    g["exit"] = b.box("IfcDoor", "Sortie", (60, -0.1, 0), (0.9, 0.1, 2.1), storey="RDC").GlobalId
    return b.write(tmp_path / "lot3.ifc"), g


@pytest.fixture()
def built(tmp_path):
    path, g = _model(tmp_path)
    idx, _ = get_or_build(path, tmp_path / "c", key="k")
    return idx, g, path


def _records(idx, **extra):
    recs = {u: Record(uuid=u, ifc_class=e["ifc_class"], attrs={"Name": e["name"], "LongName": e["name"]})
            for u, e in idx.elements.items()}
    for u, (attrs, props, zone) in extra.items():
        recs[u].attrs.update(attrs)
        _props(recs[u], props)
        recs[u].zone = zone
    return recs


def _props(rec, props):
    """Comme build_records : index des propriétés par nom, pour les chemins « *.Prop »."""
    for k, v in props.items():
        rec.props[k] = v
        rec._by_prop.setdefault(k.split(".", 1)[1].lower(), []).append(k)


def test_distance_modes(built):
    idx, g, _ = built
    recs = _records(idx)
    res, _ = run_distance_rules(idx, recs, [g["sink"]], [g["outlet"]], mode="min", distance_m=0.5)
    assert len(res.issues) == 1 and res.issues[0].measure["value"] == pytest.approx(0.3, abs=1e-6)
    res, _ = run_distance_rules(idx, recs, [g["sink"]], [g["outlet"]], mode="max", distance_m=0.2)
    assert len(res.issues) == 1 and "plus de 0.2 m" in res.issues[0].message
    res, _ = run_distance_rules(idx, recs, [g["lamp"]], [g["table"]], mode="min", distance_m=1.5, direction="below")
    assert res.issues[0].measure["value"] == pytest.approx(1.25, abs=1e-6)
    res, _ = run_distance_rules(idx, recs, [g["lamp"]], [g["table"]], mode="min", distance_m=0.1,
                                direction="horizontal")
    assert res.issues and res.issues[0].measure["value"] == 0.0  # en plan, la suspension est au-dessus de la table


def test_clearance_opening_side(built):
    idx, g, _ = built
    recs = _records(idx, **{g["door"]: ({"OverallWidth": 0.9}, {}, ""), g["door2"]: ({"OverallWidth": 0.9}, {}, "")})
    res, _ = run_clearance_rules(idx, recs, [g["door"], g["door2"]], obstacles=None, front_m=1.0, back_m=0.0,
                                 side_m=0.0, height_m=2.0, width_m=None, use_opening_side=True)
    got = {(i.uuid, i.elements[1]["uuid"]) for i in res.issues}
    assert (g["door"], g["col"]) in got  # côté ouverture (+Y)
    assert (g["door"], g["cab"]) not in got  # côté opposé ignoré : porte battante, back_m = 0
    assert (g["door2"], g["cab2"]) in got  # sens inconnu : les deux côtés
    assert all(i.elements[1]["uuid"] != g["wall"] for i in res.issues)  # mur hôte exclu
    col = next(i for i in res.issues if i.elements[1]["uuid"] == g["col"])
    assert col.measure["zone"] == "côté ouverture" and col.measure["value"] == pytest.approx(0.3, abs=1e-3)


def test_accessibility(built):
    idx, g, _ = built
    recs = _records(idx, **{
        g["door"]: ({"OverallWidth": 0.83}, {"Pset_DoorCommon.IsExternal": False,
                                             "Pset_DoorCommon.HandicapAccessible": True}, ""),
        g["door2"]: ({"OverallWidth": 0.83}, {"Pset_DoorCommon.IsExternal": True}, ""),
    })
    doors = [g["door"], g["door2"]]
    spaces = [g["sejour"], g["wc"], g["bloc_sp"]]
    res, notes, extra = run_accessibility_rules(idx, recs, referentiel="BHC 2015", thresholds=None, doors=doors,
                                                spaces=spaces)
    by = {}
    for i in res.issues:
        by.setdefault(i.req_id, []).append(i)
    assert [i.uuid for i in by["A1"]] == [g["door2"]]  # entrée 0,83 < 0,90 ; intérieure 0,83 ≥ 0,80
    a3 = {i.uuid: i.measure["value"] for i in by["A3"]}
    assert set(a3) == {g["wc"], g["bloc_sp"]} and a3[g["wc"]] == pytest.approx(1.2, abs=0.1)
    assert any("A2 non contrôlée" in n for n in notes) and any("A4 non contrôlée" in n for n in notes)
    assert {i.uuid for i in by["A6"]} == {g["door2"], *spaces}
    res2, _, _ = run_accessibility_rules(idx, recs, referentiel="ERP 2017", thresholds=None, doors=doors, spaces=[],
                                         checks=["A1"])
    assert len(res2.issues) == 2  # ERP : 0,90 partout
    res3, notes3, _ = run_accessibility_rules(idx, recs, referentiel="BHC 2015", doors=doors, spaces=[], checks=["A2"],
                                              thresholds={"a2_width_m": 1.2, "a2_pull_m": 1.7, "a2_push_m": 1.4})
    assert {i.uuid for i in res3.issues} >= {g["door"]}
    with pytest.raises(ValueError, match="Référentiel"):
        run_accessibility_rules(idx, recs, referentiel="XYZ", thresholds=None, doors=[], spaces=[])


def test_fire(built):
    idx, g, _ = built
    assert parse_rating("REI 60") == ({"R", "E", "I"}, 60) and parse_rating("1h")[1] == 60
    assert rating_ok("REI90", "EI60") and not rating_ok("EI30", "EI60") and not rating_ok("E60", "EI60")
    recs = _records(idx, **{g["l1"]: ({}, {}, "Logement A"), g["l2"]: ({}, {}, "Logement B"),
                            g["exit"]: ({"OverallWidth": 0.83}, {}, "")})
    res, notes, extra = run_fire_rules(idx, recs, required_ratings=None, compartments="zones",
                                       compartment_rating="EI60", door_rating=None, exits=[g["exit"]], stairs=[],
                                       max_distance_m=5.0, exit_up=1, stairs_up=1)
    assert any(n.startswith("F1 : données absentes") for n in extra["donnees_absentes"])
    assert extra["parois_separatives"] == 1
    f3 = [i for i in res.issues if i.req_id == "F3"]
    assert g["l2"] in {i.uuid for i in f3}  # point le plus éloigné de Logement B à plus de 5 m
    assert [i.uuid for i in res.issues if i.req_id == "F4"] == [g["exit"]]
    _props(recs[g["sep"]], {"Pset_WallCommon.FireRating": "EI30"})
    _props(recs[g["wall"]], {"Pset_WallCommon.FireRating": "EI60"})
    res, _, _ = run_fire_rules(idx, recs, required_ratings={"IfcWall": "EI60"}, compartments="zones",
                               compartment_rating="EI60", door_rating=None, exits=[], stairs=[], max_distance_m=None,
                               exit_up=1, stairs_up=1)
    f1 = {i.uuid: i.status for i in res.issues if i.req_id == "F1"}
    assert f1[g["sep"]] == "sous_seuil" and g["wall"] not in f1
    assert [i.uuid for i in res.issues if i.req_id == "F2"] == [g["sep"]]


def test_lot3_through_mcp(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp import service_geometry

    path, g = _model(tmp_path)
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    d = mcp_call("run_distance_check", set_a="classes:IfcSanitaryTerminal", set_b="classes:IfcOutlet", distance_m=0.5)
    assert d["summary"]["non_conformities"] == 1
    c = mcp_call("run_clearance_check", preset="portes")
    assert c["summary"]["non_conformities"] >= 2 and c["cibles"] == 3
    with pytest.raises(Exception, match="distances sont à fournir"):
        mcp_call("run_clearance_check", preset="fenetres")
    menu = {a["id"]: a for a in mcp_call("list_audits")["audits"]}
    assert menu["c5_pmr"]["status"] == "partiel" and menu["c6_degagements"]["status"] == "pret"
    assert menu["c7_incendie"]["status"] == "partiel" and "sorties détectées automatiquement" in menu["c7_incendie"]["reason"]
    # sans paramètres, le menu demande les référentiels au lieu de lancer avec des valeurs par défaut
    pmr = mcp_call("run_audit", audit="c5_pmr")
    assert pmr["jeton_hypotheses"] is None and [q["option"] for q in pmr["a_demander"]] == ["referentiel"]
    fire = mcp_call("run_audit", audit="c7_incendie")
    assert {q["option"] for q in fire["a_demander"]} == {"f3_referentiel", "eas"} and fire["jeton_hypotheses"] is None
    # sans référentiel, un jeton quelconque ne lance rien
    assert mcp_call("run_audit", audit="c5_pmr", hypotheses_validees="x")["etape"] == "validation_hypotheses"

    def run(aid, **opts):
        tok = mcp_call("run_audit", audit=aid, options=opts)["jeton_hypotheses"]
        return mcp_call("run_audit", audit=aid, options=opts, hypotheses_validees=tok)

    deg = run("c6_degagements")
    assert deg["audit"]["regles_controlees"] == "1 sur 1" and deg["audit"]["anomalies"]
    assert deg["proposition_smart_view"]["appel"]["outil"] == "prepare_smart_view"  # fin d'audit : BCF ou Smart View
    assert deg["proposition_bcf"]["appel"]["outil"] == "prepare_bcf_topics" and "Smart View" in deg["question_suite"]
    assert any("A2 non contrôlée" in w for w in run("c5_pmr", referentiel="ERP 2017")["audit"]["avertissements"])
    f = run("c7_incendie", f3_referentiel="ERP", eas=None)
    assert any("F6 non contrôlée" in w for w in f["audit"]["avertissements"])


def test_thin_floor_threshold_step_and_auto_exits(tmp_path):
    from bimdata_mcp.quality.lot3 import auto_exits

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    g = {}
    # RDC : porte extérieure (dehors côté −Y) ; sol fini de 5 cm devant, côté +Y : pas un obstacle, mais ressaut A7.
    wall = b.box("IfcWall", "Façade", (0, 0, 0), (8, 0.2, 3), storey="RDC")
    g["entree"] = b.door_in_wall(wall, "Entrée", (1, 0, 0), (0.9, 0.2, 2.1), storey="RDC",
                                 operation="SINGLE_SWING_LEFT").GlobalId
    g["hall"] = _space(b, "HALL", (0, 0.2, 0), (8, 5, 2.6)).GlobalId
    g["sol"] = b.box("IfcSlab", "Sol fini 50 mm", (0, 0.2, 0), (8, 5, 0.05), storey="RDC").GlobalId
    # R+1 : porte d'accès à l'escalier (cage « STAIR ») et porte de balcon (jamais une sortie).
    w1 = b.box("IfcWall", "Mur cage", (0, 0, 3), (8, 0.2, 3), storey="R+1")
    g["porte_esc"] = b.door_in_wall(w1, "Porte escalier", (1, 0, 3), (0.9, 0.2, 2.1), storey="R+1").GlobalId
    g["porte_balcon"] = b.door_in_wall(w1, "Porte balcon", (5, 0, 3), (0.9, 0.2, 2.1), storey="R+1").GlobalId
    g["palier"] = _space(b, "PALIER", (0, 0.2, 3), (8, 4, 2.6), storey="R+1").GlobalId
    g["cage"] = _space(b, "STAIR", (0, -3.0, 3), (3, 3, 2.6), storey="R+1").GlobalId
    g["balcon"] = _space(b, "BALCON", (4, -2.0, 3), (3, 2, 2.6), storey="R+1").GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "ex.ifc"), tmp_path / "c", key="k")
    recs = _records(idx, **{g["entree"]: ({"OverallWidth": 0.9}, {"Pset_DoorCommon.IsExternal": True}, ""),
                            g["porte_balcon"]: ({"OverallWidth": 0.9}, {"Pset_DoorCommon.IsExternal": True}, "")})
    res, _ = run_clearance_rules(idx, recs, [g["entree"]], obstacles=None, front_m=1.0, back_m=0.0, side_m=0.0,
                                 height_m=2.0, width_m=None, use_opening_side=True)
    assert not res.issues  # le sol fini de 5 cm n'est pas un obstacle
    a7, _, _ = run_accessibility_rules(idx, recs, referentiel="BHC 2015", thresholds=None, doors=[g["entree"]],
                                       spaces=[], checks=["A7"])
    assert len(a7.issues) == 1 and a7.issues[0].severity == "info"
    assert a7.issues[0].measure["value"] == pytest.approx(0.05, abs=1e-3) and a7.issues[0].measure["cote"] == "+Y"
    exits, info = auto_exits(idx, recs)
    assert set(exits) == {g["entree"], g["porte_esc"]} and info["balcons_exclus"] == 1
    assert info["niveaux_acces"] == ["RDC"] and info["origine_niveaux_acces"].startswith("nom")


def test_multiple_access_storeys(tmp_path):
    """Bâtiment en escalier : un niveau d'accès par bloc (RDC_A, RDC_B), retenus par leur nom ou désignés."""
    from bimdata_mcp.quality.lot3 import auto_exits

    b = IfcBuilder()
    b.storey("RDC_A", 0.0)
    b.storey("RDC_B", 1.5)
    b.storey("R+1_A & TOITURE_B", 3.0)
    b.storey("R+2_A & RDC_C", 6.0)  # étage combiné : une de ses parties est un niveau d'accès
    wa = b.box("IfcWall", "Mur A", (0, 0, 0), (6, 0.2, 3), storey="RDC_A")
    wb = b.box("IfcWall", "Mur B", (20, 0, 1.5), (6, 0.2, 3), storey="RDC_B")
    da = b.door_in_wall(wa, "Entrée A", (1, 0, 0), (0.9, 0.2, 2.1), storey="RDC_A").GlobalId
    db = b.door_in_wall(wb, "Entrée B", (21, 0, 1.5), (0.9, 0.2, 2.1), storey="RDC_B").GlobalId
    _space(b, "Hall A", (0, 0.2, 0), (6, 4, 2.6), storey="RDC_A")
    _space(b, "Hall B", (20, 0.2, 1.5), (6, 4, 2.6), storey="RDC_B")
    idx, _ = get_or_build(b.write(tmp_path / "acc.ifc"), tmp_path / "c", key="k")
    exits, info = auto_exits(idx, _records(idx))
    assert set(exits) == {da, db} and info["niveaux_acces"] == ["RDC_A", "RDC_B", "R+2_A & RDC_C"]
    assert info["exterieures_par_niveau"] == {"RDC_A": 1, "RDC_B": 1, "R+2_A & RDC_C": 0}
    exits, info = auto_exits(idx, _records(idx), access_storeys=["rdc_b"])
    assert exits == [db] and info["origine_niveaux_acces"] == "désignés"
    with pytest.raises(ValueError, match="introuvable"):
        auto_exits(idx, _records(idx), access_storeys="RDC_Z")


def test_stair_doors_detected_by_geometry(tmp_path):
    """Porte d'accès à un escalier sans aucun nom de local : détectée par la distance à l'IfcStair."""
    from bimdata_mcp.quality.lot3 import auto_exits

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    w = b.box("IfcWall", "Mur", (0, 0, 3), (10, 0.2, 3), storey="R+1")
    near = b.door_in_wall(w, "Porte cage", (1, 0, 3), (0.9, 0.2, 2.1), storey="R+1").GlobalId
    far = b.door_in_wall(w, "Porte bureau", (8, 0, 3), (0.9, 0.2, 2.1), storey="R+1").GlobalId
    b.box("IfcStair", "Escalier", (1, -3.5, 0), (1.2, 2.0, 3.0), storey="RDC")  # dessert R+1, à 1,5 m de la porte
    _space(b, "Local 1", (0, 0.2, 3), (10, 4, 2.6), storey="R+1")
    idx, _ = get_or_build(b.write(tmp_path / "st.ifc"), tmp_path / "c", key="k")
    exits, info = auto_exits(idx, _records(idx))
    assert near not in exits and "R+1" in info["etages_sans_sortie"]  # critère de distance désactivé par défaut
    assert info["etages_sans_objet"] == ["RDC"]  # aucun local au RDC : sans objet, pas « non contrôlé »
    exits, info = auto_exits(idx, _records(idx), stair_door_distance_m=1.5)
    assert near in exits and far not in exits
    assert info["par_critere"] == {"proche d'un escalier hors local": 1}


def test_f3_walking_distance_goes_through_doors(tmp_path):
    """Deux locaux séparés par une cloison de 10 cm, reliés par une porte au fond : la distance de marche contourne
    par la porte, la cloison n'est jamais traversée ; un local sans porte est signalé « relié à aucune sortie »."""
    from bimdata_mcp.geometry.paths import storey_paths
    from bimdata_mcp.quality.lot3 import run_fire_rules

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    facade = b.box("IfcWall", "Façade", (0, 0, 0), (15, 0.2, 3), storey="RDC")
    exit_ = b.door_in_wall(facade, "Sortie", (1, 0, 0), (0.9, 0.2, 2.1), storey="RDC").GlobalId
    part = b.box("IfcWall", "Cloison", (4.9, 0.2, 0), (0.1, 8, 3), storey="RDC")
    b.door_in_wall(part, "Porte fond", (4.9, 7, 0), (0.1, 0.9, 2.1), storey="RDC")
    b.box("IfcWall", "Mur plein", (10.0, 0.2, 0), (0.1, 8, 3), storey="RDC")
    a = _space(b, "Local A", (0, 0.2, 0), (4.9, 8, 2.6)).GlobalId
    bb = _space(b, "Local B", (5.0, 0.2, 0), (5.0, 8, 2.6)).GlobalId
    c = _space(b, "Local C", (10.1, 0.2, 0), (4.0, 8, 2.6)).GlobalId  # enclavé : aucune porte
    idx, _ = get_or_build(b.write(tmp_path / "f3.ifc"), tmp_path / "c", key="k")
    doors = [g for g, e in idx.elements.items() if e["ifc_class"] == "IfcDoor"]
    p = storey_paths(idx, [a, bb, c], doors, [exit_])
    da, ua, _ = p.space_distance(a)
    db, ub, far = p.space_distance(bb)
    assert ua == 0 and ub == 0
    assert 8.0 < da < 9.5  # coin opposé de A : ≈ 8,6 m en ligne droite
    # B : remonter A jusqu'à la porte du fond (≈ 7,5 m) puis redescendre B jusqu'au coin bas droit (≈ 8,5 m)
    assert db > 14.0 and far[1] < 1.5
    assert p.space_distance(c)[0] is None
    recs = _records(idx)
    res, notes, _ = run_fire_rules(idx, recs, required_ratings=None, compartments=None, compartment_rating=None,
                                   door_rating=None, exits=[exit_], stairs=[], max_distance_m=12.0, exit_up=1,
                                   stairs_up=1, checks=["F3"])
    f3 = {i.uuid: i for i in res.issues}
    assert set(f3) == {bb, c}
    assert f3[bb].measure["cheminement_m"] > 14.0 and f3[bb].measure["vol_oiseau_m"] < 12.0  # vol d'oiseau : conforme
    assert f3[c].severity == "info" and "aucune sortie" in f3[c].message
    res2, _, _ = run_fire_rules(idx, recs, required_ratings=None, compartments=None, compartment_rating=None,
                                door_rating=None, exits=[exit_], stairs=[], max_distance_m=12.0, exit_up=1,
                                stairs_up=1, checks=["F3"], distance_method="vol_oiseau")
    v1 = {i.uuid: i for i in res2.issues}
    assert bb not in v1  # à vol d'oiseau (11,x m), le détour par la porte du fond disparaît


def test_f3_open_passage_without_door(tmp_path):
    """Réservation sans porte dans un mur : passage libre, la marche le franchit ; le mur plein reste infranchissable."""
    from bimdata_mcp.geometry.paths import storey_paths

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    facade = b.box("IfcWall", "Façade", (0, 0, 0), (10, 0.2, 3), storey="RDC")
    exit_ = b.door_in_wall(facade, "Sortie", (1, 0, 0), (0.9, 0.2, 2.1), storey="RDC").GlobalId
    wall = b.box("IfcWall", "Mur", (4.8, 0.2, 0), (0.2, 6, 3), storey="RDC")
    hole = b.box("IfcOpeningElement", "Passage", (4.8, 4.0, 0), (0.2, 1.2, 2.2))
    ifcopenshell.api.run("feature.add_feature", b.f, feature=hole, element=wall)
    a = _space(b, "Hall", (0, 0.2, 0), (4.8, 6, 2.6)).GlobalId
    c = _space(b, "Salle", (5.0, 0.2, 0), (5.0, 6, 2.6)).GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "pass.ifc"), tmp_path / "c", key="k")
    p = storey_paths(idx, [a, c], [exit_], [exit_])
    dc, uc, far = p.space_distance(c)
    assert dc is not None and uc < 0.05
    assert dc > 10.0 and far[1] < 1.5  # par le passage (y ≈ 4,6 m) puis retour vers le bas de la salle


def test_f3_taut_path_accuracy_and_zone_volumes(tmp_path):
    """Grande salle : la distance tendue vaut la ligne droite (à 2 % près), la grille seule la surestime ; une
    surface IPMS posée sur les pièces est écartée, une chambre dessinée par-dessus son placard ne l'est pas."""
    from bimdata_mcp.geometry.paths import storey_paths
    from bimdata_mcp.quality.lot3 import run_fire_rules

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    facade = b.box("IfcWall", "Façade", (0, 0, 0), (40, 0.2, 3), storey="RDC")
    exit_ = b.door_in_wall(facade, "Sortie", (0.2, 0, 0), (0.9, 0.2, 2.1), storey="RDC").GlobalId
    hall = _space(b, "Salle", (0, 0.2, 0), (20, 10, 2.6)).GlobalId
    ch = _space(b, "Chambre", (20.2, 0.2, 0), (6, 4, 2.6)).GlobalId
    cloison = b.box("IfcWall", "Cloison", (20.0, 0.2, 0), (0.2, 10, 3), storey="RDC")
    b.door_in_wall(cloison, "Porte chambre", (20.0, 1.0, 0), (0.2, 0.9, 2.1), storey="RDC")
    _space(b, "Placard", (25.0, 0.2, 0), (1.2, 0.6, 2.6))  # dans la chambre
    ipms = _space(b, "IPMS2-A", (0, 0.2, 0), (26, 10, 2.6)).GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "taut.ifc"), tmp_path / "c", key="k")
    doors = [g for g, e in idx.elements.items() if e["ifc_class"] == "IfcDoor"]
    p = storey_paths(idx, [hall, ch, ipms], doors, [exit_], ipms={ipms})
    assert p.zones == [ipms]
    r = p.space_path(hall)
    straight = float(np.hypot(20 - 1.1, 10.2 - 0.5))  # depuis le seuil de la sortie (coin côté salle)
    assert abs(r["distance"] - straight) < 0.02 * straight
    assert r["grille"] > r["distance"] and len(r["chemin"]) == 2  # ligne droite : deux sommets
    res, notes, _ = run_fire_rules(idx, _records(idx), required_ratings=None, compartments=None,
                                   compartment_rating=None, door_rating=None, exits=[exit_], stairs=[],
                                   max_distance_m=5.0, exit_up=1, stairs_up=1, checks=["F3"])
    assert any("volume(s) de zone" in n and "IPMS2-A" in n for n in notes)
    assert ch in {i.uuid for i in res.issues}  # la chambre (avec son placard) reste contrôlée
    f = next(i for i in res.issues if i.uuid == hall)
    assert f.measure["chemin"][0] == pytest.approx(list(f.measure["point_le_plus_eloigne"]))


def test_f3_split_floor_levels(tmp_path):
    """Étage combiné : deux planchers à 1,35 m l'un de l'autre, sortie sur le premier seulement — le second est
    « non contrôlé » (ses sorties manquent), pas relié par erreur à la sortie du premier."""
    from bimdata_mcp.quality.lot3 import run_fire_rules

    b = IfcBuilder()
    b.storey("R+2_C & RDC_E", 0.0)
    st = "R+2_C & RDC_E"
    w = b.box("IfcWall", "Façade", (0, 0, 0), (10, 0.2, 3), storey=st)
    exit_ = b.door_in_wall(w, "Sortie", (1, 0, 0), (0.9, 0.2, 2.1), storey=st).GlobalId
    low = _space(b, "Bloc C", (0, 0.2, 0), (10, 6, 2.6), storey=st).GlobalId
    high = _space(b, "Bloc E", (0, 0.2, 1.35), (10, 6, 2.6), storey=st).GlobalId  # même emprise, plus haut
    idx, _ = get_or_build(b.write(tmp_path / "lv.ifc"), tmp_path / "c", key="k")
    res, notes, _ = run_fire_rules(idx, _records(idx), required_ratings=None, compartments=None,
                                   compartment_rating=None, door_rating=None, exits=[exit_], stairs=[],
                                   max_distance_m=50.0, exit_up=1, stairs_up=1, checks=["F3"])
    assert res.per_requirement[0]["applicable"] == 1 and low not in {i.uuid for i in res.issues}
    assert any("plancher à 1.35 m" in n and "1 local" in n for n in notes) and high not in {i.uuid for i in res.issues}


def test_open_stair_and_shaft_hatch(tmp_path):
    """Escalier ouvert (local contenant un escalier, sans porte) : listé, compté comme sortie sur option. Trappe de
    gaine ouvrant sur la cage : jamais une sortie."""
    from bimdata_mcp.quality.lot3 import auto_exits, run_fire_rules

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    w = b.box("IfcWall", "Mur gaines", (8.4, 6.0, 3), (3.6, 0.2, 3), storey="R+1")  # la cage, à gauche, est ouverte
    hatch = b.door_in_wall(w, "Trappe GT", (9, 6.0, 3), (0.6, 0.2, 2.0), storey="R+1").GlobalId
    corridor = _space(b, "Couloir", (0, 0, 3), (12, 6, 2.6), storey="R+1").GlobalId
    _space(b, "GT", (8.5, 6.2, 3), (1.5, 1.0, 2.6), storey="R+1")
    cage = _space(b, "STAIR", (0, 6.2, 3), (8.4, 4, 2.6), storey="R+1").GlobalId  # ouverte : sans porte
    b.box("IfcStair", "Escalier", (1, 7, 3), (1.2, 3.0, 3.0), storey="R+1")
    _space(b, "Cage fermée", (10.1, 6.2, 3), (1.8, 1.5, 2.6), storey="R+1")
    b.box("IfcStair", "Escalier 2", (10.3, 6.4, 3), (1.0, 1.0, 3.0), storey="R+1")
    hatch2 = b.door_in_wall(w, "Trappe GT 2", (10.5, 6.0, 3), (0.6, 0.2, 2.0), storey="R+1").GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "open.ifc"), tmp_path / "c", key="k")
    recs = _records(idx)
    exits, info = auto_exits(idx, recs)
    assert cage in info["escaliers_ouverts"]
    assert hatch not in exits
    assert hatch2 in exits  # porte de couloir à une cage fermée : sortie
    res, _, _ = run_fire_rules(idx, recs, required_ratings=None, compartments=None, compartment_rating=None,
                               door_rating=None, exits=[], stairs=[], max_distance_m=5.0, exit_up=1, stairs_up=1,
                               checks=["F3"], exit_spaces=[cage])
    f = next(i for i in res.issues if i.uuid == corridor)
    assert f.measure["cheminement_m"] < 9.0  # vers la cage ouverte, juste derrière le couloir


def _evac_floor(tmp_path, bureau_door_width=0.9):
    """R+1 : couloir de 30 m, cage A au bout ouest (côté sud), cage B à 20 m (côté nord), bureau au bout est
    (cul-de-sac de ≈ 9 m entre sa porte et la cage B)."""
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    st = "R+1"
    wn = b.box("IfcWall", "Mur nord", (0, 2, 3), (30, 0.2, 3), storey=st)
    ws = b.box("IfcWall", "Mur sud", (0, -0.2, 3), (30, 0.2, 3), storey=st)
    g = {}
    g["couloir"] = _space(b, "Couloir", (0, 0, 3), (30, 2, 2.6), storey=st).GlobalId
    g["cage_a"] = _space(b, "Cage A", (0.5, -4.2, 3), (3, 4, 2.6), storey=st).GlobalId
    b.box("IfcStair", "Escalier A", (0.7, -4.0, 3), (1.2, 2.5, 3), storey=st)
    g["porte_a"] = b.door_in_wall(ws, "Porte A", (1.5, -0.2, 3), (0.9, 0.2, 2.1), storey=st).GlobalId
    g["cage_b"] = _space(b, "Cage B", (19, 2.2, 3), (2, 3, 2.6), storey=st).GlobalId
    b.box("IfcStair", "Escalier B", (19.2, 3.8, 3), (1.6, 1.2, 3), storey=st)
    g["porte_b"] = b.door_in_wall(wn, "Porte B", (19.5, 2, 3), (0.9, 0.2, 2.1), storey=st).GlobalId
    g["bureau"] = _space(b, "Bureau", (26, 2.2, 3), (4, 4, 2.6), storey=st).GlobalId
    g["porte_bureau"] = b.door_in_wall(wn, "Porte palière bureau", (28.5, 2, 3), (0.9, 0.2, 2.1), storey=st).GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "evac.ifc"), tmp_path / "c", key="k")
    widths = {d: ({"OverallWidth": 0.9}, {}, "") for d in (g["porte_a"], g["porte_b"])}
    widths[g["porte_bureau"]] = ({"OverallWidth": bureau_door_width}, {}, "")
    return idx, _records(idx, **widths), g


def test_f3_referentiel_dead_end_and_landing_door(tmp_path):
    from bimdata_mcp.quality.lot3 import auto_exits, run_fire_rules

    idx, recs, g = _evac_floor(tmp_path)
    exits, _ = auto_exits(idx, recs)
    assert {g["porte_a"], g["porte_b"]} <= set(exits)
    kw = dict(required_ratings=None, compartments=None, compartment_rating=None, door_rating=None, stairs=[],
              exit_up=1, stairs_up=1)
    # ERP : bureau en cul-de-sac (≈ 9 m) — F5 avec une limite de 5 m ; F3 sous 30 m : conforme
    res, notes, extra = run_fire_rules(idx, recs, exits=exits, max_distance_m=None, checks=["F3", "F5"],
                                       f3_referentiel="ERP", dead_end_max_m=5.0, **kw)
    f5 = {i.uuid: i for i in res.issues if i.req_id == "F5"}
    assert g["bureau"] in f5 and 7.0 < f5[g["bureau"]].measure["value"] < 11.0
    assert not [i for i in res.issues if i.req_id == "F3" and i.uuid == g["bureau"]]
    assert extra["evacuation"]["groupes_de_sorties"] == {"escalier": 2}
    # Personnalisé 12 m : le bureau dépasse ; mesure suivant l'axe, au moins aussi longue que le chemin tendu
    res, _, _ = run_fire_rules(idx, recs, exits=exits, max_distance_m=12.0, checks=["F3"], **kw)
    f3 = {i.uuid: i for i in res.issues if i.req_id == "F3"}
    m = f3[g["bureau"]].measure
    assert 13.0 < m["cheminement_m"] < 18.0 and m["cheminement_m"] >= m["tendu_m"] - 1e-6
    # Habitation 3A (10 m) : départ de la porte palière, pas du fond du bureau
    res, _, extra = run_fire_rules(idx, recs, exits=exits, max_distance_m=None, checks=["F3"],
                                   f3_referentiel="habitation_3A", **kw)
    assert extra["evacuation"]["portes_palieres"] >= 1
    f3 = {i.uuid: i for i in res.issues if i.req_id == "F3" and i.status == "sous_seuil"}
    assert g["bureau"] not in f3
    if g["porte_bureau"] in f3:
        assert 9.0 < f3[g["porte_bureau"]].measure["cheminement_m"] < 12.0
    # Sans référentiel ni distance : F3 non contrôlée, avec la liste des référentiels
    _, notes, _ = run_fire_rules(idx, recs, exits=exits, max_distance_m=None, checks=["F3"], **kw)
    assert any("choisir le référentiel" in n for n in notes)


def test_f6_eas_reachability_and_capacity(tmp_path):
    from bimdata_mcp.quality.lot3 import auto_exits, run_fire_rules

    idx, recs, g = _evac_floor(tmp_path, bureau_door_width=0.80)  # porte du bureau trop étroite pour un fauteuil
    exits, _ = auto_exits(idx, recs)
    kw = dict(required_ratings=None, compartments=None, compartment_rating=None, door_rating=None, stairs=[],
              exit_up=1, stairs_up=1, exits=exits, max_distance_m=None, f3_referentiel="ERP", checks=["F6"])
    res, notes, _ = run_fire_rules(idx, recs, eas=[g["cage_b"]], effectif={"R+1": 120}, **kw)
    f6 = {i.uuid: i for i in res.issues if i.req_id == "F6"}
    assert g["bureau"] in f6 and "porte" in f6[g["bureau"]].message  # n'atteint l'EAS que par une porte étroite
    assert g["couloir"] not in f6  # le couloir l'atteint sans obstacle
    assert not [i for i in res.issues if i.ifc_class == "IfcBuildingStorey"]  # 120 personnes : 4 places, 4,2 m² ≤ 6 m²
    res, _, _ = run_fire_rules(idx, recs, eas=[g["cage_b"]], effectif={"R+1": 300}, **kw)
    cap = [i for i in res.issues if i.ifc_class == "IfcBuildingStorey"]
    assert len(cap) == 1 and cap[0].measure["places"] == 7  # 2 + 5 tranches de 50
    _, notes, _ = run_fire_rules(idx, recs, **kw)
    assert any("F6 non contrôlée" in n for n in notes)
