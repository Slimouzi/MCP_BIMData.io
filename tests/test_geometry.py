"""Index géométrique et règles S1…S10, G1/G3/G8 sur un petit IFC construit (unités : mm)."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("ifcopenshell")

import ifcopenshell.api  # noqa: E402

from bimdata_mcp.geometry import mesh as M  # noqa: E402
from bimdata_mcp.geometry.index import get_or_build  # noqa: E402
from bimdata_mcp.model_view import Record  # noqa: E402
from bimdata_mcp.quality.rules import run_geometry_rules, run_structure_rules  # noqa: E402
from tests.ifc_factory import IfcBuilder  # noqa: E402

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _model(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    b.storey("Vide", 6.0)
    wall = b.box("IfcWall", "Mur", (0, 0, 0), (5, 0.2, 3), storey="RDC")
    g = {"wall": wall.GlobalId}
    g["p_ok"] = b.door_in_wall(wall, "P ok", (0.5, -0.05, 0), (0.9, 0.3, 2.1), storey="RDC",
                               operation="SINGLE_SWING_LEFT").GlobalId
    g["p_etage"] = b.door_in_wall(wall, "P autre étage", (2.0, -0.05, 0), (0.9, 0.3, 2.1), storey="R+1").GlobalId
    g["p_orph"] = b.box("IfcDoor", "P orpheline", (10, 0, 0), (0.9, 0.2, 2.1), storey="RDC",
                        OperationType="SINGLE_SWING_RIGHT").GlobalId
    g["biais"] = b.box("IfcWall", "Biais", (20, 0, 3), (4, 0.2, 3), storey="R+1", rot_deg=45).GlobalId
    g["haut"] = b.box("IfcWall", "Mur trop haut", (30, 0, 6.5), (2, 0.2, 2), storey="RDC").GlobalId
    g["proxy"] = b.no_geometry("IfcBuildingElementProxy", "Sans géométrie", storey="RDC").GlobalId
    g["col"] = b.box("IfcColumn", "Poteau hors étage", (40, 0, 0), (0.3, 0.3, 3)).GlobalId
    for key, name, size in (("wc", "WC", (0.8, 0.8, 2.2)), ("gt", "GT 01", (0.5, 0.5, 3.0)),
                            ("bureau", "Bureau", (4, 3, 2.7))):
        sp = b.box("IfcSpace", name, (50 + len(g), 0, 0), size)
        sp.LongName = name
        ifcopenshell.api.run("aggregate.assign_object", b.f, products=[sp], relating_object=b.storeys["RDC"])
        g[key] = sp.GlobalId
    g["gc"] = b.box("IfcRailing", "Garde-corps:600mm Exterior", (60, 0, 0), (2, 0.05, 1.0), storey="RDC").GlobalId
    b.storey("R+1", 9.0)  # nom en double (créé en dernier : rien n'y est rattaché)
    return b.write(tmp_path / "test.ifc"), g


@pytest.fixture()
def built(tmp_path):
    path, g = _model(tmp_path)
    idx, built_now = get_or_build(path, tmp_path / "cache", key="1/2/3")
    assert built_now
    return idx, g, path


def test_mesh_primitives():
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                  [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    assert M.volume(v + 1e6, f) == pytest.approx(1.0)  # précision conservée en coordonnées monde
    assert M.is_closed(v, f)
    assert not M.is_closed(v, f[:-1])
    assert M.horizontal_area_up(v, f) == pytest.approx(1.0)


def test_index_units_boxes_and_cache(built, tmp_path):
    idx, g, path = built
    m = idx.meta
    assert m["unit_scale"] == pytest.approx(0.001) and m["unit"] == "m"
    assert m["unit_check"]["status"] == "ok"
    wall = idx.elements[g["wall"]]
    assert wall["aabb"] == pytest.approx([0, 0, 0, 5, 0.2, 3], abs=1e-6)  # mm → m
    assert wall["is_closed"]
    biais = idx.elements[g["biais"]]
    assert biais["obb"]["half"] == pytest.approx([2.0, 0.1, 1.5], abs=1e-4)  # OBB exacte malgré l'AABB de biais
    assert biais["volume"] == pytest.approx(2.4, rel=1e-4)
    assert abs(biais["obb"]["axes"][0][0]) == pytest.approx(np.cos(np.radians(45)), abs=1e-4)
    assert idx.elements[g["proxy"]]["has_geometry"] is False
    assert idx.elements[g["wc"]]["floor_area"] == pytest.approx(0.64, rel=1e-3)
    assert set(idx.query_box([19, -1, 3, 24, 4, 6])) == {g["biais"]}
    v, fc = idx.mesh(g["biais"])
    assert len(v) >= 8 and len(fc) >= 12
    idx2, built_now = get_or_build(path, tmp_path / "cache", key="1/2/3")
    assert not built_now and idx2.meta["ifc_sha1"] == m["ifc_sha1"]


def test_structure_rules(built):
    idx, g, _ = built
    res, notes = run_structure_rules(idx, {}, displayed_storeys={})
    by = {}
    for i in res.issues:
        by.setdefault(i.req_id, []).append(i)
    assert {i.uuid for i in by["S2"]} == {g["col"]}
    s3 = [i.message for i in by["S3"]]
    assert sum("en double" in m for m in s3) == 2 and any("« Vide » vide" in m for m in s3)
    assert {i.uuid for i in by["S4"]} == {g["haut"], g["p_etage"]}  # la porte « R+1 » est posée à z = 0
    assert {i.uuid for i in by["S5"]} == {g["p_orph"]}
    assert [i.uuid for i in by["S6"]] == [g["p_etage"]]
    assert {e["role"] for e in by["S6"][0].elements} == {"porte/fenêtre", "hôte"}
    assert {i.uuid for i in by["S7"]} == {g["p_etage"]}  # les deux autres portes ont un OperationType
    s7 = by["S7"][0]
    assert s7.status == "absent" and "sens d'ouverture absent sur 1 porte(s)" in s7.message
    idx.ifc["doors"][g["p_etage"]]["occurrence"] = "NOTDEFINED"  # attribut présent mais non renseigné
    s7b = next(i for i in run_structure_rules(idx, {}, checks=["S7"])[0].issues if i.uuid == g["p_etage"])
    assert s7b.status == "vide" and "attribut à NOTDEFINED (OperationType de l'occurrence = NOTDEFINED)" in s7b.message
    assert {i.uuid for i in by["S10"]} == {g["proxy"]}
    assert "S9" in by  # ni IfcMapConversion ni latitude/longitude
    assert all(i.method in ("Données", "Bbox") and i.rule_id == i.req_id for i in res.issues)
    # Libellé affiché différent du nom IFC (cas « Terrace » / « Terrece »), étage non déclaré vide.
    rdc = next(s for s in idx.ifc["storeys"] if s["name"] == "RDC")
    res2, _ = run_structure_rules(idx, {}, checks=["S3"], displayed_storeys={rdc["guid"]: "Rez-de-chaussée"})
    msgs = [i.message for i in res2.issues if i.uuid == rdc["guid"]]
    assert len(msgs) == 1 and "libellé affiché « Rez-de-chaussée »" in msgs[0]


def test_geometry_rules(built):
    idx, g, _ = built
    recs = {
        g["wc"]: Record(uuid=g["wc"], ifc_class="IfcSpace", attrs={"Name": "1", "LongName": "WC"}),
        g["gt"]: Record(uuid=g["gt"], ifc_class="IfcSpace", attrs={"Name": "2", "LongName": "GT 01"}),
        g["bureau"]: Record(uuid=g["bureau"], ifc_class="IfcSpace", attrs={"Name": "3", "LongName": "Bureau"},
                            props={"BaseQuantities.NetFloorArea": 12.0, "BaseQuantities.Height": 2.7}),
        g["gc"]: Record(uuid=g["gc"], ifc_class="IfcRailing", attrs={"Name": "gc", "ObjectType": "Garde-corps:600mm"},
                        props={"Pset_RailingCommon.Height": 1.0}),
    }
    res, _ = run_geometry_rules(idx, recs)
    got = sorted((i.req_id, i.uuid, i.status) for i in res.issues)
    assert got == sorted([
        ("G1", g["wc"], "sous_seuil"), ("G1", g["wc"], "sous_seuil"),  # surface (maillage) + hauteur (AABB)
        ("G1", g["gt"], "exclu"),  # gaine : signalée pour information
        ("G8", g["gc"], "incoherent"),  # « 600mm » pour 1,00 m
    ])
    gt = next(i for i in res.issues if i.uuid == g["gt"])
    assert gt.severity == "info"
    res2, _ = run_geometry_rules(idx, recs, thresholds={"railing_min_height_m": 1.1})
    assert ("G8", g["gc"], "sous_seuil") in {(i.req_id, i.uuid, i.status) for i in res2.issues}


def test_structure_check_through_mcp(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp import service_geometry

    path, g = _model(tmp_path)
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    st = mcp_call("build_geometry_index")
    assert st["elements_physiques"] >= 8 and st["sans_geometrie"] == 1 and st["unite"] == "m"
    info = mcp_call("geometry_info", uuids=[g["biais"], "inconnu"])
    assert info["elements"][0]["dimensions_m"][2] == pytest.approx(3.0) and info["introuvables"] == ["inconnu"]

    out = mcp_call("run_structure_check")
    assert out["livrable"]["download_url"] and out["summary"]["non_conformities"] >= 8
    import openpyxl

    from tests.test_workflow import _path

    assert out["livrable"]["fichier"].endswith(".pdf")
    sheets = openpyxl.load_workbook(_path(out["livrable"]["annexe_excel"])).sheetnames
    assert sheets[0] == "Synthèse" and any(n.startswith("S6 ") for n in sheets) and "Lisez-moi" in sheets
    issues = mcp_call("get_check_issues", check_id=out["check_id"], req_id="S6", limit=10)["issues"]
    assert [i["uuid"] for i in issues] == [g["p_etage"]] and issues[0]["method"] == "Données"
    ask = mcp_call("prepare_smart_view", title="Structure", check_id=out["check_id"])
    assert ask["etape"] == "choix_selection_smart_view"  # plusieurs règles : sélection proposée d'abord
    plan = mcp_call("prepare_smart_view", title="Structure", check_id=out["check_id"], perimetre_valide=True)
    assert plan["n_items"] >= 1  # anomalies sans élément (S9 maquette) ignorées par la Smart View

    full = mcp_call("list_audits")
    menu = full["audits"]
    assert [a["id"] for a in menu][:3] == ["general", "nommage", "c2_clash"] and len(menu) == 11
    assert [g["famille"] for g in full["familles"]] == ["Audit général", "Contrôle du nommage", "Conflits (clash)",
                                                         "Réglementaire", "Audit spécifique selon votre CCBIM",
                                                         "Contrôles avancés"]
    msg = full["message_utilisateur"]
    assert msg.startswith("Maquette chargée : « ") and "Par lequel souhaitez-vous commencer ?" in msg
    assert "ERP 2017, ou BHC 2015" in msg and "habitation 3A" in msg and "cahier des charges BIM (CCBIM) ?" in msg
    assert "c5_pmr" not in msg and "partial" not in msg
    st_by = {a["id"]: a["status"] for a in menu}
    assert st_by["c1_structure"] == "pret" and st_by["c2_clash"] in ("pret", "partiel") and st_by["c5_pmr"] == "partiel"
    assert st_by["c3_informations"] == "bloque"
    # un audit à la fois, choisi : « tous » refusé ; d'abord les hypothèses (rien n'est calculé), puis le lancement
    with pytest.raises(Exception, match="pas tous ensemble"):
        mcp_call("run_audit", audit="tous")
    with pytest.raises(Exception, match="Audit inconnu"):
        mcp_call("run_audit", audit="c9")
    with pytest.raises(Exception, match="Déposez-le"):
        mcp_call("run_audit", audit="c3_informations")  # bloqué : raison et proposition de dépôt
    hyp = mcp_call("run_audit", audit="c1_structure")
    assert hyp["etape"] == "validation_hypotheses" and hyp["jeton_hypotheses"] and hyp["hypotheses"]
    wrong = mcp_call("run_audit", audit="c1_structure", hypotheses_validees="faux")
    assert wrong["etape"] == "validation_hypotheses" and wrong["jeton_refuse"]
    run = mcp_call("run_audit", audit="c1_structure", hypotheses_validees=hyp["jeton_hypotheses"])
    assert run["etape"] == "termine" and run["audit"]["id"] == "c1_structure"
    assert run["livrable"]["fichier"].endswith(".pdf") and run["livrable"]["download_url"]
    for aid in ("general", "nommage", "c4_geometrie"):
        tok = mcp_call("run_audit", audit=aid)["jeton_hypotheses"]
        done = mcp_call("run_audit", audit=aid, hypotheses_validees=tok)
        assert done["etape"] == "termine"
        # chaque audit (dont le contrôle de nommage) livre un PDF ET un classeur Excel
        assert [(x["format"], x["fichier"].rsplit(".", 1)[1]) for x in done["livrables"]] == [("PDF", "pdf"),
                                                                                              ("Excel", "xlsx")]


def test_storey_altitudes_not_increasing(tmp_path):
    b = IfcBuilder()
    b.storey("A", 0.0)
    s = b.storey("B", 3.0)
    s.ObjectPlacement.RelativePlacement.Location.Coordinates = (0.0, 0.0, -3000.0)  # placé sous « A »
    b.box("IfcWall", "m", (0, 0, 0), (1, 0.2, 1), storey="A")
    b.box("IfcWall", "m", (0, 0, -3), (1, 0.2, 1), storey="B")
    idx, _ = get_or_build(b.write(tmp_path / "alt.ifc"), tmp_path / "c", key="k")
    res, _ = run_structure_rules(idx, {}, checks=["S3"])
    msgs = [i.message for i in res.issues]
    assert any(m.startswith("Altitudes non croissantes : « B »") for m in msgs)
    assert any("Altitude déclarée 3.000 m ≠ altitude du placement -3.000 m" in m for m in msgs)


def test_ifc_download_gunzips_and_caches(tmp_path, monkeypatch):
    import gzip

    import requests

    from bimdata_mcp import config
    from bimdata_mcp.geometry import source
    from bimdata_mcp.target import BIMDataTarget

    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    body = gzip.compress(b"ISO-10303-21;\nHEADER;\nENDSEC;\n")
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield body

    def fake_get(url, **kw):
        calls.append((url, kw.get("headers")))
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)

    class Client:
        timeout = 5

        def get_model(self):
            return {"name": "m.ifc", "modified_date": "2026-11-17T10:00:00Z",
                    "document": {"file": "https://storage.test/m.ifc?sig=1"}}

    t = BIMDataTarget("1", "2", "3")
    path, info = source.fetch_model_ifc(Client(), t)
    assert path.read_bytes().startswith(b"ISO-10303-21") and not info["from_cache"]
    assert calls == [("https://storage.test/m.ifc?sig=1", None)]  # URL signée : pas d'en-tête d'authentification
    _, info2 = source.fetch_model_ifc(Client(), t)
    assert info2["from_cache"] and len(calls) == 1
    config.get_settings.cache_clear()


def test_s4_seated_storey(tmp_path):
    """S4 raisonnée sur la base de l'élément : étage d'assise = le plus haut sous la base + 0,35 m."""
    b = IfcBuilder()
    b.storey("SS", -3.0)
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    b.storey("R+1bis", 3.5)  # demi-niveau à 0,50 m : ambiguïté
    b.storey("R+2", 6.0)
    g = {
        # conformes (anciens faux positifs) : fondations, structure sous le revêtement, gaine multi-étages, mur haut
        "mur_ss": b.box("IfcWall", "Mur sous-sol", (0, 0, -3.55), (4, 0.2, 3.55), storey="SS"),
        "dalle_ok": b.box("IfcSlab", "Dalle structure", (10, 0, -0.55), (4, 4, 0.25), storey="RDC"),  # dessus -0,30
        "mur_ext": b.box("IfcWall", "Mur ext", (20, 0, -0.30), (4, 0.2, 3.30), storey="RDC"),
        "gaine": b.box("IfcCovering", "Isolation gaine", (30, 0, 0), (0.3, 0.3, 9.0), storey="RDC"),
        "haut": b.box("IfcWall", "Mur de halle", (60, 0, 0), (2, 0.2, 18.0), storey="RDC"),  # cellule de 18 m
        # vrais problèmes
        "compteur": b.box("IfcBuildingElementProxy", "Compteur", (40, 0, 1.0), (0.4, 0.2, 0.3), storey="R+2"),
        "dalle_ko": b.box("IfcSlab", "Dalle trop basse", (50, 0, -0.70), (4, 4, 0.25), storey="RDC"),  # dessus -0,45
        "sous_ss": b.box("IfcWall", "Mur sous le sous-sol", (70, 0, -4.0), (2, 0.2, 1.0), storey="RDC"),
        # ambigu : rattaché à R+1, posé à +0,40 m, R+1bis 0,50 m au-dessus
        "ambigu": b.box("IfcWall", "Cloison", (80, 0, 3.4), (2, 0.1, 2.0), storey="R+1"),
        # portés : gaine sous plafond et poutre de toiture de halle (conformes), poutre qui descend (anomalie)
        "gaine_haute": b.box("IfcDuctSegment", "Gaine", (90, 0, 2.6), (3, 0.3, 0.2), storey="RDC"),
        "poutre_halle": b.box("IfcBeam", "Poutre de toiture", (100, 0, 17.5), (8, 0.3, 0.5), storey="RDC"),
        "poutre_basse": b.box("IfcBeam", "Poutre", (110, 0, 2.0), (4, 0.3, 0.5), storey="R+1"),  # 1 m sous R+1
        # dalle posée sur son étage, dessus à +0,80 m (estrade) : conforme
        "estrade": b.box("IfcSlab", "Estrade", (120, 0, 0.0), (3, 3, 0.8), storey="RDC"),
        # poutre de 65 cm rattachée au plancher qu'elle porte, pendue dessous : jugée sur son dessus, conforme
        "poutre_pendue": b.box("IfcBeam", "Poutre sous plancher", (130, 0, 2.35), (4, 0.3, 0.65), storey="R+1"),
    }
    # réseaux du plafond du sous-sol rattachés au RDC : une seule ligne mineure de synthèse
    pipes = [b.box("IfcPipeSegment", f"Tuyau EG {k}", (140 + 2 * k, 0, -0.8), (1.5, 0.1, 0.1), storey="RDC").GlobalId
             for k in range(3)]
    g = {k: v.GlobalId for k, v in g.items()}
    idx, _ = get_or_build(b.write(tmp_path / "s4.ifc"), tmp_path / "c", key="k")
    res, _ = run_structure_rules(idx, {}, checks=["S4"])
    rdc = b.storeys["RDC"].GlobalId
    net = [i for i in res.issues if i.uuid == rdc]
    assert len(net) == 1 and net[0].severity == "mineur" and "3 élément(s) de réseau" in net[0].message
    assert {e["uuid"] for e in net[0].elements} == set(pipes) and "« SS »" in net[0].message
    got = {i.uuid: i for i in res.issues if i.uuid != rdc}
    inv = {v: k for k, v in g.items()}
    assert {inv[u] for u in got} == {"compteur", "dalle_ko", "sous_ss", "ambigu", "poutre_basse"}
    assert "Élément porté" in got[g["poutre_basse"]].message and "« RDC »" in got[g["poutre_basse"]].message
    assert "posé sur « RDC »" in got[g["compteur"]].message  # étage réel nommé
    assert "au niveau de « SS »" in got[g["dalle_ko"]].message
    assert "sous l'étage le plus bas" in got[g["sous_ss"]].message
    assert got[g["ambigu"]].status == "exclu" and got[g["ambigu"]].severity == "info"
    assert got[g["ambigu"]].message.startswith("Non concluant")
    assert res.per_requirement[0]["non_conformes"] == 5  # l'élément ambigu ne compte pas en anomalie
    res2, _ = run_structure_rules(idx, {}, checks=["S4"], s4_thresholds={"slab_top_below_m": 0.5,
                                                                         "carried_tolerance_m": 0.6})
    assert g["dalle_ko"] not in {i.uuid for i in res2.issues}
    assert g["poutre_basse"] not in {i.uuid for i in res2.issues}  # dalle épaisse : tolérance augmentée

def test_aggregate_geometry_from_parts(tmp_path):
    """Mur sans représentation propre, fait de deux couches IfcBuildingElementPart : géométrie réunie."""
    from bimdata_mcp.geometry.clash import ClashOptions, find_clashes

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    wall = ifcopenshell.api.run("root.create_entity", b.f, ifc_class="IfcWall", name="Mur en couches")
    ifcopenshell.api.run("spatial.assign_container", b.f, products=[wall], relating_structure=b.storeys["RDC"])
    p1 = b.box("IfcBuildingElementPart", "Couche 1", (0, 0, 0), (4, 0.1, 3))
    p2 = b.box("IfcBuildingElementPart", "Couche 2", (0, 0.1, 0), (4, 0.1, 3))
    ifcopenshell.api.run("aggregate.assign_object", b.f, products=[p1, p2], relating_object=wall)
    col = b.box("IfcColumn", "Poteau", (1, 0.05, 0), (0.3, 0.3, 3), storey="RDC")
    idx, _ = get_or_build(b.write(tmp_path / "parts.ifc"), tmp_path / "c", key="k")
    w = idx.elements[wall.GlobalId]
    assert w["has_geometry"] and w["from_parts"] and w["aabb"] == pytest.approx([0, 0, 0, 4, 0.2, 3], abs=1e-6)
    assert w["volume"] == pytest.approx(2.4, rel=1e-6) and w["obb"]["half"][1] == pytest.approx(0.1, abs=1e-6)
    res, _ = run_structure_rules(idx, {}, checks=["S10"])
    assert not res.issues  # l'agrégat a une géométrie (celle de ses parties)
    ids = list(idx.elements)
    cl, _ = find_clashes(idx, ids, ids, ClashOptions())
    pairs = {frozenset((c.a, c.b)) for c in cl}
    assert frozenset((p1.GlobalId, col.GlobalId)) in pairs and frozenset((p2.GlobalId, col.GlobalId)) in pairs
    assert all(wall.GlobalId not in pr for pr in pairs)  # le clash teste les parties, pas l'agrégat


def test_duplicate_globalids_are_indexed_separately(tmp_path):
    """Deux éléments avec le même GlobalId : deux entrées d'index (#id), S8 bloquant, clash juste pour chacun."""
    from bimdata_mcp.geometry.clash import ClashOptions, find_clashes
    from bimdata_mcp.publication import bcf_topic

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    w1 = b.box("IfcWall", "Mur nord", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    w2 = b.box("IfcWall", "Mur sud", (0, 10, 0), (4, 0.2, 3), storey="RDC")
    w2.GlobalId = w1.GlobalId  # export défectueux
    col = b.box("IfcColumn", "Poteau", (1, 10.05, 0), (0.3, 0.3, 3), storey="RDC")  # ne touche que le mur sud
    idx, _ = get_or_build(b.write(tmp_path / "dup.ifc"), tmp_path / "c", key="k")
    keys = idx.keys_for(w1.GlobalId)
    assert sorted(keys) == sorted([f"{w1.GlobalId}#{w1.id()}", f"{w1.GlobalId}#{w2.id()}"])
    south = f"{w1.GlobalId}#{w2.id()}"
    assert idx.elements[south]["name"] == "Mur sud" and idx.elements[south]["aabb"][1] == pytest.approx(10.0)
    res, _ = run_structure_rules(idx, {}, checks=["S8"])
    assert len(res.issues) == 1 and res.issues[0].severity == "bloquant"
    assert {e["step_id"] for e in res.issues[0].elements} == {w1.id(), w2.id()}
    ids = list(idx.elements)
    cl, _ = find_clashes(idx, ids, ids, ClashOptions())
    assert [frozenset((c.a, c.b)) for c in cl] == [frozenset((south, col.GlobalId))]  # pas le mur nord
    topic = bcf_topic([south, col.GlobalId], title="t")
    assert {c["ifc_guid"] for c in topic["viewpoints"][0]["components"]["selection"]} == {w1.GlobalId, col.GlobalId}


def test_isolated_build_falls_back_to_stable_library(tmp_path, monkeypatch):
    """Calcul en processus séparé : la bibliothèque demandée échoue (ici, nom invalide) → reprise avec OpenCascade."""
    from bimdata_mcp.geometry import index as gi

    path, g = _model(tmp_path)
    monkeypatch.setenv("MCP_GEOMETRY_SUBPROCESS", "1")
    monkeypatch.setenv("MCP_GEOMETRY_LIBRARY", "bibliotheque-inexistante")
    idx = gi.build_isolated(path, tmp_path / "iso", key="k")
    assert idx.meta["geometry_library"] == "opencascade" and "a échoué" in idx.meta["fallback"]
    assert idx.elements[g["wall"]]["aabb"] == pytest.approx([0, 0, 0, 5, 0.2, 3], abs=1e-6)


def test_memory_budget_threads_refusal_and_watchdog(tmp_path, monkeypatch):
    from bimdata_mcp.geometry import index as gi

    occ = "opencascade"
    assert gi.plan_threads(28, 8, limit_mb=4000, library=occ) == 8
    assert gi.plan_threads(28, 8, limit_mb=1300, library=occ) == 4  # 336 + 600 + 3 × 110 = 1 266 ≤ 1 300 < 1 376
    assert gi.plan_threads(28, 8, limit_mb=1000, library=occ) == 1
    assert gi.plan_threads(28, 8, limit_mb=1000, library="hybrid-cgal-simple-opencascade") == 8  # 336 + 400 + 140
    with pytest.raises(ValueError, match="au-delà de la mémoire allouée"):
        gi.plan_threads(800, 8, limit_mb=8192)  # 800 Mo : ≈ 10 Go estimés pour 8 Go alloués
    path, _ = _model(tmp_path)
    monkeypatch.setenv("MCP_GEOMETRY_MAX_MEMORY_MB", "100")
    with pytest.raises(ValueError, match="mémoire allouée"):  # refus avant de lancer le calcul
        gi.build_isolated(path, tmp_path / "m1", key="k")
    # Surveillance : estimation à zéro, 50 Mo alloués → le processus de calcul est arrêté avec un message clair.
    monkeypatch.setattr(gi, "MEM_PER_FILE_MB", 0.0)
    monkeypatch.setattr(gi, "MEM_ITER_BASE_MB", 0.0)
    monkeypatch.setattr(gi, "MEM_ITER_BASE_MB_HYBRID", 0.0)
    monkeypatch.setattr(gi, "MEM_PER_THREAD_MB_HYBRID", 0.0)
    monkeypatch.setattr(gi, "MEM_PER_THREAD_MB", 0.0)
    monkeypatch.setenv("MCP_GEOMETRY_MAX_MEMORY_MB", "50")
    monkeypatch.setenv("MCP_GEOMETRY_SUBPROCESS", "1")
    with pytest.raises(ValueError, match="Calcul de l'index géométrique arrêté"):
        gi.build_isolated(path, tmp_path / "m2", key="k")
    assert not (tmp_path / "m2" / "meta.json").exists()  # pas d'index partiel


def test_s11_space_outside_building_and_s4_ramp_hint(tmp_path):
    """S11 : un local posé loin de tout mur ou dalle est hors de l'emprise. S4 : une suite d'éléments portés
    rattachés trop haut, dont les dessus montent d'un pas régulier, reçoit l'indice « rampe probable »."""
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+2", 8.0)
    b.box("IfcWall", "Mur", (0, 0, 0), (6, 0.2, 3), storey="RDC")
    b.box("IfcSlab", "Dalle", (0, 0, -0.2), (6, 5, 0.2), storey="RDC")
    inside = b.box("IfcSpace", "Séjour", (0, 0.2, 0), (6, 4.8, 2.6))
    lost = b.box("IfcSpace", "001", (60, 60, 0), (3, 4, 2.6))
    for sp in (inside, lost):
        ifcopenshell.api.run("aggregate.assign_object", b.f, products=[sp], relating_object=b.storeys["RDC"])
    tt = [b.box("IfcBeam", "Précoulé-Té double:2400 x 600mm", (20 + 2.4 * k, 0, 1.0 + 0.185 * k), (2.4, 8, 0.6),
                storey="R+2").GlobalId for k in range(6)]
    b.box("IfcBeam", "Poutre isolée:30x60", (40, 0, 2.0), (4, 0.3, 0.6), storey="R+2")
    idx, _ = get_or_build(b.write(tmp_path / "s11.ifc"), tmp_path / "c", key="k")
    res, _ = run_structure_rules(idx, {}, checks=["S11", "S4"])
    s11 = [i for i in res.issues if i.req_id == "S11"]
    assert [i.uuid for i in s11] == [lost.GlobalId] and s11[0].measure["value"] > 50
    s4 = {i.uuid: i for i in res.issues if i.req_id == "S4"}
    assert set(tt) <= set(s4)
    assert all("rampe probable" in s4[g].message and "pente ≈ 7.7 %" in s4[g].message for g in tt)
    assert not any("rampe" in i.message for g, i in s4.items() if g not in tt)


def test_general_audit_rules_and_inventory(fake_env, tmp_path):
    """Audit général : inventaire et règles de données de base (proxy, niveau, nom), rapport PDF."""
    from bimdata_mcp.model_view import Record
    from bimdata_mcp.quality.engine import run_check
    from bimdata_mcp.service import general_rules

    recs = [Record(uuid="a", ifc_class="IfcBuildingElementProxy", attrs={"Name": "Objet"}, storey="RDC"),
            Record(uuid="b", ifc_class="IfcWall", attrs={"Name": "Mur"}, storey=""),
            Record(uuid="c", ifc_class="IfcWall", attrs={"Name": ""}, storey="RDC")]
    res = run_check(recs, general_rules())
    bad = {(i.req_id, i.uuid) for i in res.issues}
    assert ("M04", "a") in bad and ("M04", "b") not in bad  # objet générique signalé, mur non
    assert ("M03", "b") in bad and ("M05", "c") in bad
