"""Audit spécifique selon le CCBIM : lecture du document, attendus validés, rapport PDF et classeur Excel."""

from __future__ import annotations

import pytest

URL = "https://platform.bimdata.io/spaces/1/projects/2/viewer/3"


def _docx(path):
    from docx import Document

    d = Document()
    d.add_heading("Cahier des charges BIM — Projet test", 1)
    d.add_paragraph("§3.1 Format des livrables : les maquettes sont livrées au format IFC4.")
    d.add_paragraph("§4.2 Nommage des niveaux : RDC, R+1, R+2… Les niveaux R+1 et R+2 sont attendus.")
    d.add_paragraph("§5.1 Propriétés : chaque mur porte la propriété Pset_WallCommon.FireRating renseignée.")
    d.add_paragraph("§6 Planning : la maquette est livrée à chaque phase (non contrôlable sur la maquette).")
    d.save(str(path))
    return path


def test_ccbim_audit_end_to_end(mcp_call, fake_env, tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from pypdf import PdfReader

    from bimdata_mcp.config import get_settings

    mcp_call("set_active_model", bimdata_url=URL)
    menu = mcp_call("list_audits")
    assert "cahier des charges BIM (CCBIM) ?" in menu["message_utilisateur"]
    with pytest.raises(Exception, match="read_ccbim"):
        mcp_call("run_audit", audit="ccbim")
    doc = _docx(tmp_path / "CCBIM.docx")
    with pytest.raises(Exception, match="Chemin local refusé"):  # serveur web : pas d'accès aux fichiers locaux
        mcp_call("read_ccbim", file_path=str(doc))
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    read = mcp_call("read_ccbim", file_path=str(doc))
    txt = " ".join(p["texte"] for p in read["texte"])
    assert "IFC4" in txt and read["page_suivante"] is None
    assert any("FireRating" in p["texte"] for p in read["passages_cles"])
    # attendus extraits par l'assistant, avec leur source
    exp = mcp_call("set_ccbim_expectations", ccbim_id=read["ccbim_id"],
                   requirements=[{"label": "Résistance au feu des murs", "ifc_classes": ["IfcWall"],
                                  "field": "Pset_WallCommon.FireRating", "severity": "majeur", "source": "§5.1"}],
                   model_checks={"ifc_schema": "IFC4", "storey_names": ["R+1", "R+2", "R+3"]},
                   sources={"ifc_schema": "§3.1", "storey_names": "§4.2"},
                   not_checkable=[{"exigence": "Livraison à chaque phase", "source": "§6", "raison": "planning"}])
    assert exp["exigences_donnees"] == 1 and exp["non_controlables"] == 1
    assert {a["source"] for a in exp["attendus"]} == {"§5.1", "§3.1", "§4.2"}
    st = {a["id"]: a for a in mcp_call("list_audits")["audits"]}["ccbim"]
    assert st["status"] == "pret" and "CCBIM.docx" in st["reason"]
    hyp = mcp_call("run_audit", audit="ccbim")
    assert hyp["etape"] == "validation_hypotheses" and hyp["jeton_hypotheses"]
    assert hyp["non_controlables"][0]["source"] == "§6" and "CCBIM.docx" in hyp["references"][0]
    run = mcp_call("run_audit", audit="ccbim", hypotheses_validees=hyp["jeton_hypotheses"])
    assert run["etape"] == "termine" and run["audit"]["anomalies"] >= 2
    lv = run["livrable"]
    assert lv["fichier"].endswith(".pdf") and lv["annexe_excel"]["fichier"].endswith(".xlsx")
    pdf = next(get_settings().users_dir.glob(f"*/files/{lv['file_id']}/{lv['fichier']}"))
    text = " ".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages)
    assert "CCBIM.docx" in text and "Niveaux attendus" in text and "§4.2" in text
    xl = next(get_settings().users_dir.glob(f"*/files/{lv['annexe_excel']['file_id']}/{lv['annexe_excel']['fichier']}"))
    wb = load_workbook(xl)
    assert "Attendus du CCBIM" in wb.sheetnames
    vals = [c.value for row in wb["Attendus du CCBIM"].iter_rows() for c in row]
    assert any(isinstance(v, str) and v.startswith("Non contrôlable") for v in vals)
    # attendus modifiés après présentation : le jeton n'est plus valable
    mcp_call("set_ccbim_expectations", ccbim_id=read["ccbim_id"], model_checks={"ifc_schema": "IFC2X3"})
    again = mcp_call("run_audit", audit="ccbim", hypotheses_validees=hyp["jeton_hypotheses"])
    assert again["etape"] == "validation_hypotheses" and again["jeton_refuse"]
