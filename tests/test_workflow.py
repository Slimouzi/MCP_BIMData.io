"""Parcours complets via le client MCP : sélection → exports → publication → contrôle → corrections."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _path(meta: dict) -> Path:
    from bimdata_mcp.config import get_settings

    name = meta.get("fichier") or meta["name"]
    return next((get_settings().users_dir).glob(f"*/files/{meta['file_id']}/{name}"))


def test_activation_and_overview(mcp_call):
    r = mcp_call("set_active_model", bimdata_url=URL)
    assert r["target"] == {"cloud_id": "1", "project_id": "2", "model_id": "3"}
    assert [s["name"] for s in r["storeys"]] == ["R+1", "R+2"]
    assert r["storeys"][0]["n_elements"] >= 3
    props = mcp_call("list_properties", ifc_classes=["IfcWall"])
    fields = {p["field"] for p in props["fields"]}
    assert "Pset_WallCommon.FireRating" in fields


def test_selection_and_excel_export(mcp_call):
    mcp_call("set_active_model", bimdata_url=URL)
    r = mcp_call("select_elements", name="murs R+1", criteria={"ifc_classes": ["IfcWall"], "storeys": ["R+1"]})
    assert r["count"] == 2  # sous-types inclus, étage filtré
    r2 = mcp_call("select_elements", name="coupe-feu", criteria={
        "ifc_classes": ["IfcWall"], "conditions": [{"field": "*.FireRating", "op": "exists"}]})
    assert r2["count"] == 2
    inter = mcp_call("combine_selections", name="inter", operation="intersection", selections=["murs_R_1", "coupe-feu"])
    assert inter["count"] == 1
    exp = mcp_call("export_selection_excel", selection="murs_R_1", publish_to_bimdata=True)
    assert exp["livrable"]["download_url"].startswith("http://testserver/files/")
    assert exp["bimdata"]["bimdata_document_id"] == 1
    wb = openpyxl.load_workbook(_path(exp["livrable"]))
    assert wb.sheetnames == ["Synthèse", "IfcWallStandardCase", "Lisez-moi", "_bimdata"]
    ws = wb["IfcWallStandardCase"]
    groups = [c.value for c in ws[1]]
    labels = [c.value for c in ws[2]]
    # Attributs comme dans le panneau Propriétés du viewer BIMData
    assert labels[:2] == ["UUID", "Type"]
    for minimal in ("Nom", "Description", "Type d'objet", "Tag", "Étage", "Matériaux"):
        assert minimal in labels
    assert groups[0] == "Attributs IFC"
    assert "Pset_WallCommon" in groups and "FireRating" in labels
    assert ws.max_row == 4  # 2 lignes d'en-tête + 2 murs
    assert wb["_bimdata"].sheet_state == "hidden"


def test_export_has_all_qto_and_psets_like_viewer(mcp_call, fake_env):
    """Minimum demandé : attributs + toutes les Qto + tous les Psets (cf. panneau Propriétés BIMData)."""
    from tests.conftest import _prop

    w1 = next(e for e in fake_env["elements"] if e["uuid"] == "W1")
    w1["attributes"]["properties"] += [_prop("PredefinedType", "NOTDEFINED"), _prop("Tag", "3365211")]
    w1["property_sets"] += [
        {"id": 1, "name": "IFC Pset - Active 3D", "properties": [_prop("IfcGUID", "W1"), _prop("Type BIM6D", "MUR")]},
        {"id": 2, "name": "Qto_WallBaseQuantities", "properties": [
            _prop("Length", 7.063, "IfcLengthMeasure"), _prop("NetVolume", 3.78, "IfcVolumeMeasure"),
            _prop("Width", 0.2, "IfcLengthMeasure")]},
    ]
    mcp_call("set_active_model", bimdata_url=URL, refresh=True)
    mcp_call("select_elements", name="w1", criteria={"uuids": ["W1"]})
    exp = mcp_call("export_selection_excel", selection="w1")
    ws = openpyxl.load_workbook(_path(exp["livrable"]))["IfcWallStandardCase"]
    groups, labels = [c.value for c in ws[1]], [c.value for c in ws[2]]
    row = dict(zip(labels, [c.value for c in ws[3]], strict=True))
    assert row["UUID"] == "W1" and row["Type"] == "IfcWallStandardCase"
    assert row["Type d'objet"] == "Mur de base:BA 200" and row["PredefinedType"] == "NOTDEFINED" and row["Tag"] == "3365211"
    assert row["Type BIM6D"] == "MUR" and row["IfcGUID"] == "W1"
    assert row["Length"] == 7.063 and row["NetVolume"] == 3.78 and row["Width"] == 0.2
    assert "IFC Pset - Active 3D" in groups and "Qto_WallBaseQuantities" in groups
    assert groups.index("Qto_WallBaseQuantities") > groups.index("IFC Pset - Active 3D")  # Psets puis Qto


def test_cobie(mcp_call):
    mcp_call("set_active_model", bimdata_url=URL)
    r = mcp_call("generate_cobie")
    assert r["synthese"]["Floor"] == 2 and r["synthese"]["Component"] >= 4
    wb = openpyxl.load_workbook(_path(r["livrable"]), read_only=True)
    assert {"Facility", "Floor", "Space", "Component", "Attribute"} <= set(wb.sheetnames)
    assert wb.sheetnames[0] == "Synthèse" and wb.sheetnames[-2:] == ["Lisez-moi", "_bimdata"]
    assert [c.value for c in next(wb["Floor"].iter_rows(max_row=1))][0] == "Name"  # en-tête COBie en ligne 1


def test_smartview_and_bcf_plans(mcp_call):
    from tests.conftest import FakeClient

    mcp_call("set_active_model", bimdata_url=URL)
    mcp_call("select_elements", name="murs", criteria={"ifc_classes": ["IfcWall"]})
    plan = mcp_call("prepare_smart_view", title="Murs par étage", selection="murs", color_by="Storey")
    assert plan["summary"]["groups"] == 2
    preview = mcp_call("apply_plan", plan_id=plan["plan_id"])  # sans confirm : rien n'est écrit
    assert preview["applied"] is False
    assert not any(c.topics for c in FakeClient.instances)
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["succeeded"] == 1
    sv = next(c for c in FakeClient.instances if c.topics).topics[0]
    assert sv["format"] == "bimdata-smartview" and len(sv["viewpoints"][0]["components"]["coloring"]) == 2
    with pytest.raises(Exception, match="déjà été appliqué"):
        mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    bcf = mcp_call("prepare_bcf_topics", title="Vérifier", selection="murs", group_by="Storey", priority="High")
    assert bcf["summary"]["topics"] == 2


def test_naming_check_and_auto_corrections(mcp_call, fake_env):
    mcp_call("set_active_model", bimdata_url=URL)
    chk = mcp_call("check_naming_completeness")
    statuses = chk["summary"]["by_status"]
    assert statuses.get("Doublon", 0) == 2          # R01-001 ×2 sur R+1
    assert statuses.get("Format invalide", 0) >= 1  # « Mur  ext »
    issues = mcp_call("get_check_issues", check_id=chk["check_id"], req_id="N02")["issues"]
    assert issues[0]["proposal"] == "Mur ext" and issues[0]["confidence"] >= 0.8
    plan = mcp_call("prepare_corrections_from_check", check_id=chk["check_id"])
    assert plan["summary"]["changes"] >= 1
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["failed"] == 0 and res["succeeded"] >= 1
    w3 = next(e for e in fake_env["elements"] if e["uuid"] == "W3")
    assert next(p["value"] for p in w3["attributes"]["properties"] if p["definition"]["name"] == "Name") == "Mur ext"
    chk2 = mcp_call("check_naming_completeness")
    assert chk2["summary"]["by_status"].get("Format invalide", 0) < statuses.get("Format invalide", 0)


def test_requirements_roundtrip_via_excel(mcp_call, fake_env):
    """Gabarit → import → contrôle → rapport complété → plan → écriture → recontrôle."""
    from bimdata_mcp import files
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.state import UserState

    mcp_call("set_active_model", bimdata_url=URL)
    tpl = mcp_call("requirements_template")
    imp = mcp_call("import_data_requirements", file_id=tpl["livrable"]["file_id"], name="CDC test")
    assert imp["n_requirements"] == 7 and "PRO" in imp["phases_available"]
    chk = mcp_call("run_requirements_check", requirements_id=imp["requirements_id"], phase="PRO")
    s = chk["summary"]
    assert s["non_conformities"] > 0
    # FireRating : « EI 60 » hors liste → proposition « EI60 » ; vide sur W2 ; mauvais Pset sur W3.
    fr = mcp_call("get_check_issues", check_id=chk["check_id"], req_id="R0003")["issues"]
    by_uuid = {i["uuid"]: i for i in fr}
    assert by_uuid["W1"]["status"] == "hors_liste" and by_uuid["W1"]["proposal"] == "EI60"
    assert by_uuid["W3"]["status"] == "mauvais_pset" and by_uuid["W3"]["proposal"] == "EI60"
    assert by_uuid["W2"]["status"] == "vide"
    # L'utilisateur complète le rapport : W2 → EI30.
    report = _path(chk["livrable"])
    wb = openpyxl.load_workbook(report)
    assert wb.sheetnames == ["Synthèse", "Erreurs à corriger", "Données contrôlées", "Exigences", "Lisez-moi", "_bimdata"]
    ws = wb["Erreurs à corriger"]
    assert ws.cell(1, 1).value == "Élément"  # ligne de groupes, comme tous les livrables
    h = [c.value for c in ws[2]]
    iu, ifld, iv, ia = h.index("UUID"), h.index("Champ"), h.index("Valeur corrigée"), h.index("Appliquer (Oui/Non)")
    for row in ws.iter_rows(min_row=3):
        if row[iu].value == "W2" and row[ifld].value == "Pset_WallCommon.FireRating":
            row[iv].value, row[ia].value = "EI30", "Oui"
    wb.save(report)
    meta = files.store_upload(UserState(Identity("local", "local")).identity.storage_key, "corrige.xlsx", report.read_bytes())
    plan = mcp_call("prepare_corrections_from_file", file_id=meta["file_id"])
    assert plan["format"] == "check-report"
    fields = {(i["uuid"], i["field"]): i["new_value"] for i in mcp_call("show_plan", plan_id=plan["plan_id"])["items"]}
    assert fields[("W2", "Pset_WallCommon.FireRating")] == "EI30"
    assert fields[("W1", "Pset_WallCommon.FireRating")] == "EI60"
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["failed"] == 0
    chk2 = mcp_call("run_requirements_check", requirements_id=imp["requirements_id"], phase="PRO")
    fr2 = mcp_call("get_check_issues", check_id=chk2["check_id"], req_id="R0003")["issues"]
    assert {i["uuid"] for i in fr2} == set()  # FireRating conforme partout


def test_corrections_from_modified_export(mcp_call, fake_env):
    from bimdata_mcp import files
    from bimdata_mcp.auth import Identity

    mcp_call("set_active_model", bimdata_url=URL)
    mcp_call("select_elements", name="portes", criteria={"ifc_classes": ["IfcDoor"]})
    exp = mcp_call("export_selection_excel", selection="portes")
    p = _path(exp["livrable"])
    wb = openpyxl.load_workbook(p)
    ws = wb["IfcDoor"]
    labels = [c.value for c in ws[2]]
    n = len(labels)
    ws.cell(3, labels.index("AcousticRating") + 1).value = "42"
    ws.cell(3, labels.index("Description") + 1).value = "Porte palière"
    ws.cell(1, n + 1).value = "Pset_DoorCommon"   # nouvelle colonne = nouvelle propriété
    ws.cell(2, n + 1).value = "FireRating"
    ws.cell(3, n + 1).value = "EI30"
    ws.cell(3, labels.index("Étage") + 1).value = "R+9"   # lecture seule : ignoré
    wb.save(p)
    meta = files.store_upload(Identity("local", "local").storage_key, "export_modifie.xlsx", p.read_bytes())
    plan = mcp_call("prepare_corrections_from_file", file_id=meta["file_id"])
    assert plan["format"] == "selection-export"
    assert plan["summary"]["changes"] == 3, plan
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["succeeded"] == 3 and res["failed"] == 0
    d1 = next(e for e in fake_env["elements"] if e["uuid"] == "D1")
    pset = next(ps for ps in d1["property_sets"] if ps["name"] == "Pset_DoorCommon")
    vals = {p["definition"]["name"]: p["value"] for p in pset["properties"]}
    assert vals["AcousticRating"] == "42" and vals["FireRating"] == "EI30"
    assert any(p["definition"]["name"] == "Description" and p["value"] == "Porte palière"
               for p in d1["attributes"]["properties"])


def test_plan_refused_on_other_model(mcp_call):
    mcp_call("set_active_model", bimdata_url=URL)
    mcp_call("select_elements", name="tout")
    plan = mcp_call("prepare_smart_view", title="x", selection="tout")
    mcp_call("set_active_model", bimdata_url=URL.replace("/viewer/3", "/viewer/4"))
    with pytest.raises(Exception, match="refusé"):
        mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)


def test_same_frame_for_every_deliverable(mcp_call):
    """Charte : tous les livrables partagent la même trame (Synthèse identique, ordre des feuilles, nommage)."""
    import re

    mcp_call("set_active_model", bimdata_url=URL)
    mcp_call("select_elements", name="murs", criteria={"ifc_classes": ["IfcWall"]})
    outs = [mcp_call("export_selection_excel", selection="murs"), mcp_call("generate_cobie"),
            mcp_call("check_naming_completeness"), mcp_call("requirements_template")]
    labels = None
    for out in outs:
        lv = out["livrable"]
        assert set(lv) >= {"type", "titre", "fichier", "file_id", "download_url", "taille_ko"}
        assert {"synthese", "avertissements", "suite"} <= set(out)
        assert re.fullmatch(r"\d{8}_[A-Za-z0-9-]+_[A-Za-z0-9-]+(_[A-Za-z0-9-]+)?_[A-Z-]+\.xlsx", lv["fichier"]), lv["fichier"]
        wb = openpyxl.load_workbook(_path(lv))
        assert wb.sheetnames[0] == "Synthèse" and wb.sheetnames[-2:] == ["Lisez-moi", "_bimdata"]
        syn = wb["Synthèse"]
        assert str(syn["D2"].value).startswith("BIMDATA — ")
        block = [syn.cell(r, 2).value for r in range(7, 15)]
        labels = labels or block
        assert block == labels == ["Projet", "Maquette", "Identifiants BIMData", "Périmètre", "Phase", "Généré le",
                                   "Généré par", "Outil"]
        for ws in wb.worksheets[1:-2]:
            hdr = ws.cell(2, 1) if ws.cell(1, 1).fill.fgColor.rgb in ("FFF9C72C",) else ws.cell(1, 1)
            assert hdr.fill.fgColor.rgb == "FF2F374A" and hdr.font.name == "Roboto"  # en-tête Primary, Roboto
