"""Points de vue BCF (méthode Solibri) : caméra, coupe, composants, image, clé d'anomalie et relance du contrôle."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from bimdata_mcp import bcf
from bimdata_mcp.geometry.render import camera_frame, project
from tests.ifc_factory import IfcBuilder

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _corners(zone):
    return np.array([[x, y, z] for x in (zone[0], zone[3]) for y in (zone[1], zone[4]) for z in (zone[2], zone[5])])


@pytest.mark.parametrize("bbox", [[3, 0, 0, 4, 0.2, 3], [0, 0, 0, 30, 0.2, 3], [10, 10, 5, 10.05, 10.05, 5.05]])
def test_camera_centres_zone_and_frames_it(bbox):
    zone = bcf.dilate(bbox)
    assert all(zone[i + 3] - zone[i] >= bcf.MIN_SIDE_M - 1e-9 for i in range(3))  # côté minimal 2 m
    for vp in ({"perspective_camera": bcf.perspective(zone, 60.0)}, {"orthogonal_camera": bcf.orthogonal_plan(zone)}):
        cam = camera_frame(vp)
        assert abs(float(cam["d"] @ cam["up"])) < 1e-6  # vecteur haut perpendiculaire
        c = (np.array(zone[:3]) + np.array(zone[3:])) / 2
        xy, _ = project(c[None, :], cam)
        assert np.allclose(xy[0], [640, 360], atol=1.0)  # le centre tombe au centre de l'image
        pts, z = project(_corners(zone), cam)
        assert (pts[:, 0] >= -1).all() and (pts[:, 0] <= 1281).all() and (pts[:, 1] >= -1).all() and (pts[:, 1] <= 721).all()
        if "perspective_camera" in vp:
            assert (z > 0).all()


def test_raw_up_vector_never_sent():
    cam = bcf.perspective([0, 0, 0, 2, 2, 2])
    u = np.array(list(cam["camera_up_vector"].values()))
    assert not np.allclose(u, [0, 0, 1]) and abs(np.linalg.norm(u) - 1) < 1e-3


def test_clipping_direction_points_to_hidden_side():
    from bimdata_mcp.geometry.render import _inside_clip

    zone = bcf.dilate([0, 0, 0, 1, 1, 1])
    planes = bcf.box_planes(zone)
    c = (np.array(zone[:3]) + np.array(zone[3:])) / 2
    outside = np.array(zone[3:]) + 1.0
    assert _inside_clip(np.array([c, outside]), planes).tolist() == [True, False]
    plane = bcf.horizontal_plane(1.2, c)
    assert _inside_clip(np.array([[0, 0, 0.5], [0, 0, 2.0]]), plane).tolist() == [True, False]


def test_components_roles_colors_and_visibility():
    els = [{"uuid": "A" * 22, "role": "A"}, {"uuid": "B" * 22 + "#12", "role": "B"}, {"uuid": "C" * 22, "role": "escalier"}]
    c = bcf.components(els, model_id=3, isolate=False, spaces_visible=True)
    assert [s["ifc_guid"] for s in c["selection"]] == ["A" * 22, "B" * 22, "C" * 22]
    assert {g["color"]: len(g["components"]) for g in c["coloring"]} == {"FF0000": 1, "FFA500": 1, "0000FF": 1}
    assert c["visibility"]["default_visibility"] is True and c["visibility"]["exceptions"] == []
    assert c["visibility"]["view_setup_hints"] == {"spaces_visible": False, "space_boundaries_visible": False,
                                                   "openings_visible": False}
    iso = bcf.components(els, model_id=3, isolate=True, spaces_visible=False)["visibility"]
    assert iso["default_visibility"] is False and len(iso["exceptions"]) == 3


def test_issue_key_guid_and_urn_are_stable():
    assert bcf.issue_key("clash", ["b", "a#3"]) == bcf.issue_key("CLASH", ["a", "b"])
    assert bcf.issue_key("S4", ["a"]) != bcf.issue_key("S5", ["a"])
    k = bcf.issue_key("F3", ["G2", "G1"])
    assert bcf.topic_guid(2, 3, k) == bcf.topic_guid("2", "3", k) != bcf.topic_guid(2, 4, k)  # UUID v5
    urn = bcf.reference_urn("F3", ["G2", "G1#7"], k)
    assert urn == "urn:mcp-bimdata:F3:G1+G2" and bcf.key_from_reference(urn) == k
    long = bcf.reference_urn("clash", [f"{i:022d}" for i in range(50)], k)
    assert long == f"urn:mcp-bimdata:clash:key:{k}" and bcf.key_from_reference(long) == k
    assert bcf.key_from_reference(f"mcp://check/chk_1/issue/{k}") == k  # anciens topics


def _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp import service_geometry

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    g = {"w1": b.box("IfcWall", "Mur 1", (0, 0, 0), (4, 0.2, 3), storey="RDC").GlobalId,
         "w2": b.box("IfcWall", "Mur 2", (3, 0, 0), (4, 0.2, 3), storey="RDC").GlobalId}
    g["w3"] = b.box("IfcWall", "Mur 3", (10, 0, 0), (2, 0.2, 3), storey="RDC").GlobalId
    g["w4"] = b.box("IfcWall", "Mur 4", (11, -1, 0), (0.2, 2, 3), storey="RDC").GlobalId
    path = b.write(tmp_path / "bcf.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    return g


def _items(plan_id):
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.plans import load_plan
    from bimdata_mcp.state import UserState

    return load_plan(UserState(Identity("local", "local")), plan_id)["items"]


def test_topic_viewpoints_snapshot_and_rerun(mcp_call, fake_env, tmp_path, monkeypatch):
    from PIL import Image

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    t0 = time.time()
    plan = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", extra_viewpoint="auto")
    dt = time.time() - t0
    allitems = _items(plan["plan_id"])
    ext = allitems[0]
    assert ext["action"] == "extensions"
    assert ext["missing"]["topic_type"] == ["Clash"] and ext["missing"]["priority"] == ["Critical", "Information"]
    assert "topic_status" not in ext["missing"]  # « Closed » du projet vaut Resolved : rien à déclarer
    assert "Clash" in ext["missing"]["topic_label"]
    items = [i for i in allitems if i["action"] != "extensions"]
    assert len(items) == 2 and all(i["action"] == "create" for i in items)
    for it in items:
        pl = it["payload"]
        assert pl["title"].startswith("[clash]") and pl["priority"] in bcf.PRIORITY.values()
        assert pl["format"] == "standard" and pl["topic_type"] == "Clash" and pl["models"] == [3]
        assert pl["guid"] == bcf.topic_guid(2, 3, it["key"])
        assert [bcf.key_from_reference(x) for x in pl["reference_links"]] == [it["key"]]
        assert pl["reference_links"][0].startswith("urn:mcp-bimdata:clash:")
        assert f"clé : {it['key']}" in pl["description"].splitlines()
        assert "Clash" in pl["labels"]
        vps = pl["viewpoints"]
        assert len(vps) == 2 and "perspective_camera" in vps[0] and "orthogonal_camera" in vps[1]  # vue plan en plus
        # viewer BIMData : origine « ifc3d » (fenêtre 3D) et une seule vue illustrée, sinon il demande une mise en page
        assert all(v["originating_system"] == "ifc3d" for v in vps)
        assert "snapshot" in vps[0] and "snapshot" not in vps[1]
        sel = [s["ifc_guid"] for s in vps[0]["components"]["selection"]]
        assert len(sel) == 2 and all([s["ifc_guid"] for s in v["components"]["selection"]] == sel for v in vps)
        assert len(vps[0]["clipping_planes"]) == 6  # boîte de coupe active par défaut pour un conflit
        for v in vps:
            assert bcf.validate_viewpoint(v, snapshot=v is vps[0]) == [] and v["guid"] and v["models"] == [3]
            assert v["pins"] == [{"point": v["pins"][0]["point"], "name": "clash", "color": "ff0000"}]
            assert len(v["lines"]) == 3  # croix de 3 segments de 0,5 m (repère lu par Solibri)
            assert all(abs(sum((ln["end_point"][a] - ln["start_point"][a]) ** 2 for a in "xyz") ** 0.5 - 0.5) < 1e-3
                       for ln in v["lines"])
            assert "bimdata_camera_direction" not in v.get("perspective_camera", {})
            if v is not vps[0]:
                continue
            path = v["snapshot"]["snapshot_data"][len("@file:"):]
            img = Image.open(path)
            assert img.format == "PNG" and max(img.size) <= 1500 and img.size == (1280, 720)
            from pathlib import Path

            assert Path(path).stat().st_size < 500_000
            arr = np.asarray(img.convert("RGB"))
            assert ((arr[:, :, 0] > 150) & (arr[:, :, 1] < 80) & (arr[:, :, 2] < 80)).sum() > 500  # élément A en rouge
    assert dt / len(items) < 2.0  # images rendues vite
    shown = mcp_call("show_plan", plan_id=plan["plan_id"])
    assert all(i["images"] for i in shown["items"] if i["action"] == "create")
    # envoi, puis relance du contrôle : aucun topic à recréer
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["failed"] == 0 and sum(1 for c in res["created"] if c["action"] == "create") == 2
    from tests.conftest import FakeClient

    sent = FakeClient.project_topics[0]["viewpoints"][0]["snapshot"]["snapshot_data"]
    assert not sent.startswith("@file:") and len(sent) > 1000  # image en base64 à l'envoi
    out2 = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    plan2 = mcp_call("prepare_bcf_topics", check_id=out2["check_id"], check_group_by="issue")
    assert plan2["summary"]["topics_a_creer"] == 0 and plan2["summary"]["topics_a_commenter"] == 2
    assert not plan2["summary"]["extensions_a_declarer"]  # déclarées au premier envoi
    # une anomalie disparaît du contrôle suivant : son topic est proposé au statut Resolved
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.state import UserState

    out3 = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    st = UserState(Identity("local", "local"))
    chk = st.load("checks", out3["check_id"])
    chk["issues"] = chk["issues"][:1]
    st.save("checks", out3["check_id"], chk, gz=True)
    plan3 = mcp_call("prepare_bcf_topics", check_id=out3["check_id"], check_group_by="issue")
    assert plan3["summary"]["topics_a_creer"] == 0 and plan3["summary"]["topics_a_cloturer"] == 1
    res3 = mcp_call("apply_plan", plan_id=plan3["plan_id"], confirm=True)
    assert res3["failed"] == 0
    assert sum(1 for t in FakeClient.project_topics if t.get("topic_status") == "Closed") == 1  # équivalent du projet


def test_large_topic_is_split_without_truncation(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.state import UserState

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    st = UserState(Identity("local", "local"))
    chk = st.load("checks", out["check_id"])
    many = [{"uuid": f"{k:022d}", "role": "élément"} for k in range(2500)]
    chk["issues"] = [dict(chk["issues"][0], elements=many, issue_id="I000001")]
    st.save("checks", out["check_id"], chk, gz=True)
    plan = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", snapshot=False,
                    extra_viewpoint="none")
    items = [i for i in _items(plan["plan_id"]) if i["action"] == "create"]
    sizes = [len(i["payload"]["viewpoints"][0]["components"]["selection"]) for i in items]
    assert max(sizes) <= 1000 and sum(sizes) >= 2500  # découpé, rien de perdu
    assert all("liste complète dans la sélection MCP" in i["payload"]["description"] for i in items)


def test_duplicate_globalid_flagged_in_description():
    els = [{"uuid": "X" * 22 + "#5", "role": "A"}]
    c = bcf.components(els, model_id=3, isolate=False, spaces_visible=False)
    assert c["selection"][0]["ifc_guid"] == "X" * 22 and c["selection"][0]["authoring_tool_id"] == "3"


def test_units_in_metres_from_millimetre_ifc(tmp_path):
    """Le fichier d'essai est en millimètres : la caméra et la coupe sortent en mètres."""
    from bimdata_mcp.geometry.index import get_or_build

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    w = b.box("IfcWall", "Mur", (0, 0, 0), (4, 0.2, 3), storey="RDC").GlobalId
    idx, _ = get_or_build(b.write(tmp_path / "mm.ifc"), tmp_path / "c", key="k")
    assert idx.meta["unit_scale"] == pytest.approx(0.001)
    zone = bcf.dilate(idx.elements[w]["aabb"])
    cam = bcf.perspective(zone)
    eye = np.array(list(cam["camera_view_point"].values()))
    assert np.linalg.norm(eye - np.array([2, 0.1, 1.5])) < 30  # mètres, pas millimètres
    assert all(abs(p["location"]["x"]) < 20 for p in bcf.box_planes(zone))
    assert math.isclose(cam["field_of_view"], 60.0)


def test_refused_extensions_fall_back_to_nearest_value(mcp_call, fake_env, tmp_path, monkeypatch):
    """Droits insuffisants pour déclarer les extensions : valeur existante la plus proche, valeur souhaitée dans la
    description."""
    from tests.conftest import FakeClient

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    FakeClient.refuse_extensions = True
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    plan = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", snapshot=False)
    mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    sent = FakeClient.project_topics
    assert len(sent) == 2
    for t in sent:
        assert t["topic_type"] == "Issue" and t["priority"] in ("High", "Medium", "Low")
        last = t["description"].splitlines()[-1]  # une seule ligne d'adaptation, en fin de description
        assert last.startswith("Adaptations au projet BCF : type Clash → Issue") and "libellés non déclarés" in last
        assert "Clash" not in t["labels"]


def test_report_offers_bcf_and_offer_is_usable(mcp_call, fake_env, tmp_path, monkeypatch):
    """Après un rapport de contrôle, une proposition de topics BCF (question + appel prêt) ; rien n'est préparé.
    Acceptée, elle produit le plan annoncé ; le filtre de sévérité limite les topics."""
    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    offer = out["proposition_bcf"]
    assert offer["appel"] == {"outil": "prepare_bcf_topics",
                              "arguments": {"check_id": out["check_id"], "check_group_by": "issue"}}
    assert offer["topics_estimes"] == out["summary"]["non_conformities"] == 2
    assert offer["question"].startswith("Voulez-vous des topics BCF pour") and "accord" in offer["question"]
    assert offer["topics_estimes"] == 2 and "choisir quoi envoyer" not in offer["question"]
    assert not mcp_call("list_plans", limit=10)["plans"]  # proposition seulement : aucun plan préparé
    plan = mcp_call("prepare_bcf_topics", snapshot=False, **offer["appel"]["arguments"])
    assert plan["summary"]["topics_a_creer"] == 2
    sev = sorted(offer["anomalies"])[0]
    one = mcp_call("prepare_bcf_topics", snapshot=False, severities=[sev], **offer["appel"]["arguments"])
    assert one["summary"]["topics_a_creer"] == offer["anomalies"][sev]
    with pytest.raises(Exception, match="Aucune anomalie à transmettre"):
        mcp_call("prepare_bcf_topics", snapshot=False, severities=["info"], **offer["appel"]["arguments"])


def test_long_preparation_and_send_run_in_background_and_resume(mcp_call, fake_env, tmp_path, monkeypatch):
    """Beaucoup de topics : préparation et envoi en tâche de fond (réponse immédiate, job_status), envoi parallèle et
    reprenable sans doublon après une interruption."""
    import time as _t

    from bimdata_mcp import service_geometry
    from tests.conftest import FakeClient

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    for k in range(15):  # 15 paires de murs qui se chevauchent
        b.box("IfcWall", f"Mur A{k}", (10 * k, 0, 0), (4, 0.2, 3), storey="RDC")
        b.box("IfcWall", f"Mur B{k}", (10 * k + 3, 0, 0), (4, 0.2, 3), storey="RDC")
    path = b.write(tmp_path / "many.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    assert out["summary"]["non_conformities"] == 15

    def wait(job_id):
        for _ in range(300):
            st = mcp_call("job_status", job_id=job_id)
            if st["status"] != "en_cours":
                return st
            _t.sleep(0.2)
        raise AssertionError("tâche trop longue")

    job = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", extra_viewpoint="none")
    assert job["status"] == "en_cours" and job["topics_estimes"] == 15 and "job_status" in job["consigne"]
    done = wait(job["job_id"])
    assert done["status"] == "termine" and done["done"] == done["total"] == 15  # 15 images
    plan_id = done["result"]["plan_id"]
    assert done["result"]["summary"]["topics_a_creer"] == 15
    # envoi : une panne au 6e topic, puis reprise
    calls = {"n": 0}
    real = FakeClient.create_bcf_full_topic

    def flaky(self, payload):
        _t.sleep(0.15)  # envoi réaliste : le second appel arrive pendant l'envoi
        calls["n"] += 1
        if calls["n"] == 6:
            raise ConnectionError("réseau coupé")
        return real(self, payload)

    monkeypatch.setattr(FakeClient, "create_bcf_full_topic", flaky)
    monkeypatch.setattr("bimdata_mcp.plans._retry", lambda fn, payload: fn(payload))  # pas de 2e essai ici
    job2 = mcp_call("apply_plan", plan_id=plan_id, confirm=True)
    assert job2["applied"] == "en_cours"
    from bimdata_mcp import service as svc

    assert plan_id in svc._SENDING
    with pytest.raises(Exception, match="Envoi déjà en cours"):  # second envoi pendant le premier : refusé
        mcp_call("apply_plan", plan_id=plan_id, confirm=True)
    r1 = wait(job2["job_id"])["result"]
    assert r1["failed"] == 1 and r1["restants"] == 1
    assert len(FakeClient.project_topics) == 14
    r2 = mcp_call("apply_plan", plan_id=plan_id, confirm=True)  # reprise : 1 seul topic restant, envoyé tout de suite
    assert r2["deja_faits_avant_reprise"] >= 15 and r2["failed"] == 0
    assert len(FakeClient.project_topics) == 15
    assert len({t["guid"] for t in FakeClient.project_topics}) == 15  # aucun doublon


def test_project_vocabulary_equivalents_are_used_without_declaring():
    from bimdata_mcp.service_bcf import project_value

    ext = {"topic_status": ["Opened", "Closed"], "topic_type": ["Error", "Request"], "priority": ["High", "Medium"]}
    assert project_value("topic_status", "Open", ext) == "Opened"
    assert project_value("topic_status", "Resolved", ext) == "Closed"
    assert project_value("topic_type", "Issue", ext) == "Error"
    assert project_value("topic_type", "Clash", ext) is None  # pas d'équivalent : à déclarer
    assert project_value("priority", "high", ext) == "High"


def test_excel_report_offered_after_sending_bcf(mcp_call, fake_env, tmp_path, monkeypatch):
    """Après l'envoi des topics : proposition d'un rapport Excel ; le rapport liste chaque topic avec sa miniature."""
    import openpyxl

    from bimdata_mcp.config import get_settings

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    plan = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", extra_viewpoint="none")
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    offer = res["proposition_rapport"]
    assert offer["appel"] == {"outil": "export_bcf_report", "arguments": {"plan_id": plan["plan_id"]}}
    assert "rapport Excel des 2 topic(s)" in offer["question"]
    rep = mcp_call("export_bcf_report", **offer["appel"]["arguments"])
    assert rep["topics"] == rep["envoyes"] == 2 and rep["livrable"]["fichier"].endswith("_BCF.xlsx")
    p = next(get_settings().users_dir.glob(f"*/files/{rep['livrable']['file_id']}/{rep['livrable']['fichier']}"))
    wb = openpyxl.load_workbook(p)
    assert wb.sheetnames[:2] == ["Synthèse", "Topics BCF"]
    ws = wb["Topics BCF"]
    head = [c.value for c in ws[2]]
    assert {"Titre", "Règle", "Priorité", "Constat", "GlobalId", "Clé d'anomalie", "GUID du topic", "Image"} <= set(head)
    rows = [r for r in ws.iter_rows(min_row=3, values_only=True) if r[1]]
    assert len(rows) == 2 and all(r[head.index("Envoi")] == "Envoyé" for r in rows)
    assert all(r[head.index("Règle")] == "clash" and r[head.index("GlobalId")] for r in rows)
    assert len(ws._images) == 2  # une miniature par topic


def test_repair_old_topics_for_bimdata_viewer(mcp_call, fake_env):
    """Topics envoyés avant la correction (origine « MCP BIMData », deux vues illustrées) : plan de réparation, puis
    origine « ifc3d » et vue complémentaire supprimée ; les topics d'autres origines ne sont pas touchés."""
    from tests.conftest import FakeClient

    mcp_call("set_active_model", bimdata_url="https://platform.bimdata.io/spaces/1/projects/2/viewer/3")
    FakeClient.project_topics[:] = [
        {"guid": "t1", "title": "[clash] Toit × Mur", "index": 247, "viewpoints": [
            {"guid": "v1", "index": 0, "originating_system": "MCP BIMData 0.1.0", "snapshot": {"url": "a"}},
            {"guid": "v2", "index": 1, "originating_system": "MCP BIMData 0.1.0", "snapshot": {"url": "b"}}]},
        {"guid": "t2", "title": "Topic manuel", "viewpoints": [{"guid": "v3", "originating_system": "ifc3d"}]}]
    plan = mcp_call("repair_bcf_topics_for_viewer")
    assert plan["summary"] == {"topics_lus": 2, "topics_a_reparer": 1, "points_de_vue_corriges": 2,
                               "vues_complementaires_supprimees": 1}
    assert not [w for c in FakeClient.instances for w in c.writes if w[0].startswith("bcf_vp")]  # rien avant accord
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["failed"] == 0 and "proposition_rapport" not in res
    t1 = FakeClient.project_topics[0]
    assert [(v["guid"], v["originating_system"]) for v in t1["viewpoints"]] == [("v1", "ifc3d")]
    assert FakeClient.project_topics[1]["viewpoints"] == [{"guid": "v3", "originating_system": "ifc3d"}]
    assert mcp_call("repair_bcf_topics_for_viewer")["summary"]["topics_a_reparer"] == 0


def test_many_topics_ask_to_choose_by_severity_rule_or_type(mcp_call, fake_env, tmp_path, monkeypatch):
    """Plus de 20 topics sans filtre : rien n'est préparé, l'outil propose de choisir par gravité, règle ou type ;
    l'option choisie prépare le plan correspondant ; « tout envoyer » seulement explicitement."""
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.state import UserState

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    st = UserState(Identity("local", "local"))
    c = st.load("checks", out["check_id"])
    base = c["issues"][0]
    iss = []
    for k in range(26):  # 26 anomalies : 4 bloquants doublons, 22 mineurs chevauchements (dont 12 d'une autre règle)
        sev, ty, rule = ("bloquant", "doublon", "CLASH") if k < 4 else ("mineur", "chevauchement",
                                                                       "CLASH" if k < 14 else "S4")
        iss.append(dict(base, issue_id=f"I{k:03d}", severity=sev, clash_type=ty, rule_id=rule, req_id=rule))
    c["issues"] = iss
    st.save("checks", out["check_id"], c, gz=True)
    offer = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")["proposition_bcf"]
    assert offer is not None
    ask = mcp_call("prepare_bcf_topics", check_id=out["check_id"], check_group_by="issue", snapshot=False)
    assert ask["etape"] == "choix_perimetre_bcf" and ask["topics_total"] == 26
    assert ask["par_gravite"] == {"bloquant": 4, "majeur": 0, "mineur": 22, "info": 0}
    assert ask["par_type"] == {"chevauchement": 22, "doublon": 4}
    assert {r["regle"]: r["topics"] for r in ask["par_regle"]} == {"CLASH": 14, "S4": 12}
    assert "Par gravité : 4 bloquant, 22 mineur." in ask["question"]
    assert not mcp_call("list_plans", limit=10)["plans"]  # rien de préparé
    rec = ask["options"][0]
    assert rec["recommande"] and rec["arguments"]["severities"] == ["bloquant"] and rec["topics"] == 4
    plan = mcp_call("prepare_bcf_topics", snapshot=False, **rec["arguments"])
    assert plan["summary"]["topics_a_creer"] == 4
    by_type = next(o for o in ask["options"] if o["choix"] == "Type de conflit « chevauchement »")
    by_rule = next(o for o in ask["options"] if o["choix"].startswith("Règle S4"))
    assert by_rule["topics"] == 12 and by_type["topics"] == 22
    assert mcp_call("prepare_bcf_topics", snapshot=False, dedupe=False,
                    **by_rule["arguments"])["summary"]["topics_a_creer"] == 12
    everything = ask["options"][-1]
    assert everything["choix"] == "Tout envoyer" and everything["arguments"]["perimetre_valide"] is True


def test_smart_view_from_check_proposes_selections(mcp_call, fake_env, tmp_path, monkeypatch):
    """Smart View depuis un contrôle : plusieurs règles → choix proposé (gravité, règle, niveau, une par règle) ;
    l'option choisie produit le plan (une ou plusieurs Smart Views)."""
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.plans import load_plan
    from bimdata_mcp.state import UserState

    _clash_setup(mcp_call, fake_env, tmp_path, monkeypatch)
    out = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    st = UserState(Identity("local", "local"))
    c = st.load("checks", out["check_id"])
    a, b = c["issues"][:2]
    c["issues"] = [dict(a, severity="bloquant", storey="RDC"),
                   dict(b, severity="mineur", rule_id="S4", req_id="S4", req_label="Porté", storey="R+1")]
    st.save("checks", out["check_id"], c, gz=True)
    ask = mcp_call("prepare_smart_view", title="Conflits", check_id=out["check_id"])
    assert ask["etape"] == "choix_selection_smart_view" and not mcp_call("list_plans", limit=5)["plans"]
    assert ask["par_regle"] == {"clash": 1, "S4": 1} and set(ask["par_niveau"]) == {"RDC", "R+1"}
    choix = [o["choix"] for o in ask["options"]]
    assert choix[0].startswith("Bloquants et majeurs") and "Une Smart View par règle" in choix
    assert "Une Smart View par niveau" in choix and choix[-1].startswith("Tout le contrôle")
    one = mcp_call("prepare_smart_view", **ask["options"][0]["arguments"])
    items = load_plan(st, one["plan_id"])["items"]
    assert len(items) == 1
    labels = [g["color"] for g in items[0]["payload"]["viewpoints"][0]["components"]["coloring"]]
    assert len(labels) == 1  # bloquants seulement, pas de groupe « conforme »
    split = next(o for o in ask["options"] if o["choix"] == "Une Smart View par règle")
    per_rule = mcp_call("prepare_smart_view", **split["arguments"])
    items = load_plan(st, per_rule["plan_id"])["items"]
    assert len(items) == 2 and per_rule["summary"]["smart_views"] == 2
    titles = sorted(i["payload"]["title"] for i in items)
    assert titles[0].startswith("Conflits — S4 Porté") and titles[1].startswith("Conflits — clash")
