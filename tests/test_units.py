from __future__ import annotations

import pytest

from bimdata_mcp.target import InvalidTargetError, parse_viewer_url


def test_viewer_urls():
    t = parse_viewer_url("https://platform.bimdata.io/spaces/12/projects/34/viewer/56?foo=1")
    assert (t.cloud_id, t.project_id, t.model_id) == ("12", "34", "56")
    t = parse_viewer_url("https://platform-staging.bimdata.io/spaces/1/projects/2/viewer?modelIds=9,10")
    assert t.model_id == "9"
    with pytest.raises(InvalidTargetError):
        parse_viewer_url("https://evil.example.com/spaces/1/projects/2/viewer/3")


def test_csv_requirements(tmp_path):
    from bimdata_mcp.quality.spec_parser import parse_requirements

    p = tmp_path / "cdc.csv"
    p.write_text("Classe IFC;Pset;Propriété;Obligatoire;Type de valeur;Valeurs autorisées\n"
                 "IfcDoor;Pset_DoorCommon;FireRating;Oui;Texte;EI30|EI60\n"
                 "IfcSpace;;LongName;Oui;Texte;\n", encoding="utf-8")
    r = parse_requirements(p)
    fields = [q["field"] for q in r["requirements"]]
    assert fields == ["Pset_DoorCommon.FireRating", "LongName"]
    assert r["requirements"][0]["allowed_values"] == ["EI30", "EI60"]


def test_download_and_upload_routes(fake_env):
    from starlette.testclient import TestClient

    from bimdata_mcp import files
    from bimdata_mcp.auth import Identity
    from bimdata_mcp.server import mcp
    from bimdata_mcp.state import UserState

    st = UserState(Identity("local", "local"))
    path = files.new_output_path(st, "rapport.xlsx")
    path.write_bytes(b"PK test")
    meta = files.register_file(st, path, kind="export")
    app = mcp.http_app()
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        url = meta["download_url"].replace("http://testserver", "")
        assert client.get(url).content == b"PK test"
        assert client.get(url.replace("sig=", "sig=x")).status_code == 403
        up = files.upload_link(st)["upload_url"].replace("http://testserver", "")
        assert "Déposer" in client.get(up).text
        r = client.post(up, files={"file": ("cdc.xlsx", b"PK data", "application/octet-stream")})
        assert r.status_code == 200 and "up_" in r.text
        bad = client.post(up, files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        assert bad.status_code == 400
    assert any(f["kind"] == "upload" for f in files.list_files(st))
