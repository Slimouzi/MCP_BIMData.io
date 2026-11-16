"""Rejoue le parcours complet sur un snapshot réel en cache (sans réseau).

Usage :
    python scripts/smoke_real_snapshot.py <snapshot.json.gz> [cahier_des_charges.xlsx] [phase]

Le snapshot est un fichier de cache ``bimdata-read`` (``snapshot_<clé>.json.gz``).
Les écritures sont simulées par le faux client des tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    snap, spec = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    phase = sys.argv[3] if len(sys.argv) > 3 else None
    os.environ.update({"BIMDATA_MCP_TEST_SNAPSHOT": snap, "MCP_DATA_DIR": tempfile.mkdtemp(),
                       "MCP_AUTH_MODE": "none", "MCP_FILE_SECRET": "smoke"})
    import pytest  # noqa: F401  (fixtures)

    from tests import conftest

    class MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

        def setenv(self, k, v):
            os.environ[k] = v

    out = Path(os.getenv("SMOKE_OUT") or tempfile.mkdtemp())
    out.mkdir(parents=True, exist_ok=True)
    gen = conftest.fake_env.__wrapped__(out, MP())
    print("sorties :", out)
    next(gen)
    call = conftest.mcp_call.__wrapped__(None)

    def step(label, tool, **kw):
        t = time.time()
        r = call(tool, **kw)
        print(f"[{time.time() - t:5.2f}s] {label}")
        return r

    r = step("set_active_model", "set_active_model", cloud_id=1, project_id=2, model_id=3)
    print("   ", r["model_name"], r["n_elements"], "éléments,", len(r["storeys"]), "étages")
    st = r["storeys"][len(r["storeys"]) // 2]["name"]
    s = step(f"select murs {st}", "select_elements", name="murs", criteria={"ifc_classes": ["IfcWall"], "storeys": [st]})
    print("   ", s["count"], s["by_ifc_class"])
    e = step("export all props", "export_selection_excel", selection="murs", properties="all")
    print("   ", e["synthese"], "→", e["livrable"]["fichier"], e["livrable"]["taille_ko"], "Ko")
    c = step("COBie maquette entière", "generate_cobie")
    print("   ", c["synthese"], c["livrable"]["fichier"])
    n = step("contrôle nommage/complétude", "check_naming_completeness")
    print("   ", n["summary"]["non_conformities"], "NC /", n["summary"]["checks"], "contrôles ; propositions:",
          n["summary"]["proposals"], "dont fiables:", n["summary"]["proposals_high_confidence"])
    sv = step("smart view du contrôle", "prepare_smart_view", title="Qualité", check_id=n["check_id"])
    print("   ", sv["summary"]["legend"])
    if spec:
        from bimdata_mcp import files
        from bimdata_mcp.auth import Identity

        meta = files.store_upload(Identity("local", "local").storage_key, Path(spec).name, Path(spec).read_bytes())
        i = step("import cahier des charges", "import_data_requirements", file_id=meta["file_id"])
        print("   ", i["n_requirements"], "exigences ; phases", i["phases_available"], "; avertissements", i["n_warnings"])
        k = step(f"contrôle CDC (phase {phase})", "run_requirements_check", requirements_id=i["requirements_id"], phase=phase)
        print("   ", k["summary"])
        for w in k["worst_requirements"][:6]:
            print("     -", w["req_id"], w["ifc_classes"][:40], w["field"], f"{w['non_conformes']}/{w['applicable']}")
        print("    rapport :", k["livrable"]["fichier"], k["livrable"]["taille_ko"], "Ko")
        b = step("BCF par exigence", "prepare_bcf_topics", title="CDC", check_id=k["check_id"], max_topics=500)
        print("   ", b["summary"])
    pc = step("plan corrections (propositions ≥ 0.8)", "prepare_corrections_from_check", check_id=n["check_id"])
    print("   ", pc.get("summary"), pc.get("risks", [])[:2])


if __name__ == "__main__":
    main()
