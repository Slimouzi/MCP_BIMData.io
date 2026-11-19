"""Index géométrique d'une maquette : une boîte (AABB + OBB) et un maillage par élément.

Calculé une fois par version de fichier IFC (clé : maquette + empreinte SHA-1 du
fichier), puis relu depuis le disque :

- ``elements.sqlite`` : une ligne par élément (classe, étage, AABB, OBB, volume,
  maillage fermé, axes locaux) + table R*Tree sur les AABB pour les requêtes par boîte ;
- ``meshes.npz`` : sommets et triangles de tous les éléments, chargés à la demande ;
- ``ifc.json`` : données IFC lues pendant la même ouverture du fichier (étages, site,
  géoréférencement, contenance spatiale, réservations, sens d'ouverture des portes,
  GlobalId en double) — ce qu'exigent les règles de structuration S1…S10.

Toutes les longueurs sont en mètres, dans le repère projet (coordonnées monde de
l'IFC, placement du site compris). Une AABB seule ne prouve jamais un conflit : elle
sert à trouver les candidats ; le maillage conclut.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import mesh as M

INDEX_VERSION = 6  # 3 : CGAL hybride ; 4 : agrégats depuis leurs parties ; 5 : clé #id pour les GlobalId en double ;
#                    6 : hybride par défaut (index recalculés)

#: Classes physiques non indexées : réservations, éléments virtuels.
_NOT_PHYSICAL = ("IfcFeatureElement", "IfcVirtualElement")


def _require_ifcopenshell():
    try:
        import ifcopenshell  # noqa: F401
        import ifcopenshell.geom  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dépendance d'installation
        raise ValueError("IfcOpenShell n'est pas installé sur ce serveur (pip install ifcopenshell) : "
                           "les contrôles géométriques sont indisponibles.") from exc


def is_physical(el) -> bool:
    return el.is_a("IfcElement") and not any(el.is_a(c) for c in _NOT_PHYSICAL)


# ── Lecture des données IFC (hors géométrie) ─────────────────────────────


def _spatial_parent(el):
    """Conteneur spatial effectif : contenance directe, sinon via l'agrégat parent
    (volée → escalier → étage ; local → étage ; élément contenu dans un local → étage)."""
    import ifcopenshell.util.element as ue

    cur = el
    for _ in range(12):
        nxt = ue.get_container(cur) or ue.get_aggregate(cur)
        if nxt is None:
            return None
        if any(nxt.is_a(c) for c in ("IfcBuildingStorey", "IfcBuilding", "IfcSite", "IfcProject")):
            return nxt
        cur = nxt
    return None


def _safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception:  # noqa: BLE001 — relation absente ou schéma sans l'entité
        return None


def _placement_matrix(product, scale: float) -> np.ndarray | None:
    import ifcopenshell.util.placement as up

    if not getattr(product, "ObjectPlacement", None):
        return None
    try:
        m = np.array(up.get_local_placement(product.ObjectPlacement), dtype=float)
    except Exception:  # noqa: BLE001
        return None
    m[:3, 3] *= scale
    return m


def _axes(m: np.ndarray | None) -> dict | None:
    if m is None:
        return None
    def unit(v):
        n = np.linalg.norm(v)
        return [round(float(x), 6) for x in (v / n if n else v)]
    return {"origin": [round(float(x), 6) for x in m[:3, 3]], "x": unit(m[:3, 0]), "y": unit(m[:3, 1]),
            "z": unit(m[:3, 2])}


def _door_operation(door) -> dict:
    import ifcopenshell.util.element as ue

    t = ue.get_type(door)
    panels = []
    for p in (getattr(t, "HasPropertySets", None) or []):
        if p.is_a("IfcDoorPanelProperties"):
            panels.append(p.PanelOperation)
    return {"occurrence": getattr(door, "OperationType", None), "type": getattr(t, "OperationType", None),
            "type_name": getattr(t, "Name", None), "panels": panels}


def _georef(f, scale: float) -> dict:
    out: dict[str, Any] = {"map_conversion": None, "sites": []}
    for s in f.by_type("IfcSite"):
        out["sites"].append({"guid": s.GlobalId, "name": s.Name, "lat": s.RefLatitude, "lon": s.RefLongitude,
                             "elevation": s.RefElevation * scale if s.RefElevation is not None else None})
    try:
        convs = f.by_type("IfcMapConversion")
    except RuntimeError:  # IFC2X3 : entité absente du schéma
        convs = []
    if convs:
        c = convs[0]
        crs = c.TargetCRS
        out["map_conversion"] = {
            "eastings": c.Eastings * scale if c.Eastings is not None else None,
            "northings": c.Northings * scale if c.Northings is not None else None,
            "orthogonal_height": c.OrthogonalHeight * scale if c.OrthogonalHeight is not None else None,
            "x_axis_abscissa": c.XAxisAbscissa, "x_axis_ordinate": c.XAxisOrdinate, "scale": c.Scale,
            "crs": getattr(crs, "Name", None), "geodetic_datum": getattr(crs, "GeodeticDatum", None),
        }
    return out


def key_of(guid: str, step_id: int) -> str:
    return f"{guid}#{step_id}"


def make_keyer(f):
    """Clé d'un élément dans l'index : son GlobalId s'il est unique dans le fichier, sinon « GlobalId#idSTEP » pour
    chaque occurrence (les doublons ne s'écrasent pas l'un l'autre)."""
    counts = Counter(e.GlobalId for e in f.by_type("IfcRoot"))
    dup = {g for g, n in counts.items() if n > 1}

    def key(el):
        if el is None:
            return None
        return key_of(el.GlobalId, el.id()) if el.GlobalId in dup else el.GlobalId

    return key, counts, dup


def _read_ifc_data(f, scale: float, products: list) -> tuple[dict, dict[str, dict]]:
    """Données IFC globales + par élément (conteneur, agrégat, type, porte, réservation), par clé d'élément."""
    import ifcopenshell.util.element as ue
    import ifcopenshell.util.system as usys

    key, guids, dup = make_keyer(f)
    per: dict[str, dict] = {}
    for el in products:
        sp = _spatial_parent(el)
        parent = ue.get_aggregate(el)
        t = ue.get_type(el)
        per[key(el)] = {
            "global_id": el.GlobalId, "step_id": el.id(),
            "ifc_class": el.is_a(), "name": el.Name or "", "container": key(sp) if sp else None,
            "container_class": sp.is_a() if sp else None,
            "storey": key(sp) if sp is not None and sp.is_a("IfcBuildingStorey") else None,
            "parent": key(parent) if parent is not None and not parent.is_a("IfcSpatialStructureElement")
            and not parent.is_a("IfcProject") else None,
            "type_name": getattr(t, "Name", None) or getattr(el, "ObjectType", None) or "",
            "has_representation": bool(getattr(el, "Representation", None)),
            "predefined": _safe_call(ue.get_predefined_type, el),
            "systems": sorted({x.Name or x.GlobalId for x in (_safe_call(usys.get_element_systems, el) or [])}),
            "layers": sorted({x.Name or "" for x in (_safe_call(ue.get_layers, f, el) or [])} - {""}),
        }
    storeys = []
    for s in f.by_type("IfcBuildingStorey"):
        m = _placement_matrix(s, scale)
        contained = [e for r in (s.ContainsElements or []) for e in r.RelatedElements]
        storeys.append({
            "guid": key(s), "name": s.Name or "", "long_name": s.LongName or "",
            "elevation": s.Elevation * scale if s.Elevation is not None else None,
            "z": float(m[2, 3]) if m is not None else None,
            "building": key(ue.get_aggregate(s)),
            "n_contained": len(contained),
            "contained_by_class": dict(Counter(e.is_a() for e in contained).most_common()),
        })
    fills = {key(r.RelatedBuildingElement): key(r.RelatingOpeningElement) for r in f.by_type("IfcRelFillsElement")}
    voids = {key(r.RelatedOpeningElement): key(r.RelatingBuildingElement) for r in f.by_type("IfcRelVoidsElement")}
    dup_occ: dict[str, list[dict]] = {}
    for e in f.by_type("IfcRoot"):
        if e.GlobalId in dup:
            dup_occ.setdefault(e.GlobalId, []).append({"key": key(e), "step_id": e.id(), "ifc_class": e.is_a(),
                                                       "name": getattr(e, "Name", None) or ""})
    data = {
        "schema": f.schema, "unit_scale": scale,
        "n_sites": len(f.by_type("IfcSite")),
        "buildings": [{"guid": key(b), "name": b.Name or ""} for b in f.by_type("IfcBuilding")],
        "storeys": storeys,
        "fills": fills, "voids": voids,
        "doors": {key(d): _door_operation(d) for d in f.by_type("IfcDoor")},
        "duplicate_guids": {g: guids[g] for g in dup},
        "duplicate_occurrences": dup_occ,
        **_georef(f, scale),
    }
    return data, per


# ── Construction ─────────────────────────────────────────────────────────


#: Bibliothèque de repli, stable : utilisée quand la bibliothèque choisie fait planter le processus de calcul.
STABLE_LIBRARY = "opencascade"
#: Bibliothèque par défaut : 4 à 8 fois plus rapide et 7 fois moins gourmande en mémoire qu'OpenCascade.
DEFAULT_LIBRARY = "hybrid-cgal-simple-opencascade"


def geometry_library() -> str:
    """Bibliothèque de triangulation (MCP_GEOMETRY_LIBRARY). Défaut : hybride CGAL, repli automatique OpenCascade.

    Hybride : plantait par intermittence avec IfcOpenShell 0.8.3, plus avec 0.8.5 (60 calculs sur 60). Comparé à
    OpenCascade sur trois maquettes (tous contrôles) : boîtes à 5 mm près (sauf 5 B-rep ouverts sur 9 948), règles à
    0 à 0,4 % près, conflits à 0 à 3,7 % près — écarts tous sur des contacts à plat ou des maillages ouverts,
    instables avec l'une comme l'autre. ``MCP_GEOMETRY_LIBRARY=opencascade`` pour revenir à OpenCascade.
    """
    return os.getenv("MCP_GEOMETRY_LIBRARY", "") or DEFAULT_LIBRARY


# ── Budget mémoire ───────────────────────────────────────────────────────
#: Pic estimé (Mo) = ouverture du fichier (≈ 7 à 12 × sa taille) + triangulation (≈ 0,6 Go, + ≈ 0,1 Go par fil).
#: Calibré sur trois maquettes (28, 32 et 168 Mo) avec OpenCascade ; volontairement prudent.
MEM_PER_FILE_MB = 12.0
MEM_ITER_BASE_MB = 600.0
MEM_PER_THREAD_MB = 110.0


def memory_limit_mb() -> float:
    """Mémoire allouée au calcul de l'index (MCP_GEOMETRY_MAX_MEMORY_MB ; défaut : 75 % de la mémoire physique)."""
    raw = os.getenv("MCP_GEOMETRY_MAX_MEMORY_MB", "")
    if raw:
        return float(raw)
    try:
        return 0.75 * os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**20
    except (ValueError, OSError, AttributeError):  # pragma: no cover - plateforme sans sysconf
        return 8192.0


#: Hybride : triangulation bien plus sobre (pics mesurés à 8 fils : 549 Mo pour 28 Mo d'IFC, 1 506 Mo pour 168 Mo,
#: contre 1 566 et 1 596 Mo avec OpenCascade) ; l'ouverture du fichier domine.
MEM_ITER_BASE_MB_HYBRID = 400.0
MEM_PER_THREAD_MB_HYBRID = 20.0


def memory_estimate_mb(size_mb: float, threads: int, library: str | None = None) -> float:
    hybrid = (library or geometry_library()).startswith("hybrid")
    base, per = (MEM_ITER_BASE_MB_HYBRID, MEM_PER_THREAD_MB_HYBRID) if hybrid else (MEM_ITER_BASE_MB, MEM_PER_THREAD_MB)
    return MEM_PER_FILE_MB * size_mb + base + per * max(threads - 1, 0)


def plan_threads(size_mb: float, wanted: int, limit_mb: float, library: str | None = None) -> int:
    """Plus grand nombre de fils (≤ ``wanted``) qui tient dans ``limit_mb`` ; lève ValueError si aucun ne tient."""
    for t in range(max(wanted, 1), 0, -1):
        if memory_estimate_mb(size_mb, t, library) <= limit_mb:
            return t
    est = memory_estimate_mb(size_mb, 1, library)
    raise ValueError(
        f"Fichier IFC de {size_mb:.0f} Mo : calcul de l'index géométrique estimé à ≈ {est / 1024:.1f} Go de mémoire, "
        f"au-delà de la mémoire allouée ({limit_mb / 1024:.1f} Go, MCP_GEOMETRY_MAX_MEMORY_MB). Augmenter la mémoire "
        "du serveur ou cette limite, ou découper la maquette (par bâtiment ou par discipline).")


def _wanted_threads() -> int:
    return max(1, min(multiprocessing.cpu_count(), int(os.getenv("MCP_GEOMETRY_THREADS", "0") or 0) or 8))


def _rss_mb(pid: int) -> float:
    """Mémoire résidente d'un processus (Mo) : /proc sous Linux, ``ps`` ailleurs ; 0 si illisible."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip() or 0) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def build_isolated(ifc_path: Path, root: Path, *, key: str) -> GeometryIndex:
    """Calcul de l'index dans un processus séparé : un plantage natif de la triangulation n'emporte pas le serveur,
    et la mémoire du calcul est rendue dès la fin. Si la bibliothèque choisie plante, reprise avec OpenCascade.
    MCP_GEOMETRY_SUBPROCESS=0 : calcul dans le processus courant (tests, débogage)."""
    size_mb = ifc_path.stat().st_size / 2**20
    limit = memory_limit_mb()
    threads = plan_threads(size_mb, _wanted_threads(), limit)  # lève un message clair si rien ne tient
    budget = {"threads": threads, "memory_limit_mb": round(limit), "memory_estimate_mb": round(
        memory_estimate_mb(size_mb, threads))}
    if os.getenv("MCP_GEOMETRY_SUBPROCESS", "1").strip().lower() in ("0", "false", "non", "no"):
        prev = os.environ.get("MCP_GEOMETRY_THREADS")
        os.environ["MCP_GEOMETRY_THREADS"] = str(threads)
        try:
            idx = build(ifc_path, root, key=key)
        finally:
            if prev is None:
                os.environ.pop("MCP_GEOMETRY_THREADS", None)
            else:
                os.environ["MCP_GEOMETRY_THREADS"] = prev
        return _with_meta(root, idx, budget)
    import subprocess
    import sys

    tried = []
    for lib in dict.fromkeys([geometry_library(), STABLE_LIBRARY]):
        (root / "meta.json").unlink(missing_ok=True)
        if lib != geometry_library():  # repli OpenCascade, plus gourmand : budget recalculé
            try:
                threads = plan_threads(size_mb, _wanted_threads(), limit, lib)
            except ValueError as exc:
                tried.append(f"{lib} (non lancé : {exc})")
                break
            budget = {**budget, "threads": threads, "memory_estimate_mb": round(memory_estimate_mb(size_mb, threads, lib))}
        env = {**os.environ, "MCP_GEOMETRY_THREADS": str(threads)}
        proc = subprocess.Popen([sys.executable, "-m", "bimdata_mcp.geometry.build_worker", str(ifc_path), str(root),
                                 key, lib], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
        peak, killed = 0.0, False
        while proc.poll() is None:  # surveillance de la mémoire : arrêt net plutôt qu'un plantage du serveur
            peak = max(peak, _rss_mb(proc.pid))
            if peak > limit:
                proc.kill()
                killed = True
                break
            time.sleep(0.2)
        _, stderr = proc.communicate()
        if killed:
            raise ValueError(
                f"Calcul de l'index géométrique arrêté : {peak / 1024:.1f} Go de mémoire utilisés, au-delà de la "
                f"mémoire allouée ({limit / 1024:.1f} Go, MCP_GEOMETRY_MAX_MEMORY_MB) pour un fichier IFC de "
                f"{size_mb:.0f} Mo. Augmenter la mémoire du serveur ou cette limite, ou découper la maquette.")
        err = (stderr or "").strip().splitlines()[-1:] or [""]
        tried.append(f"{lib} (code {proc.returncode}{' : ' + err[0][:160] if proc.returncode else ''})")
        if proc.returncode == 0 and (root / "meta.json").exists():
            idx = GeometryIndex.load(root)
            extra = {**budget, "memory_peak_mb": round(peak)}
            if lib != geometry_library():
                extra["fallback"] = f"{geometry_library()} a échoué ; index calculé avec {lib}"
            return _with_meta(root, idx, extra)
    raise ValueError("Échec du calcul de l'index géométrique : " + " ; ".join(tried)
                     + " — fichier IFC peut-être illisible par IfcOpenShell.")


def _iter_shapes(f, products: list):
    import ifcopenshell.geom

    s = ifcopenshell.geom.settings()
    s.set("use-world-coords", True)
    s.set("weld-vertices", True)
    n_proc = _wanted_threads()
    try:
        it = ifcopenshell.geom.iterator(s, f, n_proc, include=products, geometry_library=geometry_library())
    except TypeError:  # IfcOpenShell sans choix de bibliothèque
        it = ifcopenshell.geom.iterator(s, f, n_proc, include=products)
    if not it.initialize():
        return
    while True:
        sh = it.get()
        yield int(sh.id), np.asarray(sh.geometry.verts, float).reshape(-1, 3), \
            np.asarray(sh.geometry.faces, np.int64).reshape(-1, 3)
        if not it.next():
            break


def _unit_check(f, scale: float, rows: dict[str, dict]) -> dict:
    """Hauteur IFC déclarée (OverallHeight des portes/fenêtres) contre la hauteur mesurée."""
    ratios = []
    by_step = {r.get("step_id"): r for r in rows.values()}
    for cls in ("IfcDoor", "IfcWindow"):
        for el in f.by_type(cls):
            h = getattr(el, "OverallHeight", None)
            r = by_step.get(el.id())
            if h and r and r.get("obb"):
                measured = 2 * r["obb"]["half"][2]
                if measured > 0:
                    ratios.append(h * scale / measured)
    if not ratios:
        return {"status": "non_verifiable", "detail": "aucune porte ni fenêtre avec OverallHeight"}
    med = statistics.median(ratios)
    ok = 0.8 <= med <= 1.25
    return {"status": "ok" if ok else "ecart", "median_ratio": round(med, 3), "samples": len(ratios),
            "detail": "hauteur déclarée / hauteur mesurée (portes et fenêtres)"}


def build(ifc_path: Path, root: Path, *, key: str) -> GeometryIndex:
    _require_ifcopenshell()
    import ifcopenshell
    import ifcopenshell.util.unit as uu

    from .source import file_sha1

    t0 = time.time()
    f = ifcopenshell.open(str(ifc_path))
    scale = float(uu.calculate_unit_scale(f))
    products = [e for e in f.by_type("IfcElement") if is_physical(e)] + list(f.by_type("IfcSpace"))
    ifc_data, per = _read_ifc_data(f, scale, products)
    t_data = time.time() - t0

    verts_all, faces_all, vofs, fofs, mesh_guids = [], [], [0], [0], []
    rows: dict[str, dict] = {g: {**d, "has_geometry": False} for g, d in per.items()}
    key_by_step = {d["step_id"]: k for k, d in per.items()}
    for step_id, v, fc in _iter_shapes(f, products) if products else []:
        guid = key_by_step.get(step_id)
        r = rows.get(guid)
        if r is None or len(v) == 0 or len(fc) == 0:
            continue
        r.update(has_geometry=True, aabb=M.aabb(v), obb=M.obb_vertical(v), volume=M.volume(v, fc),
                 is_closed=M.is_closed(v, fc), mesh_idx=len(mesh_guids))
        if r["ifc_class"] == "IfcSpace":
            r["floor_area"] = M.horizontal_area_up(v, fc)
        mesh_guids.append(guid)
        verts_all.append(v)
        faces_all.append(fc.astype(np.int32))
        vofs.append(vofs[-1] + len(v))
        fofs.append(fofs[-1] + len(fc))
    # Agrégats sans géométrie propre (mur en couches IfcBuildingElementPart, escalier en volées…) : géométrie
    # réunie de leurs parties, marquée from_parts — vue par les règles « élément » (S4, G5, G6, dégagements, F2),
    # ignorée par le clash et les doublons, qui testent les parties.
    children: dict[str, list[str]] = {}
    for guid, r in rows.items():
        if r.get("parent"):
            children.setdefault(r["parent"], []).append(guid)
    for _ in range(4):  # agrégats imbriqués
        changed = False
        for pg, kids in children.items():
            pr = rows.get(pg)
            ks = [k for k in kids if rows[k]["has_geometry"]]
            if pr is None or pr["has_geometry"] or not ks:
                continue
            vs = [verts_all[rows[k]["mesh_idx"]] for k in ks]
            fs, off = [], 0
            for k, v in zip(ks, vs, strict=True):
                fs.append(faces_all[rows[k]["mesh_idx"]] + off)
                off += len(v)
            v, fc = np.concatenate(vs), np.concatenate(fs).astype(np.int32)
            pr.update(has_geometry=True, from_parts=True, aabb=M.aabb(v), obb=M.obb_vertical(v),
                      volume=sum(rows[k].get("volume") or 0.0 for k in ks),
                      is_closed=all(rows[k].get("is_closed") for k in ks), mesh_idx=len(mesh_guids))
            mesh_guids.append(pg)
            verts_all.append(v)
            faces_all.append(fc)
            vofs.append(vofs[-1] + len(v))
            fofs.append(fofs[-1] + len(fc))
            changed = True
        if not changed:
            break
    for r in rows.values():
        el = f.by_id(r["step_id"])
        r["axes"] = _axes(_placement_matrix(el, scale))
        if not r["has_geometry"]:
            r["reason"] = "représentation non triangulable" if r["has_representation"] else "aucune représentation"
    t_geom = time.time() - t0 - t_data

    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "elements.sqlite.tmp"
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    _write_sqlite(con, rows)
    con.close()
    tmp.replace(root / "elements.sqlite")
    np.savez_compressed(root / "meshes.tmp.npz",
                        verts=np.concatenate(verts_all) if verts_all else np.zeros((0, 3)),
                        faces=np.concatenate(faces_all) if faces_all else np.zeros((0, 3), np.int32),
                        vofs=np.asarray(vofs, np.int64), fofs=np.asarray(fofs, np.int64),
                        guids=np.asarray(mesh_guids))
    (root / "meshes.tmp.npz").replace(root / "meshes.npz")
    n_phys = sum(1 for r in rows.values() if r["ifc_class"] != "IfcSpace")
    meta = {
        "version": INDEX_VERSION, "key": key, "ifc_file": ifc_path.name, "ifc_sha1": file_sha1(ifc_path),
        "ifc_size_mb": round(ifc_path.stat().st_size / 2**20, 1), "schema": f.schema,
        "unit_scale": scale, "unit": "m", "unit_check": _unit_check(f, scale, rows),
        "geometry_library": geometry_library(),
        "n_elements": n_phys, "n_spaces": len(rows) - n_phys,
        "n_indexed": sum(1 for r in rows.values() if r["has_geometry"]),
        "n_without_geometry": sum(1 for r in rows.values() if not r["has_geometry"] and r["ifc_class"] != "IfcSpace"),
        "n_from_parts": sum(1 for r in rows.values() if r.get("from_parts")),
        "durations_s": {"donnees_ifc": round(t_data, 1), "geometrie": round(t_geom, 1),
                        "total": round(time.time() - t0, 1)},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (root / "ifc.json").write_text(json.dumps(ifc_data, ensure_ascii=False, default=str))
    (root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))  # écrit en dernier : index complet
    return GeometryIndex.load(root)


_COLS = ("guid", "ifc_class", "name", "storey", "container", "container_class", "parent", "type_name",
         "has_geometry", "reason", "xmin", "ymin", "zmin", "xmax", "ymax", "zmax", "obb", "volume", "is_closed",
         "axes", "mesh_idx", "floor_area", "predefined", "systems", "layers", "from_parts", "global_id", "step_id")


def _write_sqlite(con: sqlite3.Connection, rows: dict[str, dict]) -> None:
    con.execute(f"CREATE TABLE elements (id INTEGER PRIMARY KEY, {', '.join(c for c in _COLS)})")
    con.execute("CREATE UNIQUE INDEX elements_guid ON elements(guid)")
    try:
        con.execute("CREATE VIRTUAL TABLE elements_rtree USING rtree(id, xmin, xmax, ymin, ymax, zmin, zmax)")
        rtree = True
    except sqlite3.OperationalError:
        rtree = False
    for i, (guid, r) in enumerate(rows.items(), start=1):
        bb = r.get("aabb") or [None] * 6
        con.execute(f"INSERT INTO elements VALUES ({', '.join('?' * (len(_COLS) + 1))})", (
            i, guid, r["ifc_class"], r["name"], r["storey"], r["container"], r["container_class"], r["parent"],
            r["type_name"], int(r["has_geometry"]), r.get("reason"), *bb,
            json.dumps(r.get("obb")) if r.get("obb") else None, r.get("volume"),
            None if r.get("is_closed") is None else int(r["is_closed"]),
            json.dumps(r.get("axes")) if r.get("axes") else None, r.get("mesh_idx"), r.get("floor_area"),
            r.get("predefined"), json.dumps(r.get("systems") or []), json.dumps(r.get("layers") or []),
            int(bool(r.get("from_parts"))), r.get("global_id") or guid, r.get("step_id")))
        if rtree and r.get("aabb"):
            con.execute("INSERT INTO elements_rtree VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (i, bb[0], bb[3], bb[1], bb[4], bb[2], bb[5]))
    con.commit()


# ── Lecture ──────────────────────────────────────────────────────────────


@dataclass
class GeometryIndex:
    root: Path
    meta: dict
    ifc: dict
    elements: dict[str, dict] = field(default_factory=dict)
    _meshes: Any = None

    @classmethod
    def load(cls, root: Path) -> GeometryIndex:
        meta = json.loads((root / "meta.json").read_text())
        ifc = json.loads((root / "ifc.json").read_text())
        con = sqlite3.connect(root / "elements.sqlite")
        con.row_factory = sqlite3.Row
        els = {}
        for row in con.execute("SELECT * FROM elements"):
            d = dict(row)
            d["has_geometry"] = bool(d["has_geometry"])
            d["is_closed"] = None if d["is_closed"] is None else bool(d["is_closed"])
            d["obb"] = json.loads(d["obb"]) if d["obb"] else None
            d["axes"] = json.loads(d["axes"]) if d["axes"] else None
            d["from_parts"] = bool(d.get("from_parts"))
            d["systems"] = json.loads(d["systems"] or "[]")
            d["layers"] = json.loads(d["layers"] or "[]")
            d["aabb"] = [d.pop(k) for k in ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")]
            if d["aabb"][0] is None:
                d["aabb"] = None
            els[d["guid"]] = d
        con.close()
        return cls(root=root, meta=meta, ifc=ifc, elements=els)

    def query_box(self, box: list[float], *, tol: float = 0.0) -> list[str]:
        """GlobalId des éléments dont l'AABB touche ``box`` (xmin, ymin, zmin, xmax, ymax, zmax)."""
        con = sqlite3.connect(self.root / "elements.sqlite")
        try:
            q = ("SELECT e.guid FROM elements_rtree r JOIN elements e ON e.id = r.id WHERE r.xmax >= ? AND "
                 "r.xmin <= ? AND r.ymax >= ? AND r.ymin <= ? AND r.zmax >= ? AND r.zmin <= ?")
            args = (box[0] - tol, box[3] + tol, box[1] - tol, box[4] + tol, box[2] - tol, box[5] + tol)
            return [r[0] for r in con.execute(q, args)]
        except sqlite3.OperationalError:  # pas de module R*Tree : balayage
            return [g for g, e in self.elements.items() if e["aabb"] and all(
                e["aabb"][i] <= box[i + 3] + tol and e["aabb"][i + 3] >= box[i] - tol for i in range(3))]
        finally:
            con.close()

    def keys_for(self, guid: str) -> list[str]:
        """Clés de l'index pour un GlobalId (plusieurs s'il est en double dans le fichier)."""
        if guid in self.elements:
            return [guid]
        occ = (self.ifc.get("duplicate_occurrences") or {}).get(guid.split("#", 1)[0]) or []
        return [o["key"] for o in occ if o["key"] in self.elements]

    def resolve(self, guids) -> list[str]:
        out: list[str] = []
        for g in guids:
            for k in self.keys_for(g):
                if k not in out:
                    out.append(k)
        return out

    def mesh(self, guid: str) -> tuple[np.ndarray, np.ndarray] | None:
        e = self.elements.get(guid)
        if not e or e.get("mesh_idx") is None:
            return None
        if self._meshes is None:
            self._meshes = dict(np.load(self.root / "meshes.npz"))
        m, k = self._meshes, int(e["mesh_idx"])
        v = m["verts"][m["vofs"][k]:m["vofs"][k + 1]]
        fc = m["faces"][m["fofs"][k]:m["fofs"][k + 1]]
        return v, fc

    def storey_names(self) -> dict[str, str]:
        return {s["guid"]: s["name"] for s in self.ifc["storeys"]}


# ── Cache (disque + mémoire) ─────────────────────────────────────────────

_mem: dict[str, GeometryIndex] = {}
_build_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def index_dir(cache_dir: Path, key: str, sha1: str) -> Path:
    return cache_dir / "geometry" / f"{key.replace('/', '_')}_{sha1[:16]}"


#: Débit mesuré de construction de l'index (Mo de fichier IFC par seconde) : sert aux estimations.
BUILD_MB_PER_S = 3.0


class IndexBuilding(ValueError):
    """L'index se calcule en tâche de fond : relancer l'outil plus tard (``info`` : avancement estimé)."""

    def __init__(self, info: dict):
        self.info = info
        super().__init__(
            f"Index géométrique en cours de calcul en tâche de fond (fichier IFC de {info['taille_mo']} Mo, "
            f"≈ {info['duree_estimee_s']} s, démarré il y a {info['ecoule_s']} s) : relancer l'outil dans "
            f"≈ {info['reste_estime_s']} s.")


_jobs: dict[str, dict] = {}


def _job_info(job: dict) -> dict:
    el = round(time.time() - job["started"])
    return {"etat": "en_cours", "taille_mo": job["size_mb"], "duree_estimee_s": job["eta_s"], "ecoule_s": el,
            "reste_estime_s": max(5, job["eta_s"] - el)}


def get_or_build(ifc_path: Path, cache_dir: Path, *, key: str, refresh: bool = False,
                 background_over_mb: float | None = None) -> tuple[GeometryIndex, bool]:
    """Index du fichier ``ifc_path`` ; ``(index, construit_maintenant)``.

    Au-delà de ``background_over_mb`` Mo, un index à construire l'est en tâche de fond : l'appel lève
    :class:`IndexBuilding` (avec l'avancement estimé) jusqu'à ce qu'il soit prêt.
    """
    from .source import file_sha1

    sha1 = file_sha1(ifc_path)
    root = index_dir(cache_dir, key, sha1)
    job = _jobs.get(str(root))
    if job is not None:
        if job["thread"].is_alive():
            raise IndexBuilding(_job_info(job))
        _jobs.pop(str(root), None)
        if job.get("error"):
            raise ValueError(f"Échec du calcul de l'index géométrique en tâche de fond : {job['error']}")
        refresh = False  # la reconstruction demandée vient d'être faite
    size_mb = ifc_path.stat().st_size / 2**20
    cached = str(root) in _mem or ((root / "meta.json").exists() and not _stale(root))
    if (refresh or not cached) and background_over_mb is not None and 0 < background_over_mb < size_mb:
        job = {"started": time.time(), "size_mb": round(size_mb, 1), "eta_s": round(size_mb / BUILD_MB_PER_S),
               "error": None}

        def work():
            try:
                _get_or_build_sync(ifc_path, root, key=key, refresh=refresh)
            except Exception as exc:  # noqa: BLE001 — remonté à l'appel suivant
                job["error"] = f"{type(exc).__name__}: {exc}"[:300]

        job["thread"] = threading.Thread(target=work, name=f"geometry-index-{key}", daemon=True)
        _jobs[str(root)] = job
        job["thread"].start()
        raise IndexBuilding(_job_info(job))
    return _get_or_build_sync(ifc_path, root, key=key, refresh=refresh)


def _get_or_build_sync(ifc_path: Path, root: Path, *, key: str, refresh: bool) -> tuple[GeometryIndex, bool]:
    with _guard:
        lock = _build_locks.setdefault(str(root), threading.Lock())
    with lock:
        hit = _mem.get(str(root))
        if hit and not refresh:
            return hit, False
        built = False
        if refresh or not (root / "meta.json").exists() or _stale(root):
            (root / "meta.json").unlink(missing_ok=True)  # un index partiel ne doit jamais passer pour complet
            idx = build_isolated(ifc_path, root, key=key)
            built = True
            for old in root.parent.glob(f"{key.replace('/', '_')}_*"):
                if old != root and old.is_dir():
                    _rmtree(old)
        else:
            idx = GeometryIndex.load(root)
        with _guard:
            for k in [k for k in _mem if Path(k).name.startswith(key.replace("/", "_") + "_")]:
                _mem.pop(k, None)
            if len(_mem) >= 4:
                _mem.pop(next(iter(_mem)))
            _mem[str(root)] = idx
        return idx, built


def _with_meta(root: Path, idx: GeometryIndex, extra: dict) -> GeometryIndex:
    """Ajoute au meta.json (et à l'index en mémoire) le budget et le déroulé du calcul."""
    idx.meta.update(extra)
    (root / "meta.json").write_text(json.dumps(idx.meta, ensure_ascii=False))
    return idx


def _stale(root: Path) -> bool:
    try:
        return json.loads((root / "meta.json").read_text()).get("version") != INDEX_VERSION
    except Exception:  # noqa: BLE001
        return True


def _rmtree(p: Path) -> None:
    import shutil

    shutil.rmtree(p, ignore_errors=True)
