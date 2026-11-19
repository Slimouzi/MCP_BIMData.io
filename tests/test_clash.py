"""Détection de conflits (lot 2) et règles G2, G4 à G7 sur des IFC construits (unités : mm)."""

from __future__ import annotations

import pytest

pytest.importorskip("ifcopenshell")

from bimdata_mcp.geometry.clash import ClashOptions, assign_groups, find_clashes  # noqa: E402
from bimdata_mcp.geometry.index import get_or_build  # noqa: E402
from bimdata_mcp.model_view import Record  # noqa: E402
from bimdata_mcp.quality.rules import run_geometry_rules  # noqa: E402
from tests.ifc_factory import IfcBuilder  # noqa: E402

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _clash_model(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    g = {}

    def add(key, *a, **k):
        g[key] = b.box(*a, **k).GlobalId

    add("w1", "IfcWall", "A", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    add("w2", "IfcWall", "B", (3, 0, 0), (4, 0.2, 3), storey="RDC")  # chevauchement de 1 m
    add("d1", "IfcColumn", "D1", (10, 0, 0), (0.3, 0.3, 3), storey="RDC")
    add("d2", "IfcColumn", "D2", (10, 0, 0), (0.3, 0.3, 3), storey="RDC")  # doublon exact
    add("big", "IfcSlab", "Bloc", (20, 0, 0), (2, 2, 1), storey="RDC")
    add("small", "IfcBuildingElementProxy", "Inclus", (20.5, 0.5, 0.2), (0.5, 0.5, 0.5), storey="RDC")
    add("wp", "IfcWall", "Mur tuyau", (30, 0, 0), (4, 0.2, 3), storey="RDC")
    add("pipe", "IfcPipeSegment", "Tuyau traversant", (31, -1, 1), (0.1, 2.2, 0.1), storey="RDC")  # 90°, traverse
    add("pipe_in", "IfcPipeSegment", "Tuyau arrêté", (32, -1, 1), (0.1, 1.1, 0.1), storey="RDC")  # s'arrête dedans
    add("t1", "IfcWall", "T1", (40, 0, 0), (2, 0.2, 3), storey="RDC")
    add("t4", "IfcWall", "T4mm", (40, 0.196, 0), (2, 0.2, 3), storey="RDC")  # 4 mm < tolérance 5 mm
    add("u1", "IfcWall", "U1", (50, 0, 0), (2, 0.2, 3), storey="RDC")
    add("u6", "IfcWall", "U6mm", (50, 0.194, 0), (2, 0.2, 3), storey="RDC")  # 6 mm > tolérance
    add("bias", "IfcWall", "Biais", (60, 0, 0), (4, 0.2, 3), rot_deg=45, storey="RDC")
    add("near", "IfcColumn", "Proche", (60.0, 2.4, 0), (0.3, 0.3, 3), storey="RDC")  # AABB touchée, maillage non
    add("ceil", "IfcCovering", "Faux plafond", (70, 0, 2.6), (3, 3, 0.05), storey="RDC", PredefinedType="CEILING")
    add("light", "IfcLightFixture", "Luminaire", (71, 1, 2.55), (0.6, 0.6, 0.1), storey="RDC")
    add("wa", "IfcWall", "Mur bouche", (80, 0, 0), (3, 0.2, 3), storey="RDC")
    add("air", "IfcAirTerminal", "Bouche", (81, -0.05, 2.0), (0.3, 0.08, 0.3), storey="RDC")  # 3 cm dans le mur
    return b.write(tmp_path / "clash.ifc"), g


@pytest.fixture()
def clash_idx(tmp_path):
    path, g = _clash_model(tmp_path)
    idx, _ = get_or_build(path, tmp_path / "c", key="k")
    return idx, g


def _pairs(idx, g, clashes):
    inv = {v: k for k, v in g.items()}
    return {frozenset((inv[c.a], inv[c.b])): c for c in clashes}


def test_clash_types_tolerances_and_exceptions(clash_idx):
    idx, g = clash_idx
    ids = list(g.values())
    clashes, stats = find_clashes(idx, ids, ids, ClashOptions())
    got = _pairs(idx, g, clashes)
    assert set(got) == {frozenset(p) for p in (("w1", "w2"), ("d1", "d2"), ("big", "small"), ("wp", "pipe_in"),
                                               ("u1", "u6"))}
    assert got[frozenset(("d1", "d2"))].clash_type == "duplicate"
    inc = got[frozenset(("big", "small"))]
    assert inc.clash_type == "inside" and inc.inside_of == g["big"] and inc.severity == "bloquant"
    ov = got[frozenset(("w1", "w2"))]
    assert ov.clash_type == "overlap" and ov.depth_h == pytest.approx(0.2) and ov.depth_v == pytest.approx(3.0)
    assert ov.volume == pytest.approx(0.6, rel=1e-3) and ov.orientation == "parallele" and ov.severity == "majeur"
    six = got[frozenset(("u1", "u6"))]
    assert six.depth_h == pytest.approx(0.006, abs=1e-6) and six.severity == "mineur"  # petit (< 25 mm)
    assert stats["sous_tolerance"] >= 1  # 4 mm
    assert stats["exceptions"] == {"traversee_mur": 1, "luminaire_faux_plafond": 1, "bouche_affleurante": 1}
    # Biais : AABB qui touche le poteau, maillage qui ne le touche pas → aucune anomalie.
    assert frozenset(("bias", "near")) not in got
    # Clash dur sans exception : tuyau traversant, luminaire et bouche redeviennent des conflits ; 4 mm aussi.
    hard, _ = find_clashes(idx, ids, ids, ClashOptions(tol_h=0.0, tol_v=0.0, exceptions={}))
    hard_pairs = set(_pairs(idx, g, hard))
    assert {frozenset(("wp", "pipe")), frozenset(("ceil", "light")), frozenset(("wa", "air")),
            frozenset(("t1", "t4"))} <= hard_pairs


def test_volume_tolerance_and_severity_rules(clash_idx):
    idx, g = clash_idx
    ids = list(g.values())
    cl, _ = find_clashes(idx, ids, ids, ClashOptions(tol_volume=0.05))
    assert frozenset(("u1", "u6")) not in _pairs(idx, g, cl)  # 0,036 m³ < 0,05
    assert frozenset(("w1", "w2")) in _pairs(idx, g, cl)
    rules = [{"when": {"orientation": "parallele"}, "severity": "info"},
             {"when": {"class_a": "IfcWall", "size": ["moyen", "grand"]}, "severity": "bloquant"}]
    cl, _ = find_clashes(idx, ids, ids, ClashOptions(severity_rules=rules))
    assert _pairs(idx, g, cl)[frozenset(("w1", "w2"))].severity == "bloquant"  # la plus grave l'emporte
    assert _pairs(idx, g, cl)[frozenset(("u1", "u6"))].severity == "info"


def test_groups(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    pairs = []
    for i, st in enumerate(("RDC", "R+1")):
        z = 3.0 * i
        w = b.box("IfcWall", "Mur", (0, 0, z), (2, 0.2, 2.5), storey=st)
        p = b.box("IfcColumn", "Poteau", (0.5, 0.05, z), (0.1, 0.1, 2.5), storey=st)  # même intersection, 2 étages
        pairs.append((w, p))
    w = b.box("IfcWall", "Mur seul", (10, 0, 0), (2, 0.2, 2.5), storey="RDC")
    p = b.box("IfcBeam", "Poutre R+1", (10.5, -0.5, 1.0), (0.3, 1.2, 0.3), storey="R+1")
    idx, _ = get_or_build(b.write(tmp_path / "g.ifc"), tmp_path / "c", key="k")
    ids = list(idx.elements)
    cl, _ = find_clashes(idx, ids, ids, ClashOptions())
    assign_groups(idx, cl)
    by = {frozenset((c.a, c.b)): c.group for c in cl}
    for w_, p_ in pairs:
        assert by[frozenset((w_.GlobalId, p_.GlobalId))] == "similaires_plusieurs_etages"
    assert by[frozenset((w.GlobalId, p.GlobalId))] == "etages_differents"


def test_geometry_rules_lot2(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    g = {}
    g["slab"] = b.box("IfcSlab", "Dalle", (0, 0, -0.2), (10, 10, 0.2), storey="RDC").GlobalId
    g["wa"] = b.box("IfcWall", "Mur A", (1, 1, 0), (4, 0.2, 3), storey="RDC").GlobalId
    g["wb"] = b.box("IfcWall", "Mur B", (4.8, 1.2, 0), (0.2, 3, 3), storey="RDC").GlobalId  # raccord en L (bout à bout)
    g["wfree"] = b.box("IfcWall", "Mur flottant", (1, 6, 0.1), (3, 0.2, 2.9), storey="RDC").GlobalId  # 10 cm au-dessus
    g["dup1"] = b.box("IfcBuildingElementProxy", "Meuble", (7, 7, 0), (1, 1, 1), storey="RDC").GlobalId
    g["dup2"] = b.box("IfcBuildingElementProxy", "Meuble", (7, 7, 0), (1, 1, 1), storey="RDC").GlobalId
    g["sp1"] = b.box("IfcSpace", "1", (20, 0, 0), (4, 4, 2.7)).GlobalId
    g["sp2"] = b.box("IfcSpace", "2", (23, 0, 0), (4, 4, 2.7)).GlobalId  # 1 m × 4 m × 2,7 m en commun
    g["sp3"] = b.box("IfcSpace", "3", (27, 0, 0), (4, 4, 2.7)).GlobalId  # simplement contigu à sp2
    g["stair"] = b.box("IfcStair", "Escalier", (40, 0, 0), (1, 3, 1.5), storey="RDC").GlobalId
    g["beam"] = b.box("IfcBeam", "Poutre basse", (39.5, 1, 3.2), (2, 0.3, 0.3), storey="RDC").GlobalId  # 1,70 m
    idx, _ = get_or_build(b.write(tmp_path / "g2.ifc"), tmp_path / "c", key="k")
    recs = {u: Record(uuid=u, ifc_class=idx.elements[u]["ifc_class"], attrs={"Name": idx.elements[u]["name"]})
            for u in idx.elements}
    recs[g["stair"]].props = {"Pset_StairCommon.RiserHeight": 0.20, "Pset_StairCommon.TreadLength": 0.25}
    res, notes = run_geometry_rules(idx, recs, checks=["G2", "G4", "G5", "G6", "G7"])
    by: dict[str, list] = {}
    for i in res.issues:
        by.setdefault(i.req_id, []).append(i)
    inv = {v: k for k, v in g.items()}
    assert [(inv[i.uuid], inv[i.elements[1]["uuid"]]) for i in by["G2"]] in ([("sp1", "sp2")], [("sp2", "sp1")])
    assert by["G2"][0].measure["value"] == pytest.approx(10.8, rel=1e-3)
    assert {inv[i.uuid] for i in by["G4"]} <= {"dup1", "dup2"} and len(by["G4"]) == 1
    g5 = {(inv[i.uuid], i.message.split("(")[1].split(")")[0]) for i in by["G5"]}
    assert ("wa", "fin") not in g5 and ("wb", "début") not in g5  # raccord en L reconnu
    assert {u for u, _ in g5} >= {"wfree"}
    assert [inv[i.uuid] for i in by["G6"]] == ["wfree"] and by["G6"][0].measure["value"] == pytest.approx(0.1, abs=1e-3)
    g7 = sorted(i.message.split(" ")[0] for i in by["G7"])
    assert g7 == ["2h", "Giron", "Hauteur", "Échappée"]
    head = next(i for i in by["G7"] if i.message.startswith("Échappée"))
    assert head.measure["value"] == pytest.approx(1.7, abs=1e-3) and head.elements[1]["uuid"] == g["beam"]
    res2, _ = run_geometry_rules(idx, recs, checks=["G2"], thresholds={"space_overlap_exclude_pattern": "^1$"})
    assert not res2.issues


def test_clash_tools_through_mcp(mcp_call, fake_env, tmp_path, monkeypatch):
    import openpyxl

    from bimdata_mcp import files, service_geometry
    from bimdata_mcp.auth import Identity
    from tests.test_workflow import _path

    path, g = _clash_model(tmp_path)
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)

    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall,IfcPipeSegment")
    assert out["synthese"]["par_type"] == {"chevauchement": 3}  # w1×w2, u1×u6, wp×tuyau arrêté
    issues = mcp_call("get_check_issues", check_id=out["check_id"], limit=20)["issues"]
    assert {i["clash_type"] for i in issues} == {"chevauchement"}
    assert all(len(i["elements"]) == 2 and i["bbox"] and i["group"] for i in issues)
    wb = openpyxl.load_workbook(_path(out["livrable"]["annexe_excel"]))
    assert "Volumes" in wb.sheetnames
    vol = [r for r in wb["Volumes"].iter_rows(min_row=3, values_only=True) if r[0]]
    assert any(r[4] and r[4] > 0 for r in vol)

    default = mcp_call("run_clash_matrix")
    assert default["matrice"] == "matrice par défaut" and len(default["cases"]) == 5

    tpl = mcp_call("clash_matrix_template")
    p = _path(tpl["livrable"])
    wb = openpyxl.load_workbook(p)
    ws = wb["Matrice"]
    ws.delete_rows(3, ws.max_row)
    ws.append(["MURS", "classes:IfcWall", "classes:IfcWall", "chevauchement", 0.0, 0.0, None, "bloquant", "Non",
               "Non", "Oui"])
    ws.append(["OFF", "Fluides", "Structure", "", None, None, None, None, "Non", "Non", "Non"])
    wb.save(p)
    meta = files.store_upload(Identity("local", "local").storage_key, "matrice.xlsx", p.read_bytes())
    m = mcp_call("run_clash_matrix", file_id=meta["file_id"])
    assert [c["case"] for c in m["cases"]] == ["MURS"]
    iss = mcp_call("get_check_issues", check_id=m["check_id"], limit=50)["issues"]
    assert iss and all(i["severity"] == "bloquant" and i["cell"] == "MURS" for i in iss)  # case inactive ignorée
    pairs = {frozenset(e["uuid"] for e in i["elements"]) for i in iss}
    assert frozenset((g["t1"], g["t4"])) in pairs  # tolérance 0 : les 4 mm ressortent

    menu = {a["id"]: a for a in mcp_call("list_audits")["audits"]}
    assert menu["c2_clash"]["status"] in ("pret", "partiel") and menu["c4_geometrie"]["status"] == "pret"


def test_bcf_and_smart_view_from_clash_check(mcp_call, fake_env, tmp_path, monkeypatch):
    import math

    from bimdata_mcp import service_geometry

    path, g = _clash_model(tmp_path)
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    plan = mcp_call("prepare_bcf_topics", title="Conflits", check_id=out["check_id"], check_group_by="issue",
                    isolate=True, section_box=True)
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.plans import load_plan
    from bimdata_mcp.state import UserState

    items = [i for i in load_plan(UserState(Identity("local", "local")), plan["plan_id"])["items"]
             if i["action"] == "create"]
    assert len(items) == out["summary"]["non_conformities"] == 2
    pair = {g["w1"], g["w2"]}
    vp = next(it["payload"]["viewpoints"][0] for it in items
              if {c["ifc_guid"] for c in it["payload"]["viewpoints"][0]["components"]["selection"]} == pair)
    cam = vp["perspective_camera"]
    eye = [cam["camera_view_point"][k] for k in "xyz"]
    d = [cam["camera_direction"][k] for k in "xyz"]
    center = (3.5, 0.1, 1.5)  # boîte d'intersection de w1 × w2 : x 3–4, y 0–0,2, z 0–3
    t = sum((center[i] - eye[i]) * d[i] for i in range(3))
    closest = [eye[i] + d[i] * t for i in range(3)]
    assert t > 0 and math.dist(closest, center) < 1e-3  # la caméra vise le centre de la zone
    assert vp["components"]["visibility"]["default_visibility"] is False
    assert len(vp["clipping_planes"]) == 6
    sv = mcp_call("prepare_smart_view", title="Conflits", check_id=out["check_id"])
    from bimdata_mcp.plans import load_plan as lp

    groups = lp(UserState(Identity("local", "local")), sv["plan_id"])["items"][0]["payload"]["viewpoints"][0]
    colored = {c["ifc_guid"] for grp in groups["components"]["coloring"] for c in grp["components"]}
    assert pair <= colored  # A et B colorés


def test_g6_unmodelled_screed_is_info(tmp_path):
    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.storey("R+1", 3.0)
    g = {}
    g["dalle"] = b.box("IfcSlab", "Dalle R+1", (0, 0, 2.7), (10, 10, 0.2), storey="R+1").GlobalId  # dessus à 2,90
    g["chape"] = b.box("IfcWall", "Cloison au niveau", (1, 1, 3.0), (3, 0.1, 2.5), storey="R+1").GlobalId  # 10 cm
    g["flotte"] = b.box("IfcWall", "Cloison haute", (1, 5, 3.3), (3, 0.1, 2.2), storey="R+1").GlobalId  # 40 cm
    idx, _ = get_or_build(b.write(tmp_path / "g6.ifc"), tmp_path / "c", key="k")
    recs = {u: Record(uuid=u, ifc_class=e["ifc_class"], attrs={"Name": e["name"]}) for u, e in idx.elements.items()}
    res, _ = run_geometry_rules(idx, recs, checks=["G6"])
    by = {i.uuid: i for i in res.issues}
    assert by[g["chape"]].severity == "info" and by[g["chape"]].message.startswith("Chape non modélisée")
    assert by[g["flotte"]].severity != "info" and by[g["flotte"]].message.startswith("Mur flottant")
    assert res.per_requirement[0]["non_conformes"] == 1  # l'information ne compte pas en anomalie


def test_index_background_build(tmp_path):
    from bimdata_mcp.geometry import index as gi

    path, _ = _clash_model(tmp_path)
    with pytest.raises(gi.IndexBuilding) as exc:
        gi.get_or_build(path, tmp_path / "bg", key="bg", background_over_mb=1e-6)
    assert exc.value.info["etat"] == "en_cours" and "tâche de fond" in str(exc.value)
    for job in list(gi._jobs.values()):
        job["thread"].join(timeout=60)
    idx, built = gi.get_or_build(path, tmp_path / "bg", key="bg", background_over_mb=1e-6)
    assert idx.meta["n_indexed"] > 0 and not built  # prêt : servi sans nouveau calcul


def test_compare_with_solibri_export(mcp_call, fake_env, tmp_path, monkeypatch):
    import zipfile

    from bimdata_mcp import files, service_geometry
    from bimdata_mcp.auth import Identity

    path, g = _clash_model(tmp_path)
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall,IfcColumn", set_b="classes:IfcWall,IfcColumn")
    assert out["summary"]["non_conformities"] == 3  # w1×w2, d1×d2, u1×u6
    # Export « Solibri » BCF : w1×w2 et d2×d1 (ordre inversé) communs, t1×t4 en plus ; u1×u6 manquant.
    bcf = tmp_path / "solibri.bcfzip"
    with zipfile.ZipFile(bcf, "w") as z:
        for k, (a, b2) in enumerate([("w1", "w2"), ("d2", "d1"), ("t1", "t4")]):
            z.writestr(f"topic{k}/markup.bcf", f"<Markup><Topic><Title>Clash {k}</Title></Topic></Markup>")
            z.writestr(f"topic{k}/viewpoint.bcfv", f'<VisualizationInfo><Components><Selection>'
                       f'<Component IfcGuid="{g[a]}"/><Component IfcGuid="{g[b2]}"/></Selection></Components>'
                       f'</VisualizationInfo>')
    meta = files.store_upload(Identity("local", "local").storage_key, "solibri.bcfzip", bcf.read_bytes())
    cmp_ = mcp_call("compare_clash_results", check_id=out["check_id"], file_id=meta["file_id"])
    s = cmp_["synthese"]
    assert (s["mcp"], s["externe"], s["communs"], s["mcp_seulement"], s["externe_seulement"]) == (3, 3, 2, 1, 1)
    assert s["ecart_nombre_pct"] == 0.0 and s["ecart_paires_pct"] == pytest.approx(66.7)
    assert cmp_["export"]["format"] == "BCF" and cmp_["livrable"]["download_url"]
    # Même comparaison depuis un export Excel (colonnes quelconques, GlobalId dans les cellules).
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["N°", "Composant 1", "Composant 2", "Gravité"])
    ws.append([1, g["w1"], g["w2"], "Critique"])
    ws.append([2, g["u1"], g["u6"], "Modéré"])
    xl = tmp_path / "solibri.xlsx"
    wb.save(xl)
    meta = files.store_upload(Identity("local", "local").storage_key, "solibri.xlsx", xl.read_bytes())
    s2 = mcp_call("compare_clash_results", check_id=out["check_id"], file_id=meta["file_id"])["synthese"]
    assert (s2["communs"], s2["mcp_seulement"], s2["externe_seulement"]) == (2, 1, 0)
    assert s2["ecart_nombre_pct"] == 50.0


def test_insulation_around_its_own_duct_is_an_exception(tmp_path):
    """Isolant (IfcCovering INSULATION) autour de sa gaine : ni doublon, ni inclusion, ni chevauchement. Le même
    isolant traversé par une autre gaine perpendiculaire reste un conflit."""
    from bimdata_mcp.geometry.clash import ClashOptions, find_clashes

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    duct = b.box("IfcDuctSegment", "Gaine", (0, 0, 2.5), (6, 0.4, 0.3), storey="RDC")
    ins = b.box("IfcCovering", "Isolation des gaines:Default", (0, -0.03, 2.47), (6, 0.46, 0.36), storey="RDC",
                PredefinedType="INSULATION")
    other = b.box("IfcDuctSegment", "Autre gaine", (3, -1, 2.55), (0.2, 3, 0.2), storey="RDC")
    idx, _ = get_or_build(b.write(tmp_path / "ins.ifc"), tmp_path / "c", key="k")
    got, stats = find_clashes(idx, [duct.GlobalId, other.GlobalId], [ins.GlobalId], ClashOptions())
    pairs = {frozenset((c.a, c.b)) for c in got}
    assert frozenset((duct.GlobalId, ins.GlobalId)) not in pairs
    assert frozenset((other.GlobalId, ins.GlobalId)) in pairs
    assert stats["exceptions"].get("isolant_reseau") == 1
    off = ClashOptions()
    off.exceptions["isolant_reseau"] = {"enabled": False}
    got2, _ = find_clashes(idx, [duct.GlobalId], [ins.GlobalId], off)
    assert got2  # sans l'exception : conflit signalé
