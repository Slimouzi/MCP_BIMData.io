"""Calculs sur maillage triangulé (numpy) : AABB, OBB verticale, volume, fermeture.

Les coordonnées monde d'un projet géoréférencé sont grandes (≈ 10⁶ m) : tous les
calculs se font relativement au centre de l'élément pour garder la précision.
"""

from __future__ import annotations

import numpy as np

_MAX_HULL_FOR_CALIPERS = 4000


def aabb(verts: np.ndarray) -> list[float]:
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    return [float(x) for x in (*lo, *hi)]


def volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """Volume signé (théorème de la divergence) ; n'a de sens que si le maillage est fermé."""
    if len(faces) == 0:
        return 0.0
    v = verts - verts.mean(axis=0)
    a, b, c = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0))


def is_closed(verts: np.ndarray, faces: np.ndarray, decimals: int = 6) -> bool:
    """Fermé = chaque arête (non orientée) est partagée par exactement deux triangles.

    Les sommets sont soudés par arrondi (1 µm) : l'itérateur peut dupliquer un sommet
    par face selon les représentations.
    """
    if len(faces) < 4:
        return False
    _, inv = np.unique(np.round(verts, decimals), axis=0, return_inverse=True)
    f = inv.reshape(-1)[faces]
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]  # triangles dégénérés
    if len(f) < 4:
        return False
    edges = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return bool(np.all(counts == 2))


def _cross2(a, b) -> float:
    """Produit vectoriel 2D (scalaire) — np.cross en 2D est déprécié depuis NumPy 2."""
    return float(a[0] * b[1] - a[1] * b[0])


def _hull_2d(pts: np.ndarray) -> np.ndarray:
    """Enveloppe convexe 2D (chaîne monotone d'Andrew)."""
    pts = np.unique(np.round(pts, 6), axis=0)
    if len(pts) < 3:
        return pts
    pts = pts[np.lexsort((pts[:, 1], pts[:, 0]))]

    def half(points):
        h: list = []
        for p in points:
            while len(h) >= 2 and _cross2(h[-1] - h[-2], p - h[-2]) <= 0:
                h.pop()
            h.append(p)
        return h

    lower, upper = half(pts), half(pts[::-1])
    return np.array(lower[:-1] + upper[:-1])


def obb_vertical(verts: np.ndarray) -> dict:
    """Boîte orientée à axe vertical : rectangle d'aire minimale de l'emprise (calipers).

    Les éléments de bâtiment sont presque toujours verticaux (murs, portes, poteaux) :
    contraindre l'axe z donne des axes horizontaux stables (épaisseur / longueur d'un mur).
    ``axes[0]`` est la plus grande dimension horizontale.
    """
    c0 = verts.mean(axis=0)
    v = verts - c0
    xy = v[:, :2]
    hull = _hull_2d(xy)
    if len(hull) < 3:
        angles = np.array([0.0])
        hull = xy
    elif len(hull) > _MAX_HULL_FOR_CALIPERS:
        cov = np.cov(xy.T)
        w, vec = np.linalg.eigh(cov)
        angles = np.array([np.arctan2(vec[1, -1], vec[0, -1])])
    else:
        e = np.diff(np.vstack([hull, hull[:1]]), axis=0)
        angles = np.unique(np.round(np.mod(np.arctan2(e[:, 1], e[:, 0]), np.pi / 2), 9))
    cos, sin = np.cos(angles), np.sin(angles)
    # Projections de l'enveloppe sur (u, v) pour chaque angle candidat.
    pu = hull[:, 0][None, :] * cos[:, None] + hull[:, 1][None, :] * sin[:, None]
    pv = -hull[:, 0][None, :] * sin[:, None] + hull[:, 1][None, :] * cos[:, None]
    area = (pu.max(1) - pu.min(1)) * (pv.max(1) - pv.min(1))
    k = int(np.argmin(area))
    a = float(angles[k])
    u = np.array([np.cos(a), np.sin(a), 0.0])
    w = np.array([-np.sin(a), np.cos(a), 0.0])
    lu = (pu[k].min(), pu[k].max())
    lw = (pv[k].min(), pv[k].max())
    z = (v[:, 2].min(), v[:, 2].max())
    half = [(lu[1] - lu[0]) / 2, (lw[1] - lw[0]) / 2, (z[1] - z[0]) / 2]
    center = c0 + u * (lu[0] + lu[1]) / 2 + w * (lw[0] + lw[1]) / 2 + np.array([0, 0, (z[0] + z[1]) / 2])
    axes = [u, w, np.array([0.0, 0.0, 1.0])]
    if half[1] > half[0]:
        half[0], half[1] = half[1], half[0]
        axes[0], axes[1] = axes[1], -axes[0]
    return {"center": [round(float(x), 6) for x in center],
            "axes": [[round(float(x), 6) for x in ax] for ax in axes],
            "half": [round(float(x), 6) for x in half]}


def horizontal_area_up(verts: np.ndarray, faces: np.ndarray, cos_min: float = 0.99) -> float:
    """Aire des faces tournées vers le haut (≈ surface au sol d'un local fermé)."""
    if len(faces) == 0:
        return 0.0
    v = verts - verts.mean(axis=0)
    n = np.cross(v[faces[:, 1]] - v[faces[:, 0]], v[faces[:, 2]] - v[faces[:, 0]])
    norm = np.linalg.norm(n, axis=1)
    ok = norm > 0
    up = np.zeros(len(faces), bool)
    up[ok] = n[ok, 2] / norm[ok] >= cos_min
    return float(norm[up].sum() / 2)


# ── Requêtes sur maillage (phase fine du clash, dégagements, échappée) ─────

_EPS = 1e-9
#: Directions de rayon asymétriques (évitent les arêtes et diagonales de faces alignées sur les axes) :
#: un point est intérieur si au moins deux rayons sur trois le disent (robuste aux tangences).
_RAY_DIRS = [np.array(d) / np.linalg.norm(d) for d in ((0.8314, 0.4472, 0.3299), (-0.2711, 0.9137, 0.3027),
                                                      (0.3907, -0.1623, 0.9061))]
_CHUNK = 2_000_000  # couples (point, triangle) par paquet


def _tri_arrays(verts: np.ndarray, faces: np.ndarray):
    a = verts[faces[:, 0]]
    return a, verts[faces[:, 1]] - a, verts[faces[:, 2]] - a


def ray_hits(origins: np.ndarray, direction: np.ndarray, verts: np.ndarray, faces: np.ndarray,
             *, t_min: float = 1e-7) -> tuple[np.ndarray, np.ndarray]:
    """Rayons (Möller–Trumbore) : (nombre d'intersections, distance de la plus proche) par origine."""
    n = len(origins)
    count = np.zeros(n, np.int64)
    first = np.full(n, np.inf)
    if n == 0 or len(faces) == 0:
        return count, first
    a, e1, e2 = _tri_arrays(verts, faces)
    d = np.asarray(direction, float)
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > _EPS
    a, e1, e2, p, det = a[ok], e1[ok], e2[ok], p[ok], det[ok]
    if len(a) == 0:
        return count, first
    inv = 1.0 / det
    step = max(1, _CHUNK // len(a))
    for s in range(0, n, step):
        o = origins[s:s + step]
        tv = o[:, None, :] - a[None, :, :]
        u = np.einsum("ijk,jk->ij", tv, p) * inv
        q = np.cross(tv, e1[None, :, :])
        v = np.einsum("k,ijk->ij", d, q) * inv
        t = np.einsum("ijk,jk->ij", q, e2) * inv
        hit = (u >= 0) & (v >= 0) & (u + v <= 1) & (t > t_min)
        count[s:s + step] = hit.sum(axis=1)
        tt = np.where(hit, t, np.inf)
        first[s:s + step] = tt.min(axis=1)
    return count, first


def points_inside(points: np.ndarray, verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Points strictement à l'intérieur d'un maillage fermé (parité d'un rayon incliné)."""
    if len(points) == 0:
        return np.zeros(0, bool)
    votes = sum((ray_hits(points, d, verts, faces)[0] % 2 == 1).astype(int) for d in _RAY_DIRS)
    return votes >= 2


def edges_of(faces: np.ndarray) -> np.ndarray:
    e = np.sort(np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]]), axis=1)
    return np.unique(e, axis=0)


def segment_crossings(p0: np.ndarray, p1: np.ndarray, verts: np.ndarray, faces: np.ndarray, *,
                      contact: float = 0.0) -> np.ndarray:
    """Points où les segments [p0, p1] traversent les triangles (bornes exclues de 1e-9).

    ``contact`` : un segment presque dans le plan du triangle (ses deux bouts à moins de ``contact`` du plan) est un
    contact, pas une traversée — instable numériquement, il dépend du découpage des faces en triangles."""
    if len(p0) == 0 or len(faces) == 0:
        return np.zeros((0, 3))
    a, e1, e2 = _tri_arrays(verts, faces)
    out = []
    step = max(1, _CHUNK // len(a))
    for s in range(0, len(p0), step):
        o, dvec = p0[s:s + step], p1[s:s + step] - p0[s:s + step]
        pv = np.cross(dvec[:, None, :], e2[None, :, :])
        det = np.einsum("jk,ijk->ij", e1, pv)
        ok = np.abs(det) > _EPS
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o[:, None, :] - a[None, :, :]
        u = np.einsum("ijk,ijk->ij", tv, pv) * inv
        q = np.cross(tv, e1[None, :, :])
        v = np.einsum("ik,ijk->ij", dvec, q) * inv
        t = np.einsum("ijk,jk->ij", q, e2) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9) & (t < 1 - 1e-9)
        if contact > 0 and hit.any():
            n = np.cross(e1, e2)
            n /= np.linalg.norm(n, axis=1, keepdims=True).clip(1e-18)
            d0 = np.abs(np.einsum("ijk,jk->ij", tv, n))  # distance au plan de l'origine du segment
            d1 = np.abs(np.einsum("ijk,jk->ij", tv + dvec[:, None, :], n))  # … et de son autre bout
            hit &= np.maximum(d0, d1) > contact
        i, j = np.nonzero(hit)
        if len(i):
            out.append(o[i] + dvec[i] * t[i, j][:, None])
    return np.concatenate(out) if out else np.zeros((0, 3))


def faces_in_box(verts: np.ndarray, faces: np.ndarray, box: list[float]) -> np.ndarray:
    """Triangles dont l'AABB touche ``box`` (xmin, ymin, zmin, xmax, ymax, zmax)."""
    if len(faces) == 0:
        return faces
    tri = verts[faces]
    lo, hi = tri.min(axis=1), tri.max(axis=1)
    b0, b1 = np.asarray(box[:3]), np.asarray(box[3:])
    return faces[np.all(hi >= b0, axis=1) & np.all(lo <= b1, axis=1)]


def point_obb_distance(points: np.ndarray, obb: dict) -> np.ndarray:
    """Distance de points à une boîte orientée (0 à l'intérieur)."""
    c = np.asarray(obb["center"])
    ax = np.asarray(obb["axes"])
    h = np.asarray(obb["half"])
    local = (points - c) @ ax.T
    d = np.maximum(np.abs(local) - h, 0.0)
    return np.linalg.norm(d, axis=1)
