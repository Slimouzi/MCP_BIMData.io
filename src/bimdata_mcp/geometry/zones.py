"""Zones virtuelles (dégagements, aires de rotation) et distances entre éléments.

- Zone = boîte orientée (centre, 3 axes, demi-dimensions), maillée en 12 triangles et testée contre
  l'index comme un élément : empiètement mesuré sur les maillages (même méthode que le clash).
- Distance minimale entre deux maillages : sommet × triangle dans les deux sens et arête × arête
  (exacte pour des maillages triangulés), 0 s'ils se coupent.
"""

from __future__ import annotations

import numpy as np

from . import mesh as M
from .index import GeometryIndex

_BOX_FACES = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                       [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
_CORNERS = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1], [-1, -1, 1], [1, -1, 1], [1, 1, 1],
                     [-1, 1, 1]], float)


def box_mesh(obb: dict) -> tuple[np.ndarray, np.ndarray]:
    c, ax, h = np.asarray(obb["center"]), np.asarray(obb["axes"]), np.asarray(obb["half"])
    v = c + (_CORNERS * h) @ ax
    return v, _BOX_FACES.copy()


def obb_aabb(obb: dict) -> list[float]:
    v, _ = box_mesh(obb)
    return M.aabb(v)


def to_local(points: np.ndarray, obb: dict) -> np.ndarray:
    return (points - np.asarray(obb["center"])) @ np.asarray(obb["axes"]).T


def intrusion(idx: GeometryIndex, guid: str, zone: dict) -> tuple[float, list[float] | None]:
    """Empiètement d'un élément dans une zone : épaisseur de la région commune (plus petite dimension, dans le
    repère de la zone) et AABB de cette région. (0, None) si l'élément ne touche pas la zone."""
    m = idx.mesh(guid)
    if m is None:
        return 0.0, None
    v, f = m
    zv, zf = box_mesh(zone)
    box = obb_aabb(zone)
    f_in = M.faces_in_box(v, f, box)
    pts = []
    loc = to_local(v, zone)
    inside = np.all(np.abs(loc) < np.asarray(zone["half"]) - 1e-9, axis=1)
    pts.append(v[inside])
    if len(f_in):
        e = M.edges_of(f_in)
        pts.append(M.segment_crossings(v[e[:, 0]], v[e[:, 1]], zv, zf))
        ze = M.edges_of(zf)
        pts.append(M.segment_crossings(zv[ze[:, 0]], zv[ze[:, 1]], v, f_in))
    if idx.elements[guid].get("is_closed"):
        pts.append(zv[M.points_inside(zv, v, f)])
    pts = [p for p in pts if len(p)]
    if not pts:
        return 0.0, None
    p = np.concatenate(pts)
    lp = to_local(p, zone)
    ext = lp.max(axis=0) - lp.min(axis=0)
    return float(ext.min()), M.aabb(p)


# ── Distances ───────────────────────────────────────────────────────────


def _closest_on_triangles(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Point le plus proche de chaque ``p[i]`` sur chaque triangle (a, b, c)[j] — Ericson, vectorisé."""
    P = p[:, None, :]
    A, B, C = a[None], b[None], c[None]
    ab, ac, ap = B - A, C - A, P - A
    d1, d2 = np.einsum("ijk,ijk->ij", ab, ap), np.einsum("ijk,ijk->ij", ac, ap)
    bp, cp = P - B, P - C
    d3, d4 = np.einsum("ijk,ijk->ij", ab, bp), np.einsum("ijk,ijk->ij", ac, bp)
    d5, d6 = np.einsum("ijk,ijk->ij", ab, cp), np.einsum("ijk,ijk->ij", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    shape = d1.shape + (3,)
    out = np.empty(shape)
    denom = va + vb + vc
    safe = np.where(np.abs(denom) > 1e-18, denom, 1.0)
    v_ = vb / safe
    w_ = vc / safe
    out[:] = A + ab * v_[..., None] + ac * w_[..., None]  # intérieur
    # arêtes
    m_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    t = np.clip((d4 - d3) / np.where(((d4 - d3) + (d5 - d6)) != 0, (d4 - d3) + (d5 - d6), 1), 0, 1)
    out = np.where(m_bc[..., None], B + (C - B) * t[..., None], out)
    m_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    t = np.clip(d2 / np.where((d2 - d6) != 0, d2 - d6, 1), 0, 1)
    out = np.where(m_ac[..., None], A + ac * t[..., None], out)
    m_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    t = np.clip(d1 / np.where((d1 - d3) != 0, d1 - d3, 1), 0, 1)
    out = np.where(m_ab[..., None], A + ab * t[..., None], out)
    # sommets
    out = np.where(((d6 >= 0) & (d5 <= d6))[..., None], C + 0 * P, out)
    out = np.where(((d3 >= 0) & (d4 <= d3))[..., None], B + 0 * P, out)
    out = np.where(((d1 <= 0) & (d2 <= 0))[..., None], A + 0 * P, out)
    return out


def _point_mesh(points: np.ndarray, verts: np.ndarray, faces: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    best, pa, pb = np.inf, None, None
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    step = max(1, 1_000_000 // max(len(faces), 1))
    for s in range(0, len(points), step):
        p = points[s:s + step]
        q = _closest_on_triangles(p, a, b, c)
        d = np.linalg.norm(q - p[:, None, :], axis=2)
        i, j = np.unravel_index(np.argmin(d), d.shape)
        if d[i, j] < best:
            best, pa, pb = float(d[i, j]), p[i], q[i, j]
    return best, pa, pb


def _segments_distance(p0, p1, q0, q1) -> tuple[float, np.ndarray, np.ndarray]:
    """Distance minimale entre deux ensembles de segments (tous couples) — Ericson, vectorisé."""
    dp, dq = p1 - p0, q1 - q0
    r = p0[:, None, :] - q0[None, :, :]
    a = np.einsum("ik,ik->i", dp, dp)[:, None]
    e = np.einsum("jk,jk->j", dq, dq)[None, :]
    b = dp @ dq.T
    c = np.einsum("ijk,ik->ij", r, dp)
    f = np.einsum("ijk,jk->ij", r, dq)
    a_ = np.where(a > 1e-18, a, 1.0)
    e_ = np.where(e > 1e-18, e, 1.0)
    denom = a * e - b * b
    s = np.where(denom > 1e-18, np.clip((b * f - c * e) / np.where(denom > 1e-18, denom, 1.0), 0, 1), 0.0)
    t = (b * s + f) / e_
    s = np.where(t < 0, np.clip(-c / a_, 0, 1), np.where(t > 1, np.clip((b - c) / a_, 0, 1), s))
    t = np.clip(t, 0, 1)
    ca = p0[:, None, :] + dp[:, None, :] * s[..., None]
    cb = q0[None, :, :] + dq[None, :, :] * t[..., None]
    d = np.linalg.norm(ca - cb, axis=2)
    i, j = np.unravel_index(np.argmin(d), d.shape)
    return float(d[i, j]), ca[i, j], cb[i, j]


def mesh_distance(idx: GeometryIndex, a: str, b: str, *, max_d: float | None = None) -> tuple[float, np.ndarray | None,
                                                                                               np.ndarray | None]:
    """Distance minimale (m) entre les maillages de ``a`` et ``b`` et les deux points les plus proches."""
    from .clash import _aabb_overlap, _region

    ma, mb = idx.mesh(a), idx.mesh(b)
    if ma is None or mb is None:
        return np.inf, None, None
    ea, eb = idx.elements[a], idx.elements[b]
    ov = _aabb_overlap(ea["aabb"], eb["aabb"])
    if ov is not None:
        reg = _region(idx, a, b, ov)
        if reg is not None and len(reg["points"]):
            p = reg["points"][0]
            return 0.0, p, p
    va, fa = ma
    vb, fb = mb
    if max_d is not None:  # ne garder que les primitives proches de l'autre élément
        box_b = [x - max_d for x in eb["aabb"][:3]] + [x + max_d for x in eb["aabb"][3:]]
        box_a = [x - max_d for x in ea["aabb"][:3]] + [x + max_d for x in ea["aabb"][3:]]
        fa, fb = M.faces_in_box(va, fa, box_b), M.faces_in_box(vb, fb, box_a)
        if len(fa) == 0 or len(fb) == 0:
            return np.inf, None, None
    ua = np.unique(fa.reshape(-1))
    ub = np.unique(fb.reshape(-1))
    best = _point_mesh(va[ua], vb, fb)
    d2 = _point_mesh(vb[ub], va, fa)
    if d2[0] < best[0]:
        best = (d2[0], d2[2], d2[1])
    ea_, eb_ = M.edges_of(fa), M.edges_of(fb)
    if len(ea_) * len(eb_) <= 4_000_000:
        d3 = _segments_distance(va[ea_[:, 0]], va[ea_[:, 1]], vb[eb_[:, 0]], vb[eb_[:, 1]])
        if d3[0] < best[0]:
            best = d3
    return best


def footprint(idx: GeometryIndex, guid: str) -> np.ndarray | None:
    m = idx.mesh(guid)
    if m is None:
        return None
    return M._hull_2d(m[0][:, :2])


def _seg_point_2d(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(np.einsum("ij,ij->i", p - a, ab[None].repeat(len(p), 0)) / max(float(ab @ ab), 1e-18), 0, 1)
    return np.linalg.norm(p - (a + ab * t[:, None]), axis=1)


def _poly_inside(points: np.ndarray, poly: np.ndarray) -> np.ndarray:
    if len(poly) < 3:
        return np.zeros(len(points), bool)
    e = np.roll(poly, -1, axis=0) - poly
    rel = points[:, None, :] - poly[None, :, :]
    cross = e[None, :, 0] * rel[..., 1] - e[None, :, 1] * rel[..., 0]
    return np.all(cross >= -1e-9, axis=1)


def _edges_cross_2d(pa: np.ndarray, pb: np.ndarray) -> bool:
    """Deux contours se croisent (cas « en croix » où aucun sommet n'est dans l'autre)."""
    a0, a1 = pa, np.roll(pa, -1, axis=0)
    b0, b1 = pb, np.roll(pb, -1, axis=0)

    def orient(p, q, r):
        return (q[..., 0] - p[..., 0]) * (r[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (r[..., 0] - p[..., 0])

    A0, A1 = a0[:, None, :], a1[:, None, :]
    B0, B1 = b0[None, :, :], b1[None, :, :]
    o1, o2 = orient(A0, A1, B0), orient(A0, A1, B1)
    o3, o4 = orient(B0, B1, A0), orient(B0, B1, A1)
    return bool(np.any((o1 * o2 < 0) & (o3 * o4 < 0)))


def horizontal_distance(pa: np.ndarray, pb: np.ndarray) -> float:
    """Distance en plan entre deux emprises convexes (0 si elles se recouvrent)."""
    if pa is None or pb is None or len(pa) == 0 or len(pb) == 0:
        return np.inf
    if _poly_inside(pa, pb).any() or _poly_inside(pb, pa).any() or _edges_cross_2d(pa, pb):
        return 0.0
    best = np.inf
    for P, Q in ((pa, pb), (pb, pa)):
        for i in range(len(Q)):
            best = min(best, float(_seg_point_2d(P, Q[i], Q[(i + 1) % len(Q)]).min()))
    return best


def footprints_overlap(pa: np.ndarray, pb: np.ndarray) -> bool:
    return horizontal_distance(pa, pb) == 0.0


# ── Locaux ──────────────────────────────────────────────────────────────


class SpaceLocator:
    """Local contenant un point (maillage fermé du local, sinon son AABB)."""

    def __init__(self, idx: GeometryIndex):
        self.idx = idx
        self.spaces = [g for g, e in idx.elements.items() if e["ifc_class"] == "IfcSpace" and e["has_geometry"]]

    def at(self, point) -> str | None:
        p = np.asarray(point, float)
        hits = []
        for g in self.idx.query_box([*(p - 1e-6), *(p + 1e-6)]):
            e = self.idx.elements.get(g)
            if not e or e["ifc_class"] != "IfcSpace" or not e["has_geometry"]:
                continue
            if e.get("is_closed"):
                v, f = self.idx.mesh(g)
                if M.points_inside(p[None, :], v, f)[0]:
                    hits.append(g)
            else:
                hits.append(g)
        if not hits:
            return None
        return min(hits, key=lambda g: self.idx.elements[g]["volume"] or 1e18)  # le plus petit local

    def of_element(self, guid: str) -> str | None:
        e = self.idx.elements.get(guid)
        if not e or not e.get("aabb"):
            return None
        b = e["aabb"]
        return self.at([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, b[2] + min(0.1, (b[5] - b[2]) / 2)])
