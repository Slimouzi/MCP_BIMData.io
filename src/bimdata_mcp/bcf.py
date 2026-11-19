"""Topics BCF 2.1 avec point de vue complet, à la manière d'une diapositive d'anomalie Solibri.

Chaque point de vue porte une caméra, la sélection de **tous** les éléments de l'anomalie (dans chaque point de vue du
topic, contrairement au BCF Live Connector de Solibri), leurs couleurs selon le rôle, une visibilité explicite, la
coupe, le chemin mesuré (F3, F5) et une image rendue côté serveur avec la même caméra (:mod:`.geometry.render`).

Caméra (méthode documentée, repère du visualiseur, mètres et degrés) :

1. zone = boîte de l'anomalie (intersection A∩B pour un conflit, élément ou zone mesurée sinon) ;
2. marge = max(0,5 m ; 25 % de la diagonale), côté minimal 2 m — la même zone dilatée sert à la boîte de coupe ;
3. C = centre, R = demi-diagonale de la zone dilatée ;
4. direction d = normalise(−1 ; −1 ; −0,7) (vue aux trois quarts, depuis le côté x+ y+) ;
5. distance D = R / sin(φ/2) (φ = angle vertical, 60° par défaut) ; position P = C − D·d ;
6. vecteur haut u = normalise(z − (z·d)·d).

Plan orthogonal : d = (0 ; 0 ; −1), u = (0 ; 1 ; 0), ``view_to_world_scale`` = hauteur visible en mètres
= max(profondeur ; largeur × 9/16) de la zone dilatée. Coupe : six plans autour de la zone (la direction d'un plan
pointe vers le demi-espace masqué), ou plan horizontal à l'étage + 1,20 m pour PMR, dégagements et incendie.
Cible : BCF 2.1, la seule version acceptée par BIMData et la plus sûre pour Solibri. Tout ce qui doit arriver dans
Solibri passe par un champ standard (caméra, clipping_planes, lines, components, snapshot, reference_links) ; les champs
propres à BIMData (pins, models) servent l'affichage dans BIMData seulement — le repère est doublé d'une croix de trois
segments en ``lines``. Coordonnées dans le repère de l'IFC, en mètres, sans décalage. GUID du topic et des points de
vue dérivés de la clé d'anomalie (UUID v5) : même anomalie, même GUID à chaque relance.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from importlib.metadata import PackageNotFoundError, version

import numpy as np

MARGIN_MIN_M = 0.5
MARGIN_RATIO = 0.25
MIN_SIDE_M = 2.0
ISO_DIRECTION = (-1.0, -1.0, -0.7)
PLAN_CUT_M = 1.20
ASPECT = 16 / 9
MAX_COMPONENTS = 1000

RED, ORANGE, BLUE = "FF0000", "FFA500", "0000FF"
#: Espace de noms des GUID dérivés (UUID version 5) : même anomalie → même GUID de topic à chaque relance.
GUID_NAMESPACE = uuid.UUID("5b0e7c1e-3f4a-5d2b-9c8e-6a1f2b3c4d5e")
CROSS_HALF_M = 0.25  # croix de 3 segments de 0,5 m au centre de la zone (repère lisible dans Solibri)
#: Couleur selon le rôle de l'élément dans l'anomalie.
ROLE_RED = {"a", "cible", "élément", "porte/fenêtre", "mur", "local", "porte", "réseau", "volume de zone", "paroi"}
ROLE_ORANGE = {"b", "obstacle", "doublon", "support", "hôte", "dalle", "réservation"}
ROLE_BLUE = {"contexte", "escalier", "sortie", "local traversé"}

FAMILIES = {
    "clash": {"label": "Clash", "camera": "iso", "section": "box", "extra": "plan", "spaces": False},
    "structure": {"label": "Structure", "camera": "iso", "section": "box", "extra": None, "spaces": False},
    "geometrie": {"label": "Géométrie", "camera": "iso", "section": "box", "extra": None, "spaces": False},
    "information": {"label": "Information", "camera": "iso", "section": None, "extra": None, "spaces": False},
    "pmr": {"label": "PMR", "camera": "plan", "section": "plane", "extra": "iso", "spaces": True},
    "degagements": {"label": "Dégagements", "camera": "plan", "section": "plane", "extra": "iso", "spaces": True},
    "incendie": {"label": "Incendie", "camera": "plan", "section": "plane", "extra": None, "spaces": True},
}
PRIORITY = {"bloquant": "Critical", "critique": "Critical", "erreur": "High", "majeur": "High",
            "avertissement": "Medium", "mineur": "Low", "info": "Information"}


#: Origine d'un point de vue reconnue par le viewer BIMData : « ifc3d » ouvre la vue dans la fenêtre 3D, avec la
#: maquette du topic (une autre valeur : fenêtre inconnue, avertissement « mise en page non disponible »).
VIEWER_ORIGIN = "ifc3d"


def originating_system() -> str:
    try:
        return f"MCP BIMData {version('bimdata-mcp')}"
    except PackageNotFoundError:  # pragma: no cover
        return "MCP BIMData"


def viewer_offset(value=None) -> tuple[float, float, float]:
    """Coordonnées BCF dans le repère de l'IFC, en mètres, **sans décalage** : c'est ce qu'attend Solibri. Un écart
    affiché par le visualiseur BIMData est une limite du visualiseur, à signaler, pas à compenser. Seul
    MCP_BCF_VIEWER_OFFSET (diagnostic de recette, à laisser à 0) peut en introduire un."""
    raw = os.getenv("MCP_BCF_VIEWER_OFFSET", "")
    v = [float(x) for x in raw.split(",")] if raw.strip() else [0.0, 0.0, 0.0]
    if len(v) != 3:
        raise ValueError("MCP_BCF_VIEWER_OFFSET : trois valeurs (dx, dy, dz) en mètres.")
    return v[0], v[1], v[2]


def topic_guid(project_id, model_id, key: str) -> str:
    """GUID du topic dérivé de la clé stable (UUID v5 sur projet + maquette + clé)."""
    return str(uuid.uuid5(GUID_NAMESPACE, f"{project_id}:{model_id}:{key}"))


def viewpoint_guid(topic: str, index: int, view_hash: str) -> str:
    """GUID d'un point de vue : un nouveau point de vue (vue recalculée) a un nouveau GUID."""
    return str(uuid.uuid5(GUID_NAMESPACE, f"{topic}:vp:{index}:{view_hash}"))


def reference_urn(rule: str, guids, key: str) -> str:
    """Clé lisible : urn:mcp-bimdata:<règle>:<GlobalId triés> (repli sur la clé si la liste est trop longue)."""
    ids = sorted({str(g).split("#", 1)[0] for g in guids if g})
    urn = f"urn:mcp-bimdata:{rule}:" + "+".join(ids)
    return urn if len(urn) <= 240 else f"urn:mcp-bimdata:{rule}:key:{key}"


def key_from_reference(link: str) -> str | None:
    """Clé d'anomalie relue dans un lien de référence (URN actuel ou ancien lien mcp://check/…/issue/<clé>)."""
    link = str(link or "")
    m = re.fullmatch(r"urn:mcp-bimdata:([^:]+):key:([0-9a-f]{16})", link)
    if m:
        return m.group(2)
    m = re.fullmatch(r"urn:mcp-bimdata:([^:]+):([0-9A-Za-z_$+]+)", link)
    if m:
        return issue_key(m.group(1), m.group(2).split("+"))
    m = re.search(r"mcp://check/[^/\s]+/issue/([0-9a-f]{16})", link)
    return m.group(1) if m else None


def family_of(rule: str | None) -> str:
    r = (rule or "").upper()
    if r == "CLASH":
        return "clash"
    if re.match(r"^S\d+$", r):
        return "structure"
    if re.match(r"^G\d+$", r):
        return "geometrie"
    if re.match(r"^A\d+$", r):
        return "pmr"
    if r in ("DEG", "DIST"):
        return "degagements"
    if re.match(r"^F\d+$", r):
        return "incendie"
    return "information"


def role_color(role: str | None) -> str:
    r = (role or "").strip().lower()
    if r in ROLE_ORANGE:
        return ORANGE
    if r in ROLE_BLUE:
        return BLUE
    return RED


def issue_key(rule: str, guids) -> str:
    """Clé d'anomalie stable : haché du code de règle et des GlobalId triés des éléments en cause."""
    base = (rule or "").upper() + "|" + "|".join(sorted({str(g).split("#", 1)[0] for g in guids if g}))
    return hashlib.sha1(base.encode()).hexdigest()[:16]


# ── Géométrie du point de vue ──────────────────────────────────────────


def dilate(bbox: list[float]) -> list[float]:
    """Zone dilatée : marge max(0,5 m ; 25 % de la diagonale), côté minimal 2 m."""
    lo, hi = np.array(bbox[:3], float), np.array(bbox[3:], float)
    m = max(MARGIN_MIN_M, MARGIN_RATIO * float(np.linalg.norm(hi - lo)))
    lo, hi = lo - m, hi + m
    c, half = (lo + hi) / 2, np.maximum((hi - lo) / 2, MIN_SIDE_M / 2)
    return [*(c - half), *(c + half)]


def _xyz(v) -> dict:
    return {"x": round(float(v[0]), 4), "y": round(float(v[1]), 4), "z": round(float(v[2]), 4)}


def _unit(v) -> np.ndarray:
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    if n < 1e-12:
        raise ValueError("Vecteur de direction nul.")
    return v / n


def perspective(zone: list[float], fov_deg: float = 60.0, direction=ISO_DIRECTION, off=(0, 0, 0)) -> dict:
    lo, hi = np.array(zone[:3]), np.array(zone[3:])
    c, r = (lo + hi) / 2 + np.asarray(off), float(np.linalg.norm(hi - lo)) / 2
    d = _unit(direction)
    dist = r / math.sin(math.radians(fov_deg) / 2)
    up = _unit(np.array([0.0, 0.0, 1.0]) - d[2] * d)
    return {"camera_view_point": _xyz(c - dist * d), "camera_direction": _xyz(d), "camera_up_vector": _xyz(up),
            "field_of_view": float(fov_deg)}


def orthogonal_plan(zone: list[float], off=(0, 0, 0)) -> dict:
    lo, hi = np.array(zone[:3]), np.array(zone[3:])
    c = (lo + hi) / 2 + np.asarray(off)
    width, depth = hi[0] - lo[0], hi[1] - lo[1]
    eye = np.array([c[0], c[1], hi[2] + off[2] + 5.0])
    return {"camera_view_point": _xyz(eye), "camera_direction": _xyz((0, 0, -1)), "camera_up_vector": _xyz((0, 1, 0)),
            "view_to_world_scale": round(float(max(depth, width / ASPECT)), 4)}


def orthogonal_elevation(zone: list[float], off=(0, 0, 0)) -> dict:
    lo, hi = np.array(zone[:3]), np.array(zone[3:])
    c = (lo + hi) / 2 + np.asarray(off)
    width, height = hi[0] - lo[0], hi[2] - lo[2]
    eye = np.array([c[0], lo[1] + off[1] - 5.0, c[2]])
    return {"camera_view_point": _xyz(eye), "camera_direction": _xyz((0, 1, 0)), "camera_up_vector": _xyz((0, 0, 1)),
            "view_to_world_scale": round(float(max(height, width / ASPECT)), 4)}


def box_planes(zone: list[float], off=(0, 0, 0)) -> list[dict]:
    """Six plans autour de la zone ; direction vers l'extérieur = demi-espace masqué."""
    lo, hi = np.array(zone[:3]) + off, np.array(zone[3:]) + off
    c = (lo + hi) / 2
    out = []
    for k in range(3):
        for sign, edge in ((-1.0, lo[k]), (1.0, hi[k])):
            loc, n = c.copy(), np.zeros(3)
            loc[k], n[k] = edge, sign
            out.append({"location": _xyz(loc), "direction": _xyz(n)})
    return out


def horizontal_plane(z: float, center, off=(0, 0, 0)) -> list[dict]:
    """Plan horizontal à l'altitude ``z`` : le dessus est masqué."""
    loc = np.array([center[0], center[1], z]) + off
    return [{"location": _xyz(loc), "direction": _xyz((0, 0, 1))}]


# ── Composants ─────────────────────────────────────────────────────────


def components(elements: list[dict], *, model_id, isolate: bool, spaces_visible: bool) -> dict:
    """Sélection (tous les éléments), couleurs par rôle, visibilité toujours explicite."""
    orig = originating_system()
    try:
        mid = int(model_id)
    except (TypeError, ValueError):
        mid = None

    def comp(e: dict) -> dict:
        c = {"ifc_guid": str(e["uuid"]).split("#", 1)[0], "originating_system": orig}
        if mid is not None:  # convention BIMData (comme les Smart Views) : identifiant de la maquette
            c["authoring_tool_id"] = str(mid)
        return c

    seen, sel = set(), []
    by_color: dict[str, list[dict]] = {}
    for e in elements:
        key = e["uuid"]
        if key in seen:
            continue
        seen.add(key)
        c = comp(e)
        sel.append(c)
        by_color.setdefault(role_color(e.get("role")), []).append(c)
    vis = ({"default_visibility": False, "exceptions": sel} if isolate else
           {"default_visibility": True, "exceptions": []})
    vis["view_setup_hints"] = {"spaces_visible": False, "space_boundaries_visible": False, "openings_visible": False}
    return {"selection": sel, "coloring": [{"color": col, "components": cs} for col, cs in by_color.items()],
            "visibility": vis}


def _fmt(v) -> str:
    if isinstance(v, (int, float)):
        txt = f"{v:.3f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:.2f}".rstrip("0").rstrip(".")
        return txt.replace(".", ",")
    return str(v)


def caption(issue: dict) -> str:
    """Cartouche : « F3 — 37,7 m > 30 m » quand le sens de la comparaison est clair, sinon mesure et seuil."""
    rule = issue.get("rule_id") or issue.get("req_id") or ""
    m, t = issue.get("measure") or {}, issue.get("threshold") or {}
    v, tv, unit = m.get("value"), t.get("value"), m.get("unit") or t.get("unit") or ""
    expected = str(issue.get("expected") or "")
    if isinstance(v, (int, float)) and isinstance(tv, (int, float)):
        if v > tv and not expected.startswith("≥"):
            return f"{rule} — {_fmt(v)} {unit} > {_fmt(tv)} {unit}".strip()
        if v < tv and expected.startswith("≥"):
            return f"{rule} — {_fmt(v)} {unit} < {_fmt(tv)} {unit}".strip()
        return f"{rule} — {_fmt(v)} {unit} (seuil {_fmt(tv)} {unit})".strip()
    if isinstance(v, (int, float)):
        return f"{rule} — {_fmt(v)} {unit}".strip()
    return f"{rule} — {(issue.get('message') or '')[:70]}"


def viewpoint(issue: dict, elements: list[dict], *, zone: list[float], view: str, section: str | None,
              storey_z: float | None, fov_deg: float, isolate: bool, family: str, model_id,
              off=(0.0, 0.0, 0.0), path3d: list[list[float]] | None = None) -> dict:
    """Point de vue BCF 2.1 : ``view`` iso | plan | elevation ; ``section`` box | plane | None."""
    fam = FAMILIES[family]
    vp: dict = {"originating_system": VIEWER_ORIGIN,
                "components": components(elements, model_id=model_id, isolate=isolate,
                                         spaces_visible=fam["spaces"])}
    if view == "plan":
        vp["orthogonal_camera"] = orthogonal_plan(zone, off)
    elif view == "elevation":
        vp["orthogonal_camera"] = orthogonal_elevation(zone, off)
    else:
        vp["perspective_camera"] = perspective(zone, fov_deg, off=off)
    c = [(zone[i] + zone[i + 3]) / 2 for i in range(3)]
    if section == "box":
        vp["clipping_planes"] = box_planes(zone, off)
    elif section == "plane":
        cut = (storey_z if storey_z is not None else zone[2]) + PLAN_CUT_M
        vp["clipping_planes"] = horizontal_plane(cut, c, off)
    elif section == "center":  # plan au point de conflit : coupe à mi-hauteur de la zone
        vp["clipping_planes"] = horizontal_plane(c[2], c, off)
    lines = []
    if path3d:
        pts = np.asarray(path3d, float) + np.asarray(off)
        lines += [{"start_point": _xyz(a), "end_point": _xyz(b)} for a, b in zip(pts[:-1], pts[1:], strict=True)]
    center = np.asarray(issue.get("where", {}).get("center") or c, float) + np.asarray(off)
    for k in range(3):  # croix de 3 segments de 0,5 m : le repère tel que Solibri le relit (lines standard)
        d = np.zeros(3)
        d[k] = CROSS_HALF_M
        lines.append({"start_point": _xyz(center - d), "end_point": _xyz(center + d)})
    vp["lines"] = lines
    rule = issue.get("rule_id") or issue.get("req_id") or ""
    vp["pins"] = [{"point": _xyz(center), "name": str(rule)[:256], "color": RED.lower()}]  # extension BIMData
    return vp


def validate_viewpoint(vp: dict, *, snapshot: bool = True) -> list[str]:
    """Contrôle de forme d'un point de vue BCF API 2.1 (champs, types, vecteurs non nuls, couleurs)."""
    errs = []
    cam = vp.get("perspective_camera") or vp.get("orthogonal_camera")
    if not cam:
        errs.append("caméra absente")
    else:
        for k in ("camera_view_point", "camera_direction", "camera_up_vector"):
            v = cam.get(k)
            if not isinstance(v, dict) or set(v) != {"x", "y", "z"}:
                errs.append(f"{k} invalide")
        if not errs:
            d, u = np.array(list(cam["camera_direction"].values())), np.array(list(cam["camera_up_vector"].values()))
            if np.linalg.norm(d) < 1e-9 or np.linalg.norm(u) < 1e-9:
                errs.append("vecteur nul")
            elif abs(float(d @ u)) > 1e-3:
                errs.append("vecteur haut non perpendiculaire à la direction")
        if "perspective_camera" in vp and not 0 < float(cam.get("field_of_view", 0)) < 180:
            errs.append("field_of_view hors de ]0 ; 180[")
        if "orthogonal_camera" in vp and not float(cam.get("view_to_world_scale", 0)) > 0:
            errs.append("view_to_world_scale ≤ 0")
    comps = vp.get("components") or {}
    if not comps.get("selection"):
        errs.append("sélection vide")
    for s in comps.get("selection") or []:
        if not s.get("ifc_guid"):
            errs.append("ifc_guid manquant")
    for g in comps.get("coloring") or []:
        if not re.fullmatch(r"([0-9A-F]{6}|[0-9A-F]{8})", str(g.get("color", ""))):
            errs.append(f"couleur invalide {g.get('color')!r}")
    vis = comps.get("visibility")
    if not isinstance(vis, dict) or "default_visibility" not in vis or "exceptions" not in vis:
        errs.append("visibilité non explicite")
    for p in vp.get("clipping_planes") or []:
        if set(p) != {"location", "direction"}:
            errs.append("plan de coupe invalide")
    snap = vp.get("snapshot")
    if vp.get("originating_system") != VIEWER_ORIGIN:
        errs.append("origine non reconnue par le viewer BIMData (attendu « ifc3d »)")
    if snapshot and (not snap or snap.get("snapshot_type") != "png" or not snap.get("snapshot_data")):
        errs.append("image absente")
    return errs
