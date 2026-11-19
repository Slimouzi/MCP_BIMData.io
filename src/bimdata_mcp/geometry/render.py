"""Image d'un point de vue BCF, rendue côté serveur à partir des maillages de l'index (sans carte graphique).

La projection se construit **à partir de la caméra BCF elle-même** (``perspective_camera`` ou ``orthogonal_camera``) :
le code qui écrit la caméra est celui qui dessine l'image. Rendu par l'algorithme du peintre (triangles triés du plus
lointain au plus proche) avec Pillow, suréchantillonné ×2 puis réduit :

- éléments de contexte en gris clair translucide (opacité 0,3 : BCF 2.1 ne transmet pas la transparence) ;
- éléments en cause dans la couleur de leur rôle, opaques, ombrés, avec leurs arêtes vives ;
- coupe appliquée (boîte ou plan horizontal) : triangles hors de la zone écartés ;
- chemin mesuré (F3, F5) tracé ; cartouche en bas à gauche (règle, valeur, seuil).
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .index import GeometryIndex

WIDTH, HEIGHT = 1280, 720
SUPERSAMPLE = 2
CONTEXT_GREY = 190
CONTEXT_OPACITY = 0.3
PATH_COLOR = (0, 166, 80)
MAX_TRIANGLES = 400_000


def _vec(d: dict) -> np.ndarray:
    return np.array([d["x"], d["y"], d["z"]], float)


def camera_frame(vp: dict) -> dict:
    """Repère de la caméra du point de vue : œil, direction, haut, droite, type et ouverture."""
    if vp.get("perspective_camera"):
        c = vp["perspective_camera"]
        kind, extent = "perspective", float(c["field_of_view"])
    else:
        c = vp["orthogonal_camera"]
        kind, extent = "orthogonal", float(c["view_to_world_scale"])
    eye, d, up = _vec(c["camera_view_point"]), _vec(c["camera_direction"]), _vec(c["camera_up_vector"])
    d /= np.linalg.norm(d)
    up = up - (up @ d) * d
    up /= np.linalg.norm(up)
    right = np.cross(d, up)
    return {"kind": kind, "eye": eye, "d": d, "up": up, "right": right, "extent": extent}


def project(points: np.ndarray, cam: dict, width: int = WIDTH, height: int = HEIGHT) -> tuple[np.ndarray, np.ndarray]:
    """Pixels (x vers la droite, y vers le bas) et profondeur le long de la direction de visée."""
    rel = points - cam["eye"]
    z = rel @ cam["d"]
    x, y = rel @ cam["right"], rel @ cam["up"]
    if cam["kind"] == "perspective":
        f = (height / 2) / math.tan(math.radians(cam["extent"]) / 2)
        zs = np.where(z > 1e-6, z, 1e-6)
        px, py = width / 2 + f * x / zs, height / 2 - f * y / zs
    else:  # view_to_world_scale : hauteur visible, en mètres
        f = height / cam["extent"]
        px, py = width / 2 + f * x, height / 2 - f * y
    return np.stack([px, py], axis=-1), z


def _inside_clip(pts: np.ndarray, planes: list[dict]) -> np.ndarray:
    """Points du côté visible de tous les plans (la direction d'un plan pointe vers le demi-espace masqué)."""
    ok = np.ones(len(pts), bool)
    for p in planes:
        ok &= (pts - _vec(p["location"])) @ _vec(p["direction"]) <= 1e-6
    return ok


def _clip_triangles(tri: np.ndarray, planes: list[dict]) -> tuple[list[np.ndarray], list[int]]:
    """Polygones visibles après coupe : triangles entièrement du côté visible gardés tels quels, entièrement
    masqués par un plan écartés, à cheval découpés (Sutherland-Hodgman)."""
    if not planes or not len(tri):
        return list(tri), list(range(len(tri)))
    locs = np.array([_vec(p["location"]) for p in planes])
    dirs = np.array([_vec(p["direction"]) for p in planes])
    dist = np.einsum("tvk,pk->tvp", tri, dirs) - np.einsum("pk,pk->p", locs, dirs)[None, None, :]  # > 0 : masqué
    inside_all = (dist <= 1e-9).all(axis=(1, 2))
    out_any = (dist > 1e-9).all(axis=1).any(axis=1)
    polys, src = list(tri[inside_all]), list(np.nonzero(inside_all)[0])
    cut = np.nonzero(~inside_all & ~out_any)[0]
    for j, t, dd in zip(cut, tri[cut], dist[cut], strict=True):
        poly = [(t[i], dd[i]) for i in range(3)]
        for k in range(len(planes)):
            if not poly:
                break
            new = []
            for i in range(len(poly)):
                (a, da), (b, db) = poly[i], poly[(i + 1) % len(poly)]
                if da[k] <= 1e-9:
                    new.append((a, da))
                if (da[k] <= 1e-9) != (db[k] <= 1e-9):
                    u = da[k] / (da[k] - db[k])
                    new.append((a + u * (b - a), da + u * (db - da)))
            poly = new
        if len(poly) >= 3:
            polys.append(np.array([p for p, _ in poly]))
            src.append(int(j))
    return polys, src


def _clip_segment(seg: np.ndarray, planes: list[dict]) -> np.ndarray | None:
    """Partie visible d'un segment après coupe (None : entièrement masqué)."""
    a, b = seg[0].astype(float), seg[1].astype(float)
    t0, t1 = 0.0, 1.0
    for p in planes:
        n, loc = _vec(p["direction"]), _vec(p["location"])
        da, db = (a - loc) @ n, (b - loc) @ n
        if da > 1e-9 and db > 1e-9:
            return None
        if da > 1e-9 or db > 1e-9:
            t = da / (da - db)
            if da > 1e-9:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    if t0 >= t1:
        return None
    return np.array([a + t0 * (b - a), a + t1 * (b - a)])


def _font(size: int):
    from PIL import ImageFont

    try:  # police Unicode livrée avec matplotlib (tirets, accents)
        import matplotlib

        return ImageFont.truetype(str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"), size)
    except Exception:  # noqa: BLE001
        try:
            return ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1
            return ImageFont.load_default()


def _feature_edges(tri: np.ndarray, angle_deg: float = 30.0) -> np.ndarray:
    """Arêtes vives (bords et plis de plus de ``angle_deg``) d'un maillage triangulé — segments (n, 2, 3)."""
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True).clip(1e-12)
    edges: dict[tuple, list[int]] = {}
    keys = np.round(tri, 5)
    for k, t in enumerate(keys):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            e = tuple(sorted((tuple(t[a]), tuple(t[b]))))
            edges.setdefault(e, []).append(k)
    cos = math.cos(math.radians(angle_deg))
    out = []
    for e, fs in edges.items():
        if len(fs) == 1 or (len(fs) >= 2 and abs(float(n[fs[0]] @ n[fs[1]])) < cos):
            out.append(e)
    return np.array(out, float).reshape(-1, 2, 3)


def _shade(tri: np.ndarray, cam: dict) -> np.ndarray:
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.linalg.norm(n, axis=1, keepdims=True).clip(1e-12)
    light = -cam["d"] + 0.4 * cam["up"] + 0.3 * cam["right"]
    light /= np.linalg.norm(light)
    return 0.55 + 0.45 * np.abs(n @ light)


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")[-6:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def render_viewpoint(idx: GeometryIndex, vp: dict, roles: dict[str, str], out: Path, *, region: list[float],
                     lines: list[list[list[float]]] | None = None, caption: str = "",
                     offset: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> dict:
    """Rend le point de vue ``vp`` (format BCF) dans ``out`` (PNG).

    ``roles`` : clé d'index → couleur hexadécimale des éléments en cause ; ``region`` : boîte (repère IFC) dont les
    éléments sont rendus ; ``offset`` : décalage repère visualiseur − repère IFC (la caméra est dans le repère du
    visualiseur, les maillages dans celui de l'IFC)."""
    from PIL import Image, ImageDraw

    off = np.asarray(offset, float)
    cam = camera_frame(vp)
    planes = vp.get("clipping_planes") or []
    W, H = WIDTH * SUPERSAMPLE, HEIGHT * SUPERSAMPLE
    keys = set(idx.query_box(region)) | set(roles)
    ctx_polys, hit_polys, hit_edges = [], [], []
    n_tri = 0
    for k in keys:
        e = idx.elements.get(k)
        if not e or not e.get("has_geometry") or e.get("from_parts"):
            continue
        if e["ifc_class"] == "IfcSpace" and k not in roles:
            continue  # locaux : seulement quand ils sont en cause
        m = idx.mesh(k)
        if m is None:
            continue
        tri = m[0][m[1]] + off
        if not len(tri) or n_tri > MAX_TRIANGLES:
            continue
        shade_all = _shade(tri, cam)  # ombrage du triangle d'origine (la coupe ne change pas sa normale)
        polys, src = _clip_triangles(tri, planes)
        shades = [shade_all[j] for j in src]
        if not polys:
            continue
        n_tri += len(polys)
        is_hit = k in roles
        if is_hit:
            r, g, b = _rgb(roles[k])
            for seg in _feature_edges(tri):
                seg = _clip_segment(seg, planes)
                if seg is None:
                    continue
                sp, sz = project(seg, cam, W, H)
                if cam["kind"] == "perspective" and (sz <= 0.05).any():
                    continue
                hit_edges.append((float(sz.mean()), [tuple(q) for q in sp], (int(r * 0.45), int(g * 0.45), int(b * 0.45))))
        for p, sh in zip(polys, shades, strict=True):
            xy, z = project(p, cam, W, H)
            if cam["kind"] == "perspective" and z.min() <= 0.05:
                continue
            pts = [tuple(q) for q in xy]
            if is_hit:
                hit_polys.append((float(z.mean()), pts, (int(r * sh), int(g * sh), int(b * sh))))
            else:
                v = int(CONTEXT_GREY * sh)
                ctx_polys.append((float(z.mean()), pts, (v, v, v)))
    white = Image.new("RGB", (W, H), (255, 255, 255))
    ctx = white.copy()
    draw = ImageDraw.Draw(ctx)
    for _, poly, col in sorted(ctx_polys, key=lambda t: -t[0]):
        draw.polygon(poly, fill=col)
    img = Image.blend(white, ctx, CONTEXT_OPACITY)
    draw = ImageDraw.Draw(img)
    items = [(d, "p", poly, col) for d, poly, col in hit_polys] + [(d - 1e-3, "e", seg, col) for d, seg, col in hit_edges]
    for _, kind, geom, col in sorted(items, key=lambda t: -t[0]):
        if kind == "p":
            draw.polygon(geom, fill=col)
        else:
            draw.line(geom, fill=col, width=SUPERSAMPLE * 2)
    for poly in lines or []:
        pts = np.asarray(poly, float) + off
        xy, z = project(pts, cam, W, H)
        if cam["kind"] == "perspective" and (z <= 0.05).any():
            continue
        draw.line([tuple(q) for q in xy], fill=PATH_COLOR, width=SUPERSAMPLE * 4, joint="curve")
        for q in (xy[0], xy[-1]):
            r = SUPERSAMPLE * 7
            draw.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=PATH_COLOR)
    img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    if caption:
        d2 = ImageDraw.Draw(img)
        font = _font(18)
        x0, y0, x1, y1 = d2.textbbox((14, HEIGHT - 38), caption, font=font)
        d2.rectangle([x0 - 8, y0 - 6, x1 + 8, y1 + 6], fill=(255, 255, 255), outline=(120, 120, 120))
        d2.text((14, HEIGHT - 38), caption, fill=(30, 30, 30), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=True)
    return {"path": str(out), "triangles": n_tri, "bytes": out.stat().st_size}
