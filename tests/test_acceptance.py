"""Tests d'acceptation de la spécification « 7 contrôles type Solibri » sur la maquette de référence
(maquette d'un projet réel de référence, non publiée ; valeurs relevées le 17/11/2026).

Lancés seulement si la maquette est disponible :

    MCP_ACCEPTANCE_IFC=/chemin/maquette_reference.ifc \\
    MCP_ACCEPTANCE_SNAPSHOT=data/cache/snapshot_8a8d77108aa1da5e.json.gz pytest tests/test_acceptance.py

Le test du clash contre Solibri (Fluides × Structure à 5 % près) demande un export Solibri : il passe par
``compare_clash_results`` et n'est pas automatisé ici.
"""

from __future__ import annotations

import gzip
import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest

IFC = os.getenv("MCP_ACCEPTANCE_IFC")
SNAP = os.getenv("MCP_ACCEPTANCE_SNAPSHOT")
pytestmark = pytest.mark.skipif(not (IFC and SNAP and Path(IFC).exists() and Path(SNAP).exists()),
                                reason="maquette de référence absente (MCP_ACCEPTANCE_IFC / MCP_ACCEPTANCE_SNAPSHOT)")


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    from bim_core.model_snapshot import ModelSnapshot

    from bimdata_mcp.geometry.index import get_or_build
    from bimdata_mcp.model_view import records_by_uuid

    d = json.loads(gzip.decompress(Path(SNAP).read_bytes()))
    d.pop("_schema_version", None)
    snap = ModelSnapshot(**d).index()
    cache = Path(os.getenv("MCP_ACCEPTANCE_CACHE") or tmp_path_factory.mktemp("acc"))
    idx, _ = get_or_build(Path(IFC), cache, key="acceptance")
    labels = {s.get("uuid"): s.get("name") for s in snap.storeys or [] if s.get("uuid")}
    return idx, records_by_uuid(snap), labels


def _by_rule(issues):
    out: dict[str, list] = {}
    for i in issues:
        out.setdefault(i.req_id, []).append(i)
    return out


def test_structure_s2_s3_s7(model):
    from bimdata_mcp.quality.rules import run_structure_rules

    idx, recs, labels = model
    res, _ = run_structure_rules(idx, recs, checks=["S2", "S3", "S7"], displayed_storeys=labels)
    by = _by_rule(res.issues)
    s3 = " | ".join(i.message for i in by.get("S3", []))
    assert "Terrace" in s3 and "Terrece" in s3  # nom IFC différent du libellé affiché
    assert not any("vide" in i.message and "Terrace" in i.message for i in by.get("S3", []))
    assert not by.get("S2")
    # S7 : aucun attribut absent ; les 113 portes à NOTDEFINED sont signalées à part (statut « vide »), comme décidé
    assert not [i for i in by.get("S7", []) if i.status == "absent"]
    assert {i.status for i in by.get("S7", [])} <= {"vide"}


def test_g1_small_spaces_before_shaft_exclusion(model):
    from bimdata_mcp.quality.rules import run_geometry_rules

    idx, recs, labels = model
    res, _ = run_geometry_rules(idx, recs, checks=["G1"], thresholds={"shaft_pattern": r"(?!x)x"},
                                storey_labels=labels)
    small = {i.uuid for i in res.issues if i.message.startswith("Surface")}
    assert len(small) == 34


def test_g7_a5_risers(model):
    from bimdata_mcp.quality.lot3 import run_accessibility_rules
    from bimdata_mcp.quality.rules import run_geometry_rules

    idx, recs, labels = model
    g7, _ = run_geometry_rules(idx, recs, checks=["G7"], storey_labels=labels)
    a5, _, _ = run_accessibility_rules(idx, recs, referentiel="BHC 2015", thresholds=None, doors=[], spaces=[],
                                       checks=["A5"])
    for res in (g7, a5):
        stairs = {i.uuid for i in res.issues if re.match(r"hauteur de marche", i.message, re.I)}
        assert len(stairs) == 12


def test_g8_railings(model):
    from bimdata_mcp.quality.rules import run_geometry_rules

    idx, recs, labels = model
    res, _ = run_geometry_rules(idx, recs, checks=["G8"], storey_labels=labels)
    low = [i for i in res.issues if i.message.startswith("Hauteur")]
    named = [i for i in res.issues if i.message.startswith("Type") and "600" in i.message]
    assert len(low) == 10  # la spécification en annonçait 9 : 10 dans les données (validé)
    assert len(named) == 6


def test_a1_single_threshold(model):
    from bimdata_mcp.quality.lot3 import run_accessibility_rules

    idx, recs, _ = model
    doors = [g for g, r in recs.items() if r.ifc_class.startswith("IfcDoor")]
    res, _, _ = run_accessibility_rules(idx, recs, referentiel="BHC 2015", doors=doors, spaces=[], checks=["A1"],
                                        thresholds={"door_width_entry_m": 0.90, "door_width_interior_m": 0.90})
    assert len({i.uuid for i in res.issues if i.status != "exclu"}) == 104


def test_f1_no_fire_rating(model):
    from bimdata_mcp.quality.lot3 import run_fire_rules

    idx, recs, _ = model
    res, notes, _ = run_fire_rules(idx, recs, required_ratings=None, compartments=None, compartment_rating=None,
                                   door_rating=None, exits=[], stairs=[], max_distance_m=None, exit_up=1, stairs_up=1,
                                   checks=["F1"])
    text = " ".join(notes + [i.message for i in res.issues]).lower()
    assert "données absentes" in text and "firerating" in text


def test_list_audits_c7_partial(model):
    from types import SimpleNamespace

    from bim_core.model_snapshot import ModelSnapshot

    from bimdata_mcp.service_geometry import _audit_status

    d = json.loads(gzip.decompress(Path(SNAP).read_bytes()))
    d.pop("_schema_version", None)
    snap = ModelSnapshot(**d).index()
    call = SimpleNamespace(state=SimpleNamespace(list=lambda kind: []))
    st = _audit_status(call, snap, list(model[1].values()))
    assert st["c7_incendie"]["status"] == "partiel" and "FireRating" in st["c7_incendie"]["reason"]


def test_s4_identified_cases_present(model):
    """Les 25 vrais cas relevés à la main sont présents, quel que soit le total (les nouvelles règles peuvent en
    ajouter : réseaux, 5e plinthe…)."""
    from bimdata_mcp.quality.rules import run_structure_rules

    idx, recs, labels = model
    res, _ = run_structure_rules(idx, recs, checks=["S4"], displayed_storeys=labels)
    names = Counter(i.name.split(":")[0] for i in res.issues if i.ifc_class != "IfcBuildingStorey")
    expected = {"Electric Meter": 12, "Profil en relief": 4, "Jali On Exterior Window": 4,
                "Fan-Exhaust-CaptiveAire-Centrifugal_Roof_Upblast-CUBE": 3, "Cap": 1}
    for name, n in expected.items():
        got = sum(v for k, v in names.items() if k.startswith(name[:28]))
        assert got >= n, f"{name} : {got} < {n}"
    assert sum(v for k, v in names.items() if k.startswith("MECE-Daikin")) >= 1


def test_clash_matrix_snapshot(model):
    """Photographie de la matrice par défaut (pas une comparaison avec Solibri) : signale tout changement non voulu.
    Avec l'exception « isolant de réseau » : 524 → 513, 8 faux bloquants (gaine incluse dans son isolant ou doublée
    par lui) et 3 coudes contre l'isolant du tronçon voisin retirés. Triangulation hybride par défaut : 513 → 506
    (contacts à plat, instables avec l'une comme l'autre bibliothèque ; OpenCascade : 513, ARC-STR 421, FLU-ARC 23)."""
    from bimdata_mcp.quality.clash_check import DEFAULT_MATRIX, disciplines, parse_set, run_cells

    idx, recs, _ = model
    disc = disciplines(idx, recs)

    def resolve(spec):
        name = str(parse_set(spec)["discipline"]).capitalize()
        return [g for g, x in disc.items() if x == name]

    res, _, _ = run_cells(idx, recs, [dict(c) for c in DEFAULT_MATRIX], resolve=resolve, disc=disc)
    # Audit du 18/11 : doublons regroupés par groupe superposé (triplets d'eau glacée = 1 ligne), traversée
    # « incluse » du gaz 598082 retirée : 506 → 502 (OpenCascade : 513 → 509), 13 → 9 bloquants.
    hybrid = idx.meta.get("geometry_library", "").startswith("hybrid")
    assert len(res.issues) == (502 if hybrid else 509)
    assert Counter(i.cell for i in res.issues) == {"ARC-STR": 416 if hybrid else 421, "FLU-STR": 42,
                                                    "FLU-ARC": 21 if hybrid else 23, "FLU-FLU": 23}
    assert Counter(i.severity for i in res.issues)["bloquant"] == 9
    inclus = [i for i in res.issues if i.clash_type == "inclus"]
    assert len(inclus) == 2 and all("Chilled water" in idx.elements[i.uuid]["name"] for i in inclus)  # eau glacée 7 cm


def test_audit_18_11_grouping_and_rules(model):
    """Critères de réception de l'audit du 18/11/2026."""
    from bimdata_mcp.quality.lot3 import auto_exits, run_accessibility_rules, run_clearance_rules
    from bimdata_mcp.quality.rules import run_geometry_rules, run_structure_rules

    idx, recs, labels = model
    doors = [g for g, r in recs.items() if r.ifc_class.startswith("IfcDoor")]
    # A6 sans scope : une ligne au plus
    a6, _, _ = run_accessibility_rules(idx, recs, referentiel="ERP 2017", thresholds=None, doors=doors, spaces=[],
                                       checks=["A6"], scoped=False)
    assert len(a6.issues) <= 1
    # Dégagements : au plus une ligne par porte et par côté ; tolérance 5 cm
    dg, _ = run_clearance_rules(idx, recs, doors, obstacles=None, front_m=1.0, back_m=0.0, side_m=0.0, height_m=2.0,
                                width_m=None, use_opening_side=True)
    keys = [(i.uuid, i.measure["zone"]) for i in dg.issues]
    assert len(keys) == len(set(keys))
    # S7 : 113 portes, une ligne par type
    s7, _ = run_structure_rules(idx, recs, checks=["S7"], displayed_storeys=labels)
    assert sum(i.measure["value"] for i in s7.issues) == 113 and len(s7.issues) < 113
    assert len({i.name for i in s7.issues}) == len(s7.issues)
    # G4 : piles de compteurs = une ligne par emplacement ; G6 : chapes non modélisées en information
    g, _ = run_geometry_rules(idx, recs, checks=["G4", "G6"], storey_labels=labels)
    meters = [i for i in g.issues if i.req_id == "G4" and "Electric Meter" in i.message]
    assert 1 <= len(meters) <= 2 and sum(i.measure["value"] for i in meters) >= 13
    assert sum(1 for i in g.issues if i.req_id == "G6" and i.severity == "info") >= 17
    # Sorties : pas de porte de moins de 0,60 m ; escaliers ouverts 015 et 121 reliés au niveau d'accès
    exits, info = auto_exits(idx, recs)
    assert not [x for x in exits if "500 X 2134" in idx.elements[x]["name"]]
    assert {recs[x].attrs.get("Name") for x in info["escaliers_ouverts_relies"]} == {"015", "121"}


@pytest.mark.skipif(not os.getenv("MCP_ACCEPTANCE_DORTOIR2_M"),
                    reason="mesure à la main du chemin de Dortoir 2 à fournir (MCP_ACCEPTANCE_DORTOIR2_M, en m)")
def test_f3_dortoir2_calibration(model):
    """Calibrage de la distance de marche : chemin de « DORTOIR 2 » (1st Floor) mesuré dans la visionneuse, à 1 m
    près."""
    from bimdata_mcp.quality.lot3 import auto_exits, run_fire_rules

    idx, recs, _ = model
    exits, _ = auto_exits(idx, recs)
    res, _, _ = run_fire_rules(idx, recs, required_ratings=None, compartments=None, compartment_rating=None,
                               door_rating=None, exits=exits, stairs=[], max_distance_m=1.0, exit_up=1, stairs_up=1,
                               checks=["F3"])
    f3 = [i for i in res.issues if (recs[i.uuid].attrs.get("LongName") if i.uuid in recs else "") == "DORTOIR 2"
          and i.measure and "cheminement_m" in i.measure]
    assert len(f3) == 1
    assert f3[0].measure["cheminement_m"] == pytest.approx(float(os.environ["MCP_ACCEPTANCE_DORTOIR2_M"]), abs=1.0)


def test_bcf_viewpoints_on_reference_model(model, tmp_path):
    """Points de vue BCF de 10 conflits : schéma, sélection complète, caméra centrée, image PNG ≤ 1 500 px et < 500 ko,
    1 s par image en médiane."""
    import statistics
    import time

    import numpy as np

    from bimdata_mcp import bcf
    from bimdata_mcp.geometry.render import camera_frame, project, render_viewpoint
    from bimdata_mcp.quality.clash_check import DEFAULT_MATRIX, disciplines, parse_set, run_cells
    from bimdata_mcp.service_bcf import _elements, _storey_z

    idx, recs, _ = model
    disc = disciplines(idx, recs)
    res, _, _ = run_cells(idx, recs, [dict(c) for c in DEFAULT_MATRIX], disc=disc,
                          resolve=lambda s: [g for g, x in disc.items() if x == str(parse_set(s)["discipline"]).capitalize()])
    times = []
    for k, iss in enumerate(res.issues[:10]):
        i = iss.to_dict()
        els = _elements([i])
        zone = bcf.dilate(i["bbox"])
        vp = bcf.viewpoint(i, els, zone=zone, view="iso", section="box", storey_z=_storey_z(idx, i["uuid"]),
                           fov_deg=60, isolate=False, family="clash", model_id=3)
        cam = camera_frame(vp)
        c = (np.array(zone[:3]) + np.array(zone[3:])) / 2
        assert np.allclose(project(c[None], cam)[0][0], [640, 360], atol=1)
        assert len(vp["components"]["selection"]) == len({e["uuid"].split("#")[0] for e in els})
        t0 = time.time()
        r = render_viewpoint(idx, vp, {e["uuid"]: bcf.role_color(e["role"]) for e in els}, tmp_path / f"{k}.png",
                             region=zone, caption=bcf.caption(i))
        times.append(time.time() - t0)
        vp["snapshot"] = {"snapshot_type": "png", "snapshot_data": "x"}
        assert bcf.validate_viewpoint(vp) == [] and r["bytes"] < 500_000
    assert statistics.median(times) < 1.0
