"""Détection de conflits sur le modèle de la règle General Intersection de Solibri.

1. Phase large : couples dont les AABB, gonflées de la tolérance, se recouvrent.
2. Exclusions : élément avec lui-même, porte / fenêtre avec son mur hôte, parties d'un même
   agrégat, même système / même calque si demandé.
3. Doublon (AABB recouvertes à ≥ 90 % et volumes à moins de 10 % près) : sans phase fine.
4. Phase fine sur les triangles : les sommets de la région d'intersection sont exactement les
   sommets de A dans B, ceux de B dans A, et les points où les arêtes de l'un traversent les faces
   de l'autre. Leur boîte (orientée, axe vertical) donne les pénétrations horizontale et verticale.
5. Inclus : aucun croisement et tous les sommets de l'un dans l'autre (maillage fermé).
6. Tolérances (pénétration horizontale < ``tol_h`` ou verticale < ``tol_v`` → pas d'anomalie ;
   volume < ``tol_volume`` → pas d'anomalie), exceptions, sévérité.

Volume d'intersection : estimé par échantillonnage régulier de la boîte d'intersection, et seulement
si les deux maillages sont fermés (sinon ``None``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import mesh as M
from .index import GeometryIndex

PIPE_CLASSES = ("IfcFlowSegment", "IfcPipeSegment", "IfcDuctSegment", "IfcCableCarrierSegment", "IfcCableSegment")
WALL_CLASSES = ("IfcWall", "IfcCurtainWall")
SLAB_CLASSES = ("IfcSlab", "IfcRoof")
LIGHT_RE = re.compile(r"lumin|light|lamp|éclair|eclair|spot|luminaire", re.IGNORECASE)
AIR_RE = re.compile(r"bouche|grille|diffus|air terminal|airterminal|vmc|extract", re.IGNORECASE)
CEILING_RE = re.compile(r"plafond|ceiling", re.IGNORECASE)
INSULATION_RE = re.compile(r"isolation|isolant|insulation|calorifug|lagging", re.IGNORECASE)
NETWORK_CLASSES = ("IfcFlow", "IfcDistribution", "IfcDuct", "IfcPipe", "IfcCable", "IfcEnergyConversionDevice")
FITTING_CLASSES = ("IfcFlowFitting", "IfcDuctFitting", "IfcPipeFitting", "IfcCableCarrierFitting")

DEFAULT_EXCEPTIONS: dict[str, dict] = {
    "traversee_mur": {"enabled": True, "angle_min_deg": 89.0, "min_protrusion_m": 0.0},
    "traversee_dalle": {"enabled": True, "angle_min_deg": 89.0, "min_protrusion_m": 0.0},
    "luminaire_faux_plafond": {"enabled": True},
    "bouche_affleurante": {"enabled": True, "max_depth_m": 0.05},
    # isolant (IfcCovering INSULATION) autour de son propre réseau : même axe, l'un au cœur de l'autre
    "isolant_reseau": {"enabled": True, "max_angle_deg": 10.0, "margin_m": 0.02},
}
#: Exceptions examinées aussi pour les doublons et inclusions (un isolant a l'emprise de son réseau).
WRAP_EXCEPTIONS = {"isolant_reseau"}
SEV_ORDER = {"bloquant": 0, "majeur": 1, "mineur": 2, "info": 3}


@dataclass
class ClashOptions:
    tol_h: float = 0.005
    tol_v: float = 0.005
    tol_volume: float | None = None
    types: frozenset = frozenset({"duplicate", "inside", "overlap"})
    ignore_same_system: bool = False
    ignore_same_layer: bool = False
    exceptions: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_EXCEPTIONS.items()})
    severity_rules: list[dict] = field(default_factory=list)
    same_class_only: bool = False
    compute_volume: bool = True
    volume_samples: int = 12  # par axe
    max_candidates: int = 200_000
    contact_m: float = 0.0  # > 0 : contacts à plat ignorés (voir CONTACT_M)


@dataclass
class Clash:
    a: str
    b: str
    clash_type: str  # duplicate | inside | overlap
    depth_h: float | None = None
    depth_v: float | None = None
    depth: float | None = None
    span_h: float | None = None
    inter_aabb: list[float] | None = None
    volume: float | None = None
    orientation: str | None = None
    angle_deg: float | None = None
    exception: str | None = None
    severity: str = "majeur"
    size: str | None = None
    group: str | None = None
    inside_of: str | None = None  # pour « inclus » : élément contenant


def merge_exceptions(user: Any) -> dict:
    """``exceptions`` : liste de noms à activer, ``[]`` pour aucune, ou dict ``{nom: {enabled, valeurs…}}``."""
    ex = {k: dict(v) for k, v in DEFAULT_EXCEPTIONS.items()}
    if user is None:
        return ex
    if isinstance(user, (list, tuple)):
        names = set(user)
        for k in ex:
            ex[k]["enabled"] = k in names
        return ex
    for k, v in dict(user).items():
        if k not in ex:
            raise ValueError(f"Exception inconnue « {k} » (connues : {list(ex)}).")
        if isinstance(v, bool):
            ex[k]["enabled"] = v
        else:
            ex[k].update(v)
    return ex


def _is(cls: str, bases: tuple[str, ...]) -> bool:
    return any(cls == b or cls.startswith(b) for b in bases)


def main_axis(e: dict) -> np.ndarray:
    """Axe principal : vertical si la hauteur domine, sinon la plus grande dimension horizontale."""
    o = e["obb"]
    return np.asarray(o["axes"][2] if o["half"][2] >= o["half"][0] else o["axes"][0], float)


def _angle(u: np.ndarray, v: np.ndarray) -> float:
    c = abs(float(np.dot(u, v)) / (np.linalg.norm(u) * np.linalg.norm(v) or 1.0))
    return math.degrees(math.acos(min(1.0, c)))


def _aabb_overlap(a: list[float], b: list[float]) -> list[float] | None:
    lo = [max(a[i], b[i]) for i in range(3)]
    hi = [min(a[i + 3], b[i + 3]) for i in range(3)]
    if any(lo[i] > hi[i] for i in range(3)):
        return None
    return lo + hi


def _box_volume(b: list[float]) -> float:
    return max(0.0, b[3] - b[0]) * max(0.0, b[4] - b[1]) * max(0.0, b[5] - b[2])


# ── Phase large ─────────────────────────────────────────────────────────


def candidate_pairs(idx: GeometryIndex, set_a: list[str], set_b: list[str], tol: float) -> list[tuple[str, str]]:
    def usable(g):  # agrégat reconstitué depuis ses parties : ce sont les parties qui sont testées
        e = idx.elements.get(g) or {}
        return bool(e.get("aabb")) and not e.get("from_parts")

    a_ids = [g for g in set_a if usable(g)]
    b_ids = [g for g in set_b if usable(g)]
    if not a_ids or not b_ids:
        return []
    bb = np.array([idx.elements[g]["aabb"] for g in b_ids])
    order = np.argsort(bb[:, 0])
    bb, b_sorted = bb[order], [b_ids[i] for i in order]
    xmins = bb[:, 0]
    same = set(a_ids) & set(b_ids)
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ga in a_ids:
        a = idx.elements[ga]["aabb"]
        hi = np.searchsorted(xmins, a[3] + tol, side="right")
        if hi == 0:
            continue
        c = bb[:hi]
        m = (c[:, 3] >= a[0] - tol) & (c[:, 1] <= a[4] + tol) & (c[:, 4] >= a[1] - tol) & \
            (c[:, 2] <= a[5] + tol) & (c[:, 5] >= a[2] - tol)
        for k in np.nonzero(m)[0]:
            gb = b_sorted[k]
            if gb == ga:
                continue
            key = (ga, gb) if not (ga in same and gb in same) else tuple(sorted((ga, gb)))
            if key in seen:
                continue
            seen.add(key)
            out.append((ga, gb))
    return out


# ── Exclusions ──────────────────────────────────────────────────────────


def _hosts(idx: GeometryIndex) -> dict[str, str]:
    fills, voids = idx.ifc.get("fills") or {}, idx.ifc.get("voids") or {}
    return {g: voids[o] for g, o in fills.items() if o in voids}


def excluded(idx: GeometryIndex, a: str, b: str, opts: ClashOptions, hosts: dict[str, str]) -> str | None:
    ea, eb = idx.elements[a], idx.elements[b]
    if hosts.get(a) == b or hosts.get(b) == a:
        return "hôte"
    if ea.get("parent") and (ea["parent"] == eb.get("parent") or ea["parent"] == b):
        return "agrégat"
    if eb.get("parent") == a:
        return "agrégat"
    if opts.same_class_only and ea["ifc_class"] != eb["ifc_class"]:
        return "classe"
    if opts.ignore_same_system and set(ea.get("systems") or []) & set(eb.get("systems") or []):
        return "système"
    if opts.ignore_same_layer and set(ea.get("layers") or []) & set(eb.get("layers") or []):
        return "calque"
    return None


# ── Phase fine ──────────────────────────────────────────────────────────


def _is_duplicate(ea: dict, eb: dict) -> bool:
    ov = _aabb_overlap(ea["aabb"], eb["aabb"])
    if ov is None:
        return False
    va, vb = _box_volume(ea["aabb"]), _box_volume(eb["aabb"])
    if max(va, vb) <= 0 or _box_volume(ov) / max(va, vb) < 0.9:
        return False
    if ea.get("is_closed") and eb.get("is_closed") and ea.get("volume") and eb.get("volume"):
        ma, mb = ea["volume"], eb["volume"]
    else:
        ma, mb = va, vb
    if max(ma, mb) <= 0 or abs(ma - mb) / max(ma, mb) >= 0.10:
        return False
    oa, ob = ea.get("obb"), eb.get("obb")  # même AABB et volume mais pas la même pièce (biais) : on exclut
    if oa and ob and np.linalg.norm(np.subtract(oa["center"], ob["center"])) > 0.05 + 0.1 * max(oa["half"]):
        return False
    return True


#: Valeur conseillée de l'option ``contact_m`` (1 mm, sous la tolérance de 5 mm) : deux surfaces à moins de cette
#: distance se touchent sans se traverser. Stabilise les contacts à plat (fenêtre contre poteau), dont le résultat
#: dépend sinon du découpage des faces en triangles. Désactivée par défaut (change sensiblement le résultat).
CONTACT_M = 0.001


def _surface_distance(points: np.ndarray, verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Distance de chaque point à la surface du maillage."""
    from .zones import _closest_on_triangles

    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    out = np.empty(len(points))
    step = max(1, 1_000_000 // max(len(faces), 1))
    for s in range(0, len(points), step):
        p = points[s:s + step]
        out[s:s + step] = np.linalg.norm(_closest_on_triangles(p, a, b, c) - p[:, None, :], axis=2).min(axis=1)
    return out


def _region(idx: GeometryIndex, a: str, b: str, box: list[float], contact: float = 0.0):
    """Points de la région d'intersection et indicateurs d'inclusion.

    ``contact`` > 0 (option ``contact_m``) : surfaces qui se touchent à moins de ``contact`` = contact, pas conflit ;
    preuve du conflit par les seules traversées franches et sommets strictement intérieurs, étendue complétée par les
    sommets posés sur la surface de l'autre. 0 (défaut) : méthode d'origine."""
    ma, mb = idx.mesh(a), idx.mesh(b)
    if ma is None or mb is None:
        return None
    va, fa = ma
    vb, fb = mb
    pad = 1e-4
    box = [box[0] - pad, box[1] - pad, box[2] - pad, box[3] + pad, box[4] + pad, box[5] + pad]
    fa_in, fb_in = M.faces_in_box(va, fa, box), M.faces_in_box(vb, fb, box)
    ea, eb = idx.elements[a], idx.elements[b]
    pts = []
    if len(fa_in) and len(fb_in):  # sinon aucune surface ne traverse l'autre (inclusion possible)
        for (v1, f1), (v2, f2) in (((va, fa_in), (vb, fb_in)), ((vb, fb_in), (va, fa_in))):
            e = M.edges_of(f1)
            pts.append(M.segment_crossings(v1[e[:, 0]], v1[e[:, 1]], v2, f2, contact=contact))
    crossings = sum(len(p) for p in pts)

    def inside(v, f_all, closed, other_box):
        """Sommets de ``v`` dans l'autre maillage : (strictement dedans, posés sur sa surface à moins de ``contact``)."""
        if not closed:
            return np.zeros((0, 3)), np.zeros((0, 3))
        lo, hi = np.asarray(other_box[:3]), np.asarray(other_box[3:])
        cand = v[np.all((v >= lo) & (v <= hi), axis=1)]
        cand = np.unique(np.round(cand, 7), axis=0)
        if not len(cand):
            return cand, cand
        if contact <= 0:
            return cand[M.points_inside(cand, *f_all)], np.zeros((0, 3))
        near = _surface_distance(cand, *f_all) <= contact  # le lancer de rayons y est instable : ne prouvent rien
        far = cand[~near]
        mask = M.points_inside(far, *f_all) if len(far) else np.zeros(0, bool)
        return far[mask], cand[near]

    strict_a, on_a = inside(va, (vb, fb), eb.get("is_closed"), box)
    strict_b, on_b = inside(vb, (va, fa), ea.get("is_closed"), box)
    proof = crossings + len(strict_a) + len(strict_b)
    # étendue : une fois le conflit prouvé, les sommets posés sur la surface de l'autre en font partie
    pts += [strict_a, strict_b] + ([on_a, on_b] if proof else [])
    all_pts = np.concatenate([p for p in pts if len(p)]) if any(len(p) for p in pts) else np.zeros((0, 3))
    n_a_vertices = len(np.unique(np.round(va, 7), axis=0))
    n_b_vertices = len(np.unique(np.round(vb, 7), axis=0))
    return {"points": all_pts if proof else np.zeros((0, 3)), "crossings": crossings,
            "a_in_b": crossings == 0 and len(strict_a) > 0 and len(strict_a) + len(on_a) == n_a_vertices,
            "b_in_a": crossings == 0 and len(strict_b) > 0 and len(strict_b) + len(on_b) == n_b_vertices}


def _intersection_volume(idx: GeometryIndex, a: str, b: str, box: list[float], n: int) -> float | None:
    ea, eb = idx.elements[a], idx.elements[b]
    if not (ea.get("is_closed") and eb.get("is_closed")):
        return None
    if _box_volume(box) <= 0:
        return 0.0
    axes = [np.linspace(box[i], box[i + 3], n + 1) for i in range(3)]
    centers = [(x[:-1] + x[1:]) / 2 for x in axes]
    g = np.stack(np.meshgrid(*centers, indexing="ij"), axis=-1).reshape(-1, 3)
    ma, mb = idx.mesh(a), idx.mesh(b)
    va, fa = ma
    vb, fb = mb
    ins = M.points_inside(g, va, M.faces_in_box(va, fa, _ray_box(box, va)))
    if ins.any():
        ins[ins] = M.points_inside(g[ins], vb, M.faces_in_box(vb, fb, _ray_box(box, vb)))
    return float(ins.mean() * _box_volume(box))


def _hull_volume(idx: GeometryIndex, a: str, b: str, pts: np.ndarray) -> float | None:
    """Volume de l'enveloppe convexe des sommets de la région d'intersection : exact quand l'intersection est
    convexe (cas usuel : boîte × boîte, tuyau × paroi), majoré sinon. ``None`` si un maillage est ouvert."""
    if not (idx.elements[a].get("is_closed") and idx.elements[b].get("is_closed")):
        return None
    p = np.unique(np.round(pts - pts.mean(axis=0), 7), axis=0)
    if len(p) < 4:
        return 0.0
    try:
        from scipy.spatial import ConvexHull

        return float(ConvexHull(p).volume)
    except Exception:  # noqa: BLE001 — points coplanaires ou scipy absent : volume par échantillonnage
        return _intersection_volume(idx, a, b, M.aabb(pts), 8)


def _ray_box(box: list[float], verts: np.ndarray) -> list[float]:
    """Boîte à garder pour les rayons partant de ``box`` : toute l'étendue de l'élément au-delà de ``box``."""
    hi = verts.max(axis=0)
    lo = verts.min(axis=0)
    return [min(box[0], lo[0]), min(box[1], lo[1]), min(box[2], lo[2]), hi[0], hi[1], hi[2]]


# ── Exceptions et sévérité ──────────────────────────────────────────────


def _text(e: dict, rec) -> str:
    return " ".join(str(x or "") for x in (e.get("name"), e.get("type_name"), e.get("predefined"),
                                            getattr(rec, "name", "") if rec else ""))


def _exception(idx: GeometryIndex, c: Clash, opts: ClashOptions, records: dict,
               only: set[str] | None = None) -> str | None:
    """Exception applicable à un conflit (``only`` : n'examiner que ces exceptions)."""
    ex = {k: v for k, v in opts.exceptions.items() if only is None or k in only}
    for x, y in ((c.a, c.b), (c.b, c.a)):
        ex_, ey = idx.elements[x], idx.elements[y]
        cx, cy = ex_["ifc_class"], ey["ifc_class"]
        if _is(cx, PIPE_CLASSES) and ex_.get("obb") and ey.get("obb"):
            for key, hosts, normal in (("traversee_mur", WALL_CLASSES, np.asarray(ey["obb"]["axes"][1])),
                                       ("traversee_dalle", SLAB_CLASSES, np.array([0.0, 0.0, 1.0]))):
                cfg = ex.get(key) or {}
                if not cfg.get("enabled") or not _is(cy, hosts):
                    continue
                if key == "traversee_mur" and ey["obb"]["half"][2] < ey["obb"]["half"][1]:
                    continue  # « mur » plus épais que haut : pas une paroi verticale
                plane_angle = 90.0 - _angle(main_axis(ex_), normal)
                if plane_angle < float(cfg.get("angle_min_deg", 89.0)) - 1e-9:
                    continue
                if _passes_through(idx, x, y, normal, float(cfg.get("min_protrusion_m", 0.0))):
                    return key
        if (ex.get("luminaire_faux_plafond") or {}).get("enabled") and _is(cy, ("IfcCovering",)) and (
                (ey.get("predefined") or "").upper() == "CEILING" or CEILING_RE.search(_text(ey, records.get(y)))):
            if _is(cx, ("IfcLightFixture", "IfcLamp")) or (
                    _is(cx, ("IfcFlowTerminal",)) and LIGHT_RE.search(_text(ex_, records.get(x)))):
                return "luminaire_faux_plafond"
        cfg = ex.get("isolant_reseau") or {}
        if cfg.get("enabled") and _is(cy, ("IfcCovering",)) and _is(cx, NETWORK_CLASSES) and (
                (ey.get("predefined") or "").upper() == "INSULATION" or INSULATION_RE.search(_text(ey, records.get(y)))) \
                and _wraps(ex_, ey, float(cfg.get("max_angle_deg", 10.0)), float(cfg.get("margin_m", 0.02)),
                           _is(cx, FITTING_CLASSES)):
            return "isolant_reseau"
        cfg = ex.get("bouche_affleurante") or {}
        if cfg.get("enabled") and (_is(cx, ("IfcAirTerminal",)) or (
                _is(cx, ("IfcFlowTerminal",)) and AIR_RE.search(_text(ex_, records.get(x))))) and \
                _is(cy, WALL_CLASSES + SLAB_CLASSES + ("IfcCovering",)):
            if c.depth is not None and c.depth <= float(cfg.get("max_depth_m", 0.05)) + 1e-9:
                return "bouche_affleurante"
    return None


def _in_obb(p: np.ndarray, obb: dict, margin: float) -> bool:
    local = np.asarray(obb["axes"], float) @ (np.asarray(p, float) - np.asarray(obb["center"], float))
    return bool(np.all(np.abs(local) <= np.asarray(obb["half"], float) + margin))


def _wraps(net: dict, ins: dict, max_angle: float, margin: float, fitting: bool) -> bool:
    """L'isolant enveloppe ce réseau : axes parallèles (sauf raccord, de forme libre) et le centre de l'un dans la
    boîte orientée de l'autre (élargie de l'épaisseur d'isolant pour le réseau)."""
    if not net.get("obb") or not ins.get("obb"):
        return False
    if not fitting and _angle(main_axis(net), main_axis(ins)) > max_angle:
        return False
    thick = float(min(ins["obb"]["half"]))  # demi-épaisseur de la boîte d'isolant : majorant de l'épaisseur
    return _in_obb(net["obb"]["center"], ins["obb"], margin) or _in_obb(ins["obb"]["center"], net["obb"], thick + margin)


#: Traversée « incluse » : tronçon perpendiculaire à la paroi, de la longueur de son épaisseur (à 2 cm près).
CROSSING_LENGTH_TOL_M = 0.02


def _inside_crossing(idx: GeometryIndex, inner: str, outer: str, opts: ClashOptions) -> str | None:
    """Tronçon de réseau entièrement inclus dans un mur ou une dalle, perpendiculaire, et exactement de son épaisseur :
    c'est une traversée (le tronçon de traversée est modélisé à part), pas une inclusion."""
    ei, eo = idx.elements[inner], idx.elements[outer]
    if not _is(ei["ifc_class"], PIPE_CLASSES) or not ei.get("obb") or not eo.get("obb"):
        return None
    oi, oo = ei["obb"], eo["obb"]
    axis = main_axis(ei)
    length = 2 * (oi["half"][2] if oi["half"][2] >= oi["half"][0] else oi["half"][0])
    for key, hosts in (("traversee_mur", WALL_CLASSES), ("traversee_dalle", SLAB_CLASSES)):
        cfg = opts.exceptions.get(key) or {}
        if not cfg.get("enabled") or not _is(eo["ifc_class"], hosts):
            continue
        if key == "traversee_mur":
            normal, thick = np.asarray(oo["axes"][1], float), 2 * oo["half"][1]
        else:
            normal, thick = np.array([0.0, 0.0, 1.0]), 2 * oo["half"][2]
        if 90.0 - _angle(axis, normal) < float(cfg.get("angle_min_deg", 89.0)) - 1e-9:
            continue
        if abs(length - thick) <= CROSSING_LENGTH_TOL_M + 1e-9:
            return key
    return None


def connected_groups(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """Composantes connexes de paires (doublons superposés : 13 compteurs empilés = un seul groupe)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for x in list(parent):
        groups.setdefault(find(x), []).append(x)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g[0]))


def _passes_through(idx: GeometryIndex, pipe: str, host: str, normal: np.ndarray, protrusion: float) -> bool:
    mp, mh = idx.mesh(pipe), idx.mesh(host)
    if mp is None or mh is None:
        return False
    n = normal / (np.linalg.norm(normal) or 1.0)
    # Épaisseur de la paroi au droit du tuyau : projection des sommets de l'hôte proches de l'axe du tuyau.
    pp, hp = mp[0] @ n, mh[0] @ n
    eh = idx.elements[host]["obb"]
    thick = 2 * (eh["half"][1] if n[2] < 0.5 else eh["half"][2])
    c = float(np.dot(np.asarray(eh["center"]), n))
    h0, h1 = c - thick / 2, c + thick / 2
    if hp.min() > h0 + 1e-6 or hp.max() < h1 - 1e-6:
        h0, h1 = hp.min(), hp.max()
    return pp.min() <= h0 - protrusion + 1e-6 and pp.max() >= h1 + protrusion - 1e-6


def _size(depth: float | None) -> str:
    if depth is None:
        return "grand"
    return "petit" if depth < 0.025 else ("grand" if depth > 0.200 else "moyen")


def _orientation(angle: float | None) -> str | None:
    if angle is None:
        return None
    return "parallele" if angle <= 20 else ("perpendiculaire" if angle >= 70 else "oblique")


def default_severity(c: Clash) -> str:
    if c.clash_type in ("duplicate", "inside"):
        return "bloquant"
    return "mineur" if c.size == "petit" else "majeur"


def _rule_matches(when: dict, c: Clash, ea: dict, eb: dict, disc: dict) -> bool:
    for k, v in (when or {}).items():
        vals = v if isinstance(v, (list, tuple)) else [v]
        if k == "clash_type" and c.clash_type not in vals:
            return False
        if k == "size" and c.size not in vals:
            return False
        if k == "orientation" and c.orientation not in vals:
            return False
        if k in ("class_a", "class_b"):
            cls = (ea if k == "class_a" else eb)["ifc_class"]
            if not any(cls.startswith(x) for x in vals):
                return False
        if k in ("discipline_a", "discipline_b"):
            d = disc.get(c.a if k == "discipline_a" else c.b)
            if d not in vals:
                return False
    return True


def severity(c: Clash, rules: list[dict], ea: dict, eb: dict, disc: dict) -> str:
    """Sévérité par défaut puis règles utilisateur ; quand plusieurs règles s'appliquent, la plus grave l'emporte."""
    hits = [r["severity"] for r in rules or [] if _rule_matches(r.get("when") or {}, c, ea, eb, disc)]
    if not hits:
        return default_severity(c)
    return min(hits, key=lambda s: SEV_ORDER.get(s, 9))


# ── Orchestration ───────────────────────────────────────────────────────


def find_clashes(idx: GeometryIndex, set_a: list[str], set_b: list[str], opts: ClashOptions, *,
                 records: dict | None = None, disciplines: dict | None = None) -> tuple[list[Clash], dict]:
    records = records or {}
    disc = disciplines or {}
    tol = max(opts.tol_h, opts.tol_v, 0.0)
    pairs = candidate_pairs(idx, set_a, set_b, tol)
    stats = {"paires_candidates": len(pairs), "exclues": {}, "sous_tolerance": 0, "sans_intersection": 0,
             "exceptions": {}, "sans_maillage": 0}
    if len(pairs) > opts.max_candidates:
        raise ValueError(f"{len(pairs)} paires candidates (> {opts.max_candidates}) : restreindre set_a / set_b.")
    hosts = _hosts(idx)
    out: list[Clash] = []
    for a, b in pairs:
        why = excluded(idx, a, b, opts, hosts)
        if why:
            stats["exclues"][why] = stats["exclues"].get(why, 0) + 1
            continue
        ea, eb = idx.elements[a], idx.elements[b]
        if _is_duplicate(ea, eb):
            if "duplicate" in opts.types:
                c = Clash(a, b, "duplicate", inter_aabb=_aabb_overlap(ea["aabb"], eb["aabb"]), size="grand")
                c.volume = min(ea.get("volume") or 0, eb.get("volume") or 0) or None
                exc = _exception(idx, c, opts, records, only=WRAP_EXCEPTIONS)
                if exc:
                    stats["exceptions"][exc] = stats["exceptions"].get(exc, 0) + 1
                    continue
                c.severity = severity(c, opts.severity_rules, ea, eb, disc)
                out.append(c)
            continue
        if opts.types <= {"duplicate"}:
            continue
        box = _aabb_overlap(ea["aabb"], eb["aabb"])
        if box is None:  # AABB séparées de moins que la tolérance : pas d'intersection
            stats["sans_intersection"] += 1
            continue
        reg = _region(idx, a, b, box, opts.contact_m)
        if reg is None:
            stats["sans_maillage"] += 1
            continue
        pts = reg["points"]
        if len(pts) == 0:
            stats["sans_intersection"] += 1
            continue
        if reg["a_in_b"] or reg["b_in_a"]:
            if "inside" not in opts.types:
                continue
            inner, outer = (a, b) if reg["a_in_b"] else (b, a)
            ei = idx.elements[inner]
            c = Clash(a, b, "inside", inter_aabb=ei["aabb"], volume=ei.get("volume") if ei.get("is_closed") else None,
                      inside_of=outer, size="grand")
            o = ei["obb"]
            c.depth_h, c.depth_v = round(2 * o["half"][1], 4), round(2 * o["half"][2], 4)
            c.depth = min(c.depth_h, c.depth_v)
            exc = _exception(idx, c, opts, records, only=WRAP_EXCEPTIONS) or _inside_crossing(idx, inner, outer, opts)
            if exc:
                stats["exceptions"][exc] = stats["exceptions"].get(exc, 0) + 1
                continue
            c.severity = severity(c, opts.severity_rules, ea, eb, disc)
            out.append(c)
            continue
        if "overlap" not in opts.types:
            continue
        inter = M.aabb(pts)
        if len(pts) >= 3:
            o = M.obb_vertical(pts)
            depth_h, span_h = 2 * o["half"][1], 2 * o["half"][0]
        else:
            depth_h, span_h = min(inter[3] - inter[0], inter[4] - inter[1]), max(inter[3] - inter[0], inter[4] - inter[1])
        depth_v = inter[5] - inter[2]
        if depth_h < opts.tol_h or depth_v < opts.tol_v or depth_h <= 1e-6 or depth_v <= 1e-6:
            stats["sous_tolerance"] += 1
            continue
        c = Clash(a, b, "overlap", depth_h=round(depth_h, 4), depth_v=round(depth_v, 4),
                  depth=round(min(depth_h, depth_v), 4), span_h=round(span_h, 4), inter_aabb=[round(x, 4) for x in inter])
        if opts.tol_volume is not None:  # décision sur le volume : échantillonnage (plus juste, plus lent)
            c.volume = _intersection_volume(idx, a, b, inter, opts.volume_samples)
        elif opts.compute_volume:
            c.volume = _hull_volume(idx, a, b, pts)
        if opts.tol_volume is not None and c.volume is not None and c.volume < opts.tol_volume:
            stats["sous_tolerance"] += 1
            continue
        if ea.get("obb") and eb.get("obb"):
            c.angle_deg = round(_angle(main_axis(ea), main_axis(eb)), 1)
            c.orientation = _orientation(c.angle_deg)
        c.size = _size(c.depth)
        exc = _exception(idx, c, opts, records)
        if exc:
            stats["exceptions"][exc] = stats["exceptions"].get(exc, 0) + 1
            continue
        c.severity = severity(c, opts.severity_rules, ea, eb, disc)
        out.append(c)
    return out, stats


# ── Regroupement (6 catégories Solibri) ─────────────────────────────────

GROUP_LABELS = {
    "doublons": "Doublons", "inclus": "Inclus",
    "similaires_plusieurs_etages": "Intersections similaires sur plusieurs étages",
    "etages_differents": "Composants d'étages différents",
    "similaires_un_etage": "Intersections similaires sur un étage",
    "autres": "Autres intersections",
}


def assign_groups(idx: GeometryIndex, clashes: list[Clash]) -> None:
    """Catégorie de chaque conflit ; « similaires » = même couple de types, dimensions à 1 cm près."""
    from collections import defaultdict

    def sim_key(c: Clash):
        ta = idx.elements[c.a].get("type_name") or idx.elements[c.a]["ifc_class"]
        tb = idx.elements[c.b].get("type_name") or idx.elements[c.b]["ifc_class"]
        d = tuple(round(x / 0.01) for x in sorted([c.depth_h or 0, c.depth_v or 0, c.span_h or 0]))
        return tuple(sorted((ta, tb))), d

    sims: dict = defaultdict(list)
    for c in clashes:
        if c.clash_type == "overlap":
            sims[sim_key(c)].append(c)
    for c in clashes:
        sa, sb = idx.elements[c.a].get("storey"), idx.elements[c.b].get("storey")
        if c.clash_type == "duplicate":
            c.group = "doublons"
        elif c.clash_type == "inside":
            c.group = "inclus"
        else:
            g = sims[sim_key(c)]
            storeys = {idx.elements[x.a].get("storey") for x in g}
            if len(g) > 1 and len(storeys) > 1:
                c.group = "similaires_plusieurs_etages"
            elif sa and sb and sa != sb:
                c.group = "etages_differents"
            elif len(g) > 1:
                c.group = "similaires_un_etage"
            else:
                c.group = "autres"
