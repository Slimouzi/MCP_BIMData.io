"""Rapports PDF à la charte, hypothèses et réglages des audits (page à curseurs, set_audit_parameters)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfReader

from bimdata_mcp import audit_params as AP

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _file(meta: dict) -> Path:
    from bimdata_mcp.config import get_settings

    return next(get_settings().users_dir.glob(f"*/files/{meta['file_id']}/{meta['fichier']}"))


def _text(path: Path) -> str:
    import re

    return re.sub(r"\s+", " ", " ".join(p.extract_text() or "" for p in PdfReader(str(path)).pages))


def test_pdf_report_structure_and_disclaimer(tmp_path):
    from bimdata_mcp.charte import Contexte
    from bimdata_mcp.report_pdf import AuditSection, write_audit_pdf

    _, hyp = AP.apply_to_options("c6_degagements", {"c6_degagements.tolerance_m": 0.1}, {})
    issues = [{"severity": "bloquant", "rule_id": "DEG", "req_id": "DEG", "req_label": "Zone libre", "ifc_class": "IfcDoor",
               "name": "Porte 1", "storey": "RDC", "message": "Zone encombrée", "uuid": "x"}]
    sec = AuditSection(id="c6_degagements", label="Espaces libres et dégagements", check_id="chk_1",
                       rules=[{"req_id": "DEG", "label": "Zone libre", "applicable": 3, "non_conformes": 1, "taux": 0.67}],
                       issues=issues, notes=["Note de test"], hypotheses=hyp)
    ctx = Contexte(type_livrable="Audit de maquette", titre="Audit", projet="Projet test", maquette="M.ifc")
    p = write_audit_pdf(tmp_path / "r.pdf", ctx, title="Audit de la maquette", audits=[sec],
                        skipped=[{"audit": "Accessibilité PMR", "raison": "Paramètres à demander",
                                  "a_demander": [{"option": "referentiel", "question": "Quel référentiel ?"}]}])
    txt = _text(p)
    for chapter in ("1. Synthèse", "2. Objet, périmètre", "3. Hypothèses et paramètres", "4. Référentiels",
                    "5. Méthode", "6. Résultats détaillés", "7. Recommandations", "8. Limites"):
        assert chapter in txt, chapter
    assert "intelligence artificielle" in txt and "ne se substituent ni" in txt  # avertissement complet
    assert "ne remplace pas un bureau d'études" in txt  # pied de page
    assert "Empiètement toléré" in txt and "0,1 m" in txt  # hypothèse modifiée reprise
    assert "fournir « referentiel »" in txt  # décision attendue
    n = len(PdfReader(str(p)).pages)
    assert f"page {n} / {n}" in txt


def test_settings_validation_and_page():
    with pytest.raises(ValueError, match="hors de la plage"):
        AP.validate({"c2_clash.tol_h_m": 0.5})
    with pytest.raises(ValueError, match="choix admis"):
        AP.validate({"c7_incendie.f3_referentiel": "XYZ"})
    with pytest.raises(ValueError, match="Paramètre inconnu"):
        AP.validate({"c2_clash.inconnu": 1})
    opts, rows = AP.apply_to_options("c1_structure", {"c1_structure.carried_tolerance_m": 0.55},
                                     {"s4_thresholds": {"base_tolerance_m": 0.2}})
    assert opts["s4_thresholds"] == {"base_tolerance_m": 0.2, "carried_tolerance_m": 0.55}  # explicite prioritaire
    assert {r["cle"]: r["modifie"] for r in rows}["c1_structure.carried_tolerance_m"]


def test_settings_tools_page_and_run_audit(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp import audit_settings as AS
    from bimdata_mcp import service_geometry
    from tests.ifc_factory import IfcBuilder

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.box("IfcWall", "Mur", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    path = b.write(tmp_path / "s.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    menu = mcp_call("list_audits")
    assert menu["reglages_url"].startswith("http://testserver/reglages/")
    c6 = next(a for a in menu["audits"] if a["id"] == "c6_degagements")
    assert {p["cle"] for p in c6["parametres"]} >= {"c6_degagements.tolerance_m", "c6_degagements.front_m"}
    pmr = next(a for a in menu["audits"] if a["id"] == "c5_pmr")
    assert next(p for p in pmr["parametres"] if p["cle"] == "c5_pmr.referentiel")["a_fournir"]
    out = mcp_call("set_audit_parameters", values={"c6_degagements.tolerance_m": 0.08, "c5_pmr.referentiel": "ERP 2017"})
    assert out["enregistres"]["c6_degagements.tolerance_m"] == 0.08
    params = mcp_call("audit_parameters", audits=["c6_degagements"])
    tol = next(p for p in params["parametres"][0]["parametres"] if p["key"] == "c6_degagements.tolerance_m")
    assert tol["valeur"] == 0.08 and tol["modifie"] and tol["min"] == 0.0 and tol["max"] == 0.2
    # page à curseurs : barres glissantes et champs numériques, valeur enregistrée préremplie
    owner, key = menu["reglages_url"].split("/reglages/")[1].split("/")[0], None
    from urllib.parse import parse_qs, unquote, urlparse

    key = unquote(parse_qs(urlparse(menu["reglages_url"]).query)["t"][0])
    html = AS.page(owner, key)
    assert "type='range'" in html and "data-k='c6_degagements.tolerance_m'" in html and "value='0.08'" in html
    assert "Revenir au défaut" in html and "assistance d'IA" in html
    # run_audit : le référentiel PMR enregistré suffit (plus de question), la tolérance s'applique et figure au rapport
    pmr = mcp_call("run_audit", audit="c5_pmr")
    assert pmr["jeton_hypotheses"] and not pmr["a_demander"]  # le référentiel enregistré suffit
    h = mcp_call("run_audit", audit="c6_degagements")
    tol = next(x for x in h["hypotheses"] if x["cle"] == "c6_degagements.tolerance_m")
    assert tol["valeur"] == 0.08 and tol["modifie"] and h["reglages_url"]
    # un réglage changé après présentation invalide le jeton : nouvelles hypothèses à valider
    mcp_call("set_audit_parameters", values={"c6_degagements.tolerance_m": 0.09})
    again = mcp_call("run_audit", audit="c6_degagements", hypotheses_validees=h["jeton_hypotheses"])
    assert again["etape"] == "validation_hypotheses" and again["jeton_refuse"]
    mcp_call("set_audit_parameters", values={"c6_degagements.tolerance_m": 0.08})
    run = mcp_call("run_audit", audit="c6_degagements", hypotheses_validees=h["jeton_hypotheses"])
    assert run["etape"] == "termine"
    assert any("Empiètement toléré" in m and "0.08" in m for m in run["audit"]["parametres_modifies"])
    assert run["livrable"]["fichier"].endswith(".pdf") and run["livrable"]["annexe_excel"]["fichier"].endswith(".xlsx")
    if run["audit"]["anomalies"]:  # fin d'audit : BCF ou Smart View proposés, jamais préparés
        assert run["proposition_bcf"]["appel"]["outil"] == "prepare_bcf_topics"
        assert run["proposition_smart_view"]["appel"] == {"outil": "prepare_smart_view", "arguments": {
            "check_id": run["audit"]["check_id"], "title": "Espaces libres et dégagements — anomalies"}}
        assert "BCF" in run["question_suite"] and "Smart View" in run["question_suite"]
    txt = _text(_file(run["livrable"]))
    assert "3. Hypothèses et paramètres" in txt and "0,08 m" in txt


def test_single_tools_use_saved_settings(mcp_call, fake_env, tmp_path, monkeypatch):
    """Un outil lancé seul applique les réglages enregistrés ; une valeur explicite l'emporte ; sans référentiel PMR,
    le contrôle le demande au lieu d'appliquer une valeur par défaut."""
    from bimdata_mcp import service_geometry
    from tests.ifc_factory import IfcBuilder

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.box("IfcWall", "Mur 1", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    b.box("IfcWall", "Mur 2", (3.996, 0, 0), (4, 0.2, 3), storey="RDC")  # 4 mm de recouvrement
    path = b.write(tmp_path / "t.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    base = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall", tol_h_m=0.0, tol_v_m=0.0)
    assert base["summary"]["non_conformities"] == 1  # tolérance nulle explicite : le recouvrement ressort
    mcp_call("set_audit_parameters", values={"c2_clash.tol_h_m": 0.01, "c2_clash.tol_v_m": 0.01})
    saved = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall")
    assert saved["summary"]["non_conformities"] == 0  # 4 mm < 10 mm enregistrés
    assert any("Réglages enregistrés" in w and "0.01" in w for w in saved["avertissements"])
    explicit = mcp_call("run_clash_check", set_a="classes:IfcWall", set_b="classes:IfcWall", tol_h_m=0.0, tol_v_m=0.0)
    assert explicit["summary"]["non_conformities"] == 1  # l'explicite l'emporte sur le réglage
    with pytest.raises(Exception, match="Référentiel PMR à choisir"):
        mcp_call("run_accessibility_check")
    mcp_call("set_audit_parameters", values={"c5_pmr.referentiel": "ERP 2017"})
    pmr = mcp_call("run_accessibility_check")
    assert pmr["referentiel"] == "ERP 2017"
    txt = _text(_file(saved["livrable"]))
    assert "Tolérance horizontale des conflits" in txt and "0,01 m" in txt  # hypothèse du rapport PDF


def test_reports_delivered_as_pdf_files_not_dead_links(fake_env, tmp_path, monkeypatch):
    """Le rapport PDF est joint au résultat de l'outil (ressource application/pdf), copié dans le dossier des rapports,
    et, en stdio (pas de serveur web), le lien pointe vers le fichier local."""
    import asyncio
    import base64
    from urllib.parse import unquote, urlparse

    from fastmcp import Client

    from bimdata_mcp import service_geometry
    from bimdata_mcp.server import mcp
    from tests.ifc_factory import IfcBuilder

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.box("IfcWall", "Mur", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    path = b.write(tmp_path / "d.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")

    async def go():
        async with Client(mcp) as c:
            await c.call_tool("set_active_model", {"bimdata_url": URL})
            return await c.call_tool("run_structure_check", {})

    res = asyncio.run(go())
    pdfs = [b for b in res.content if getattr(b, "type", "") == "resource"
            and b.resource.mimeType == "application/pdf"]
    assert len(pdfs) == 1 and base64.b64decode(pdfs[0].resource.blob)[:5] == b"%PDF-"
    lv = res.structured_content["livrable"]
    local = Path(lv["chemin_local"])
    assert local.exists() and local.parent == tmp_path / "rapports" and local.suffix == ".pdf"
    assert lv["download_url"].startswith("file://") and Path(unquote(urlparse(lv["download_url"]).path)).exists()
    xl = [b for b in res.content if getattr(b, "type", "") == "resource" and b.resource.mimeType.endswith(".sheet")]
    assert len(xl) == 1 and base64.b64decode(xl[0].resource.blob)[:2] == b"PK"  # classeur Excel joint aussi
    assert res.structured_content["rapports_joints"] == [Path(lv["fichier"]).name,
                                                          Path(lv["annexe_excel"]["fichier"]).name]
    assert Path(lv["annexe_excel"]["chemin_local"]).exists()


def test_insight_flags_doubtful_rules():
    """Cas type A2 / A4 : cloisons au contact (écarts de 5 cm), éviers hors champ, sens d'ouverture inconnu."""
    from bimdata_mcp.quality.insight import analyze, family

    assert family("Porte 90:NAD_P90_EI30:123456") == "NAD_P90" and family("Evier:NAD_EVIER 120:7") == "NAD_EVIER"
    issues = []
    for k in range(30):
        issues.append({"rule_id": "A2", "severity": "majeur", "uuid": f"d{k}", "name": f"Porte:NAD_P90:{k}",
                       "measure": {"value": 0.05, "unit": "m"}, "threshold": {"value": 0.01, "unit": "m"},
                       "elements": [{"uuid": f"d{k}", "role": "A"}, {"uuid": f"w{k}", "role": "B"}]})
    for k in range(12):
        issues.append({"rule_id": "A4", "severity": "majeur", "uuid": f"e{k}", "name": f"Evier:NAD_EVIER:{k}"})
    check = {"issues": issues, "per_requirement": [{"req_id": "A2", "label": "Zone de manœuvre", "applicable": 40},
                                                   {"req_id": "A4", "label": "Espace d'usage", "applicable": 200}],
             "notes": ["Sens d'ouverture non renseigné sur 459 portes : zone appliquée des deux côtés."]}
    names = {f"w{k}": ("IfcWall", "Cloison:NAD_CLOISON72:1") for k in range(30)}
    out = analyze(check, names)
    a2 = next(r for r in out["regles"] if r["regle"] == "A2")
    assert a2["verdict"] == "peu fiable" and len(a2["raisons"]) == 3  # taux, écarts faibles, obstacle dominant
    assert any("IfcWall NAD_CLOISON" in x for x in a2["raisons"])
    a4 = next(r for r in out["regles"] if r["regle"] == "A4")
    assert a4["verdict"] == "à confirmer" and "NAD_EVIER" in a4["raisons"][0]
    assert out["donnees_manquantes"] and "Ne pas envoyer en l'état en topics BCF les 42 anomalies" in \
        out["recommandation_bcf"]


def test_expert_analysis_on_top_of_pdf(mcp_call, fake_env, tmp_path, monkeypatch):
    from bimdata_mcp import service_geometry
    from tests.ifc_factory import IfcBuilder

    b = IfcBuilder()
    b.storey("RDC", 0.0)
    b.box("IfcWall", "Mur", (0, 0, 0), (4, 0.2, 3), storey="RDC")
    path = b.write(tmp_path / "e.ifc")
    monkeypatch.setattr(service_geometry, "fetch_model_ifc", lambda client, target, refresh=False: (
        path, {"file": path.name, "from_cache": True}))
    fake_env["model"]["document"] = {"file": "https://storage.test/model.ifc", "size": 50_000}
    mcp_call("set_active_model", bimdata_url=URL)
    out = mcp_call("run_structure_check")
    assert "analyse_automatique" in out
    with pytest.raises(Exception, match="Analyse vide"):
        mcp_call("annotate_audit_report", check_id=out["check_id"], analyse={})
    res = mcp_call("annotate_audit_report", check_id=out["check_id"], analyse={
        "a_retenir": "Le seul problème d'accessibilité sûr concerne les escaliers.",
        "fiabilite": [{"regle": "A2", "verdict": "peu fiable", "raisons": ["Tolérance de 1 cm : cloisons au contact."]}],
        "prerequis": [{"action": "Exporter le sens d'ouverture des portes", "responsable": "Architecte"}],
        "recommandation": "Ne pas envoyer ces anomalies en topics BCF en l'état."})
    lv = res["livrable"]
    assert lv["file_id"] != out["livrable"]["file_id"] and lv["annexe_excel"]["fichier"].endswith(".xlsx")
    txt = _text(_file(lv))
    pos = [txt.index(x) for x in ("L'essentiel", "Le seul problème d'accessibilité sûr", "peu fiable",
                                  "Exporter le sens d'ouverture des portes (Architecte)", "1. Synthèse")]
    assert pos == sorted(pos)  # l'analyse vient avant la synthèse chiffrée


def test_audit_files_published_to_ged_folder(mcp_call, fake_env, tmp_path, monkeypatch):
    """Après un audit : proposition de dépôt dans la GED (dossier par défaut nommé) ; plan montré, rien d'écrit avant
    accord ; dossiers créés au besoin, dossier existant réutilisé, pas de redépôt ; fichier local en stdio."""
    from tests.conftest import FakeClient

    mcp_call("set_active_model", bimdata_url=URL)
    tok = mcp_call("run_audit", audit="nommage")["jeton_hypotheses"]
    run = mcp_call("run_audit", audit="nommage", hypotheses_validees=tok)
    offer = run["proposition_ged"]
    assert offer["dossier_par_defaut"] == "Audits BIM/Maquette test"
    assert "« Audits BIM/Maquette test »" in offer["question"] and "autre dossier" in offer["question"]
    plan = mcp_call("prepare_ged_publication", **offer["appel"]["arguments"])
    assert plan["dossier"] == "Audits BIM/Maquette test" and not plan["dossier_existant"]
    assert [d.rsplit(".", 1)[1] for d in plan["documents"]] == ["pdf", "xlsx"]
    assert [i["action"] for i in plan["preview"]] == ["Créer le dossier", "Créer le dossier", "Déposer", "Déposer"]
    assert not FakeClient.ged_documents  # rien d'écrit avant accord
    res = mcp_call("apply_plan", plan_id=plan["plan_id"], confirm=True)
    assert res["failed"] == 0 and res["dossier"] == "Audits BIM/Maquette test"
    audits = next(f for f in FakeClient.ged_folders if f["name"] == "Audits BIM")
    sub = next(f for f in FakeClient.ged_folders if f["name"] == "Maquette test")
    assert audits["parent_id"] == 100 and sub["parent_id"] == audits["id"]
    assert {d["parent_id"] for d in FakeClient.ged_documents} == {sub["id"]} and len(FakeClient.ged_documents) == 2
    with pytest.raises(Exception, match="déjà présent"):  # même dossier, mêmes fichiers : rien à redéposer
        mcp_call("prepare_ged_publication", **offer["appel"]["arguments"])
    # autre dossier choisi par l'utilisateur, existant : réutilisé
    other = mcp_call("prepare_ged_publication", check_id=run["audit"]["check_id"], folder="03 - Synthèse")
    assert other["dossier_existant"] and [i["action"] for i in other["preview"]] == ["Déposer", "Déposer"]
    assert "03 - Synthèse" in mcp_call("list_ged_folders")["dossiers"]
    # tout document : fichier local (serveur sur le poste seulement)
    doc = tmp_path / "CR réunion.pdf"
    doc.write_bytes(b"%PDF-1.4 test")
    with pytest.raises(Exception, match="Chemin local refusé"):
        mcp_call("prepare_ged_publication", file_paths=[str(doc)], folder="03 - Synthèse")
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    p = mcp_call("prepare_ged_publication", file_paths=[str(doc)], folder="03 - Synthèse")
    mcp_call("apply_plan", plan_id=p["plan_id"], confirm=True)
    assert any(d["name"] == "CR réunion.pdf" and d["parent_id"] == 101 for d in FakeClient.ged_documents)
