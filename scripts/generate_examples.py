"""Génère des fichiers d'exemple (maquette synthétique) dans docs/exemples/."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "exemples"


def main() -> None:
    from tests import conftest

    class MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

        def setenv(self, k, v):
            os.environ[k] = v

    os.environ.pop("BIMDATA_MCP_TEST_SNAPSHOT", None)
    gen = conftest.fake_env.__wrapped__(Path(tempfile.mkdtemp()), MP())
    next(gen)
    call = conftest.mcp_call.__wrapped__(None)
    from bimdata_mcp.config import get_settings

    def path(meta):
        return next(get_settings().users_dir.glob(f"*/files/{meta['file_id']}/{meta['fichier']}"))

    OUT.mkdir(parents=True, exist_ok=True)
    call("set_active_model", bimdata_url="https://platform.bimdata.io/spaces/1/projects/2/viewer/3")
    call("select_elements", name="murs", criteria={"ifc_classes": ["IfcWall"]})
    shutil.copy(path(call("export_selection_excel", selection="murs")["livrable"]), OUT / "exemple_export_selection.xlsx")
    shutil.copy(path(call("generate_cobie")["livrable"]), OUT / "exemple_cobie.xlsx")
    tpl = call("requirements_template")
    shutil.copy(path(tpl["livrable"]), OUT / "gabarit_cahier_des_charges_donnees.xlsx")
    imp = call("import_data_requirements", file_id=tpl["livrable"]["file_id"])
    chk = call("run_requirements_check", requirements_id=imp["requirements_id"], phase="PRO", include_naming_rules=True)
    shutil.copy(path(chk["livrable"]), OUT / "exemple_rapport_controle.xlsx")
    print("ok :", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
