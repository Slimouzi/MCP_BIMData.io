"""Lot 3 : distances (règle Solibri 222), dégagements (226 / 248), accessibilité PMR (A1…A6), incendie (F1…F4).

Les seuils réglementaires sont **indicatifs** (spécification : à faire valider par un spécialiste avant
mise en service) : ils sont tous paramétrables et chaque anomalie indique le seuil appliqué et sa source.
Les règles « à paramétrer » (A2, A4) ne tournent que si leurs dimensions sont fournies. Aide à la décision,
pas attestation de conformité : F3 est une distance de marche indicative sur grille (mobilier ignoré).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np

from ..geometry import mesh as M
from ..geometry import zones as Z
from ..geometry.index import GeometryIndex
from ..model_view import Record, resolve_field
from ..selection import _num, as_bool
from .engine import CheckResult
from .rules import RULES, SHAFT_PATTERN, Rule, _first_number, _Run

for _r in (
    Rule("DIST", "Distance entre composants", "Maillage", "majeur", "distance",
         "Distance minimale (ou maximale) entre deux ensembles (équivalent règle Solibri 222)."),
    Rule("DEG", "Espace libre devant les composants", "Maillage", "majeur", "dégagement",
         "Zone devant / derrière / autour d'un composant libre d'obstacles (équivalent règles 226 et 248)."),
    Rule("A1", "Largeur nominale des portes", "Données", "majeur", "PMR", "OverallWidth contre le référentiel."),
    Rule("A2", "Espace de manœuvre de porte", "Maillage", "majeur", "PMR",
         "Rectangle libre devant la porte (largeur, longueur côté poussant / tirant) — à paramétrer."),
    Rule("A3", "Espace de demi-tour", "Bbox", "majeur", "PMR", "Cercle libre de 1,50 m dans les locaux désignés."),
    Rule("A4", "Espace d'usage devant WC et lavabo", "Maillage", "majeur", "PMR",
         "Rectangle libre devant l'équipement — à paramétrer."),
    Rule("A5", "Marches et giron", "Données", "majeur", "PMR", "Hauteur de marche et giron contre le référentiel."),
    Rule("A6", "Propriété HandicapAccessible", "Données", "mineur", "PMR",
         "HandicapAccessible renseignée sur les portes et locaux désignés."),
    Rule("A7", "Ressaut au seuil des portes", "Maillage", "info", "PMR",
         "Écart entre le seuil de la porte et le dessus du sol de chaque côté (2 cm) — information tant que le "
         "ressaut peut venir de la modélisation des sols."),
    Rule("F1", "Degré coupe-feu renseigné et suffisant", "Données", "majeur", "incendie",
         "FireRating présent et au moins égal à l'exigence (par classe ou par zone)."),
    Rule("F2", "Parois entre compartiments", "Maillage", "majeur", "incendie",
         "Murs et dalles séparant deux compartiments, et leurs portes, au moins au degré exigé."),
    Rule("F3", "Distance indicative à la sortie", "Maillage", "majeur", "incendie",
         "Distance suivant l'axe des circulations du point de départ (point le plus éloigné du local, ou porte palière "
         "en habitation) à la sortie cible du référentiel (escalier, escalier protégé, sortie extérieure), avec le "
         "seuil du référentiel (cul-de-sac compris). Indicatif."),
    Rule("F5", "Cul-de-sac", "Maillage", "majeur", "incendie",
         "Longueur parcourue avant d'avoir le choix entre deux sorties distinctes (ou issue unique du niveau)."),
    Rule("F6", "Espaces d'attente sécurisés", "Maillage", "majeur", "incendie",
         "Chaque local atteint un EAS (ou une sortie extérieure au niveau d'accès) sans marche ni porte étroite, dans "
         "les distances de F3 ; surface des EAS suffisante pour l'effectif du niveau."),
    Rule("F4", "Largeur des sorties et escaliers", "Données", "majeur", "incendie",
         "Largeur ≥ unités de passage exigées (1 UP = 0,90 m, 2 UP = 1,40 m, n ≥ 3 : 0,60 m × n)."),
):
    RULES[_r.id] = _r

#: Référentiels PMR — valeurs de départ de la spécification, à faire valider.
REFERENTIELS = {
    "BHC 2015": {"door_width_entry_m": 0.90, "door_width_interior_m": 0.80, "turning_diameter_m": 1.50,
                 "riser_max_m": 0.17, "tread_min_m": 0.28},
    "ERP 2017": {"door_width_entry_m": 0.90, "door_width_interior_m": 0.90, "turning_diameter_m": 1.50,
                 "riser_max_m": 0.16, "tread_min_m": 0.28},
}
PMR_DEFAULTS = {"turning_height_m": 0.70, "turning_step_m": 0.10, "a2_width_m": None, "a2_push_m": None,
                "a2_pull_m": None, "a4_width_m": None, "a4_depth_m": None, "threshold_step_max_m": 0.02,
                "clearance_tolerance_m": 0.01}
SANITARY_RE = re.compile(r"\bwc\b|toilet|cuvette|lavabo|vasque|basin|sink|évier|evier|urinoir", re.IGNORECASE)
SWING = ("SWING", "DOUBLE_DOOR")
#: Ressaut toléré au seuil : un sol ou revêtement dont le dessus dépasse la base de la zone de moins de 2 cm
#: n'est pas un obstacle.
FLOOR_STEP_M = 0.02
#: Sols minces au niveau du seuil (dalle ou revêtement ≤ 10 cm d'épaisseur, dessus ≤ 10 cm au-dessus du seuil) :
#: jamais des obstacles de dégagement — la marche au seuil relève de la règle A7 « ressaut ».
THIN_FLOOR_M = 0.10
BALCONY_RE = re.compile(r"balcon|loggia|terrasse|terrace", re.IGNORECASE)
STAIR_SPACE_RE = re.compile(r"stair|escalier|\bcage\b|\besc\b", re.IGNORECASE)
STRUCT_OBSTACLE_SKIP = ("IfcSpace", "IfcOpeningElement", "IfcVirtualElement")


def _is(cls: str, bases: tuple[str, ...]) -> bool:
    return any(cls == b or cls.startswith(b) for b in bases)


def _new_run(rules: list[str], records, idx) -> _Run:
    run = _Run(rules=[RULES[r] for r in rules], records=records, idx=idx)
    if idx is not None:
        for s in idx.ifc.get("storeys") or []:
            run.storey_label.setdefault(s["guid"], s["name"])
    return run


# ── Distance (DIST) ─────────────────────────────────────────────────────


def run_distance_rules(idx: GeometryIndex, records: dict[str, Record], set_a: list[str], set_b: list[str], *,
                       mode: str, distance_m: float, direction: str = "any",
                       same_space_only: bool = False) -> tuple[CheckResult, list[str]]:
    run = _new_run(["DIST"], records, idx)
    loc = Z.SpaceLocator(idx) if same_space_only else None
    space_of: dict[str, str | None] = {}

    def sp(g):
        if g not in space_of:
            space_of[g] = loc.of_element(g)
        return space_of[g]

    reach = distance_m + 0.01 if mode == "min" else max(distance_m * 4, 20.0)
    fps: dict[str, np.ndarray] = {}

    def fp(g):
        if g not in fps:
            fps[g] = Z.footprint(idx, g)
        return fps[g]

    for a in set_a:
        ea = idx.elements.get(a)
        if not ea or not ea["has_geometry"]:
            continue
        run.seen("DIST", a)
        b0 = ea["aabb"]
        box = [b0[0] - reach, b0[1] - reach, b0[2] - reach, b0[3] + reach, b0[4] + reach, b0[5] + reach]
        if direction in ("horizontal", "below", "above"):  # en plan / à la verticale : toute la hauteur
            box[2], box[5] = -1e9, 1e9
        cand = set(idx.query_box(box)) & set(set_b)
        cand.discard(a)
        if same_space_only:
            cand = {b for b in cand if sp(b) is not None and sp(b) == sp(a)}
        best, who, pts = math.inf, None, (None, None)
        for b in cand:
            eb = idx.elements[b]
            if not eb["has_geometry"]:
                continue
            if direction == "horizontal":
                d, p = Z.horizontal_distance(fp(a), fp(b)), (None, None)
            elif direction in ("below", "above"):
                if not Z.footprints_overlap(fp(a), fp(b)):
                    continue
                d = (b0[2] - eb["aabb"][5]) if direction == "below" else (eb["aabb"][2] - b0[5])
                if d < -1e-6:
                    continue
                d, p = max(d, 0.0), (None, None)
            else:
                d, pa, pb = Z.mesh_distance(idx, a, b, max_d=reach)
                p = (pa, pb)
            if d < best:
                best, who, pts = d, b, p
        what = {"any": "", "horizontal": " en plan", "below": " (dessous)", "above": " (dessus)"}[direction]
        if mode == "min" and who is not None and best < distance_m - 1e-9:
            eb = idx.elements[who]
            bbox = None
            if pts[0] is not None:
                q = np.vstack(pts)
                bbox = [*(q.min(axis=0) - 0.01), *(q.max(axis=0) + 0.01)]
            run.add("DIST", a, f"À {best:.3f} m{what} de « {eb['name']} » ({eb['ifc_class']}) : moins de "
                    f"{distance_m} m", actual=round(best, 4), expected=f"≥ {distance_m} m",
                    measure={"value": round(best, 4), "unit": "m"},
                    threshold={"value": distance_m, "unit": "m", "source": "distance_m (mode min)"},
                    elements=[{"uuid": a, "role": "A"}, {"uuid": who, "role": "B"}], bbox=bbox)
        elif mode == "max" and (who is None or best > distance_m + 1e-9):
            msg = (f"Aucun élément B à moins de {reach:.0f} m{what}" if who is None else
                   f"Élément B le plus proche à {best:.2f} m{what} (« {idx.elements[who]['name']} ») : plus de "
                   f"{distance_m} m")
            run.add("DIST", a, msg, actual=None if who is None else round(best, 4), expected=f"≤ {distance_m} m",
                    measure={"value": None if who is None else round(best, 4), "unit": "m"},
                    threshold={"value": distance_m, "unit": "m", "source": "distance_m (mode max)"},
                    elements=[{"uuid": a, "role": "A"}] + ([{"uuid": who, "role": "B"}] if who else []))
    return run.result(), run.notes


# ── Zones de dégagement ─────────────────────────────────────────────────


def _frame(idx: GeometryIndex, g: str):
    """Repère horizontal de l'élément (axes de placement, z vertical) et étendue de son maillage dans ce repère."""
    e = idx.elements[g]
    ax = e.get("axes")
    if ax:
        x = np.array(ax["x"], float)
        x[2] = 0.0
    else:
        x = np.array(e["obb"]["axes"][0], float)
    if np.linalg.norm(x) < 1e-6:
        x = np.array(e["obb"]["axes"][0], float)
    x /= np.linalg.norm(x)
    z = np.array([0.0, 0.0, 1.0])
    y = np.cross(z, x)
    v, _ = idx.mesh(g)
    loc = v @ np.vstack([x, y, z]).T
    return np.vstack([x, y, z]), loc.min(axis=0), loc.max(axis=0)


def _zone(axes: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> dict:
    """Boîte orientée de bornes locales [lo, hi] dans le repère ``axes``."""
    c_loc = (lo + hi) / 2
    return {"center": [float(x) for x in c_loc @ axes], "axes": axes.tolist(),
            "half": [float(x) for x in (hi - lo) / 2]}


def door_zones(idx: GeometryIndex, g: str, *, front_m: float, back_m: float, side_m: float, height_m: float,
               width_m: float | None, use_opening_side: bool, push_m: float | None = None,
               pull_m: float | None = None, host: str | None = None) -> list[tuple[str, dict]]:
    """Zones d'une cible (porte, fenêtre, équipement) dans son repère local : +Y = côté d'ouverture (convention
    du placement IFC). Côté inconnu (OperationType non battant ou absent) : les deux côtés. Les zones partent des
    faces du mur hôte (une porte est souvent plus mince que son mur)."""
    axes, lo, hi = _frame(idx, g)
    lo, hi = lo.copy(), hi.copy()
    he = idx.elements.get(host) if host else None
    if he and he.get("mesh_idx") is not None:
        hv, _ = idx.mesh(host)
        hl = hv @ axes.T
        near = (hl[:, 0] >= lo[0] - 0.5) & (hl[:, 0] <= hi[0] + 0.5)  # au droit de la porte seulement
        if near.any() and (hl[near, 1].max() - hl[near, 1].min()) < 1.0:
            lo[1], hi[1] = min(lo[1], hl[near, 1].min()), max(hi[1], hl[near, 1].max())
    if width_m:
        cx = (lo[0] + hi[0]) / 2
        x0, x1 = cx - width_m / 2 - side_m, cx + width_m / 2 + side_m
    else:
        x0, x1 = lo[0] - side_m, hi[0] + side_m
    z0, z1 = lo[2], lo[2] + height_m
    op = (idx.ifc.get("doors") or {}).get(g) or {}
    swing = any(str(v or "").upper().find(s) >= 0 for v in (op.get("occurrence"), op.get("type")) for s in SWING)
    known = use_opening_side and swing
    out = []
    f_open = pull_m if pull_m is not None else front_m  # côté où l'on tire la porte = côté d'ouverture
    f_other = push_m if push_m is not None else (back_m if known else front_m)
    if f_open and f_open > 0:
        out.append(("côté ouverture" if known else "côté +Y", _zone(axes, np.array([x0, hi[1], z0]),
                                                                   np.array([x1, hi[1] + f_open, z1]))))
    if f_other and f_other > 0:
        out.append(("côté opposé" if known else "côté −Y", _zone(axes, np.array([x0, lo[1] - f_other, z0]),
                                                                np.array([x1, lo[1], z1]))))
    return out


def equipment_zone(idx: GeometryIndex, g: str, *, depth_m: float, width_m: float, height_m: float) -> tuple[str, dict]:
    """Zone d'usage devant un équipement : côté opposé au mur le plus proche (± X ou ± Y local)."""
    axes, lo, hi = _frame(idx, g)
    c = (lo + hi) / 2
    best = None
    for k, sgn in ((1, 1), (1, -1), (0, 1), (0, -1)):  # face arrière candidate
        p = c.copy()
        p[k] = hi[k] if sgn > 0 else lo[k]
        world = p @ axes
        box = [*(world - 0.3), *(world + 0.3)]
        walls = [o for o in idx.query_box(box) if o != g and _is(idx.elements[o]["ifc_class"], ("IfcWall",))]
        d = min((float(M.point_obb_distance(world[None, :], idx.elements[o]["obb"])[0]) for o in walls),
                default=math.inf)
        if best is None or d < best[0]:
            best = (d, k, sgn)
    _, k, sgn = best
    other = 1 - k
    lo_z, hi_z = lo.copy(), hi.copy()
    co = (lo[other] + hi[other]) / 2
    lo_z[other], hi_z[other] = co - width_m / 2, co + width_m / 2
    if sgn > 0:  # arrière en +k : la zone est en −k
        hi_z[k], lo_z[k] = lo[k], lo[k] - depth_m
    else:
        lo_z[k], hi_z[k] = hi[k], hi[k] + depth_m
    lo_z[2], hi_z[2] = lo[2], lo[2] + height_m
    return f"devant ({'±XY'[k]}{'−' if sgn > 0 else '+'})", _zone(axes, lo_z, hi_z)


def _obstacles_in(idx: GeometryIndex, zone: dict, target: str, *, obstacles: set[str] | None, tol: float,
                  hosts: dict[str, str]) -> list[tuple[str, float, list[float] | None]]:
    e = idx.elements[target]
    skip = {target, hosts.get(target), e.get("parent")}
    zone_floor = float(Z.obb_aabb(zone)[2])
    out = []
    for o in idx.query_box(Z.obb_aabb(zone)):
        eo = idx.elements.get(o)
        if o in skip or not eo or not eo["has_geometry"] or _is(eo["ifc_class"], STRUCT_OBSTACLE_SKIP):
            continue
        if obstacles is not None and o not in obstacles:
            continue
        if eo.get("parent") == target or (eo.get("parent") and eo["parent"] in skip):
            continue  # parties de la cible ou de son mur hôte
        if eo.get("parent") and (idx.elements.get(eo["parent"]) or {}).get("from_parts"):
            continue  # couche d'un mur en parties : c'est le mur (géométrie réunie) qui est signalé
        if _is(eo["ifc_class"], ("IfcSlab", "IfcCovering")):
            top, thick = eo["aabb"][5], eo["aabb"][5] - eo["aabb"][2]
            if top <= zone_floor + FLOOR_STEP_M or (thick <= THIN_FLOOR_M + 1e-6 and top <= zone_floor + THIN_FLOOR_M):
                continue  # sol (mince ou sous le seuil) : pas un obstacle, voir A7
        depth, bbox = Z.intrusion(idx, o, zone)
        if depth > tol:
            out.append((o, depth, bbox))
    return out


def run_clearance_rules(idx: GeometryIndex, records: dict[str, Record], targets: list[str], *,
                        obstacles: set[str] | None, front_m: float, back_m: float, side_m: float, height_m: float,
                        width_m: float | None, use_opening_side: bool, tolerance_m: float = 0.05,
                        rule_id: str = "DEG", run: _Run | None = None, source: str = "paramètres",
                        push_m: float | None = None, pull_m: float | None = None) -> tuple[CheckResult, list[str]]:
    own = run is None
    run = run or _new_run([rule_id], records, idx)
    from ..geometry.clash import _hosts

    hosts = _hosts(idx)
    for g in targets:
        e = idx.elements.get(g)
        if not e or not e["has_geometry"]:
            continue
        run.seen(rule_id, g)
        w = width_m
        if w is None and records.get(g) is not None:  # largeur de passage nominale, pas l'encombrement du cadre
            w = _num(resolve_field(records[g], "OverallWidth").value) or None
        zones_ = door_zones(idx, g, front_m=front_m, back_m=back_m, side_m=side_m, height_m=height_m,
                            width_m=w, use_opening_side=use_opening_side, push_m=push_m, pull_m=pull_m,
                            host=hosts.get(g))
        for side, zone in zones_:
            h = zone["half"]
            dims = f"{2 * h[0]:.2f} × {2 * h[1]:.2f} × {2 * h[2]:.2f} m"
            found = sorted(_obstacles_in(idx, zone, g, obstacles=obstacles, tol=tolerance_m, hosts=hosts),
                           key=lambda t: -t[1])
            if not found:
                continue
            # une ligne par cible et par côté, avec la liste des obstacles (la plus forte emprise d'abord)
            depth = found[0][1]
            names = [f"« {idx.elements[o]['name']} » ({idx.elements[o]['ifc_class']}, {d:.2f} m)" for o, d, _ in found]
            more = f" et {len(names) - 5} autre(s)" if len(names) > 5 else ""
            boxes = [b for _, _, b in found if b]
            bbox = ([min(b[i] for b in boxes) for i in range(3)] + [max(b[i] for b in boxes) for i in range(3, 6)]
                    if boxes else None)
            run.add(rule_id, g, f"Zone {side} ({dims}) encombrée par {len(found)} obstacle(s) : "
                    + ", ".join(names[:5]) + more + f" — empiètement maximal {depth:.3f} m", actual=round(depth, 4),
                    expected=f"zone libre (tolérance {tolerance_m} m)",
                    measure={"value": round(depth, 4), "unit": "m", "zone": side, "zone_dims_m": dims,
                             "obstacles": len(found)},
                    threshold={"value": tolerance_m, "unit": "m", "source": source}, bbox=bbox,
                    elements=[{"uuid": g, "role": "cible"}] + [{"uuid": o, "role": "obstacle"} for o, _, _ in found])
    if own:
        return run.result(), run.notes
    return None, []  # type: ignore[return-value]


# ── Accessibilité PMR ───────────────────────────────────────────────────


def _turning_space(idx: GeometryIndex, space: str, *, diameter: float, height: float, step: float) -> float:
    """Plus grand diamètre libre trouvé (≤ ``diameter``) dans le local : 0 si aucun."""
    e = idx.elements[space]
    v, f = idx.mesh(space)
    b = e["aabb"]
    floor = b[2]
    band = (floor + 0.02, floor + height)
    obs = []
    for o in idx.query_box(b):
        eo = idx.elements.get(o)
        if o == space or not eo or not eo.get("obb") or _is(eo["ifc_class"], STRUCT_OBSTACLE_SKIP + ("IfcSlab",)):
            continue
        if eo["aabb"][5] < band[0] or eo["aabb"][2] > band[1]:
            continue
        if _is(eo["ifc_class"], ("IfcCovering",)) and eo["aabb"][5] <= band[0] + 0.03:
            continue  # revêtement de sol
        obs.append(eo["obb"])
    xs = np.arange(b[0] + step / 2, b[3], step)
    ys = np.arange(b[1] + step / 2, b[4], step)
    centers = np.array([[x, y] for x in xs for y in ys])
    if len(centers) == 0:
        return 0.0
    if len(centers) > 4000:
        centers = centers[np.linspace(0, len(centers) - 1, 4000).astype(int)]
    # Distance de chaque centre à l'obstacle le plus proche (emprises orientées).
    dmin = np.full(len(centers), np.inf)
    for o in obs:
        c, ax, h = np.asarray(o["center"]), np.asarray(o["axes"]), np.asarray(o["half"])
        rel = np.c_[centers - c[:2], np.zeros(len(centers))] @ ax.T
        d = np.linalg.norm(np.maximum(np.abs(rel[:, :2]) - h[:2], 0), axis=1)
        dmin = np.minimum(dmin, d)
    ang = np.linspace(0, 2 * np.pi, 16, endpoint=False)
    closed = e.get("is_closed")
    for r in np.arange(diameter / 2, 0.3, -0.05):
        ok = dmin >= r - 1e-6
        if not ok.any():
            continue
        cand = centers[ok]
        ring = (cand[:, None, :] + r * np.stack([np.cos(ang), np.sin(ang)], axis=1)[None]).reshape(-1, 2)
        pts = np.c_[np.vstack([cand, ring]), np.full(len(cand) * 17, floor + 0.05)]
        if closed:
            inside = M.points_inside(pts, v, f)
        else:
            inside = np.all((pts[:, :2] >= [b[0], b[1]]) & (pts[:, :2] <= [b[3], b[4]]), axis=1)
        inside_c = inside[:len(cand)] & inside[len(cand):].reshape(len(cand), 16).all(axis=1)
        if inside_c.any():
            return float(2 * r)
    return 0.0


def run_accessibility_rules(idx: GeometryIndex | None, records: dict[str, Record], *, referentiel: str,
                            thresholds: dict | None, doors: list[str], spaces: list[str],
                            checks: list[str] | None = None,
                            scoped: bool | None = None) -> tuple[CheckResult, list[str], dict]:
    """``scoped`` : un périmètre a été désigné (scope) — A6 contrôle alors chaque porte et local du périmètre ; sinon
    une seule ligne de synthèse (défaut : périmètre désigné si des locaux sont fournis)."""
    if scoped is None:
        scoped = bool(spaces)
    if referentiel not in REFERENTIELS and referentiel != "personnalisé":
        raise ValueError(f"Référentiel « {referentiel} » inconnu : {', '.join(REFERENTIELS)} ou personnalisé.")
    th = {**REFERENTIELS.get(referentiel, REFERENTIELS["BHC 2015"]), **PMR_DEFAULTS, **(thresholds or {})}
    ids = [r for r in ("A1", "A2", "A3", "A4", "A5", "A6", "A7") if not checks or r in {c.upper() for c in checks}]
    run = _new_run(ids, records, idx)
    src = f"référentiel {referentiel} (valeurs indicatives, à valider)"
    run.notes.append(f"Seuils {src} : " + ", ".join(f"{k} = {th[k]}" for k in REFERENTIELS["BHC 2015"]) + ".")
    by = {g: records.get(g) for g in set(doors) | set(spaces)}

    if "A1" in ids:
        for g in doors:
            rec = by.get(g)
            if rec is None:
                continue
            w = _num(resolve_field(rec, "OverallWidth").value)
            run.seen("A1", g)
            if w is None:
                run.add("A1", g, "Largeur nominale (OverallWidth) absente", status="absent",
                        expected="OverallWidth renseignée")
                continue
            entry = as_bool(resolve_field(rec, "*.IsExternal").value) is True
            need = th["door_width_entry_m"] if entry else th["door_width_interior_m"]
            if w < need - 1e-6:
                run.add("A1", g, f"Largeur {w:.2f} m < {need:.2f} m ({'porte d’entrée' if entry else 'porte intérieure'})",
                        status="sous_seuil", actual=round(w, 3), expected=f"≥ {need} m",
                        measure={"value": round(w, 3), "unit": "m"},
                        threshold={"value": need, "unit": "m",
                                   "source": f"{src} — {'door_width_entry_m' if entry else 'door_width_interior_m'}"})

    if "A2" in ids:
        if not (th["a2_width_m"] and (th["a2_push_m"] or th["a2_pull_m"])):
            run.notes.append("A2 non contrôlée : dimensions à paramétrer (a2_width_m, a2_push_m, a2_pull_m).")
        elif idx is None:
            run.notes.append("A2 non contrôlée : index géométrique indisponible.")
        else:
            run_clearance_rules(idx, records, doors, obstacles=None, front_m=0, back_m=0, side_m=0, height_m=2.0,
                                width_m=th["a2_width_m"], use_opening_side=True,
                                tolerance_m=th["clearance_tolerance_m"], rule_id="A2", run=run,
                                source=f"{src} — a2_*", push_m=th["a2_push_m"], pull_m=th["a2_pull_m"])

    designated = spaces  # locaux désignés par le projet (scope) : jamais déduits des noms
    if "A3" in ids:
        if not designated:
            run.notes.append("A3 non contrôlée : aucun local désigné par le projet (scope).")
        elif idx is None:
            run.notes.append("A3 non contrôlée : index géométrique indisponible.")
        else:
            dia = th["turning_diameter_m"]
            for g in designated:
                e = idx.elements.get(g)
                if not e or not e["has_geometry"]:
                    continue
                run.seen("A3", g)
                got = _turning_space(idx, g, diameter=dia, height=th["turning_height_m"], step=th["turning_step_m"])
                if got < dia - 1e-6:
                    run.add("A3", g, f"Pas de cercle libre de {dia:.2f} m : plus grand cercle trouvé "
                            f"{got:.2f} m" if got else f"Pas de cercle libre de {dia:.2f} m (aucun cercle ≥ 0,60 m)",
                            status="sous_seuil", actual=round(got, 2), expected=f"cercle libre ⌀ {dia} m",
                            measure={"value": round(got, 2), "unit": "m (diamètre)"},
                            threshold={"value": dia, "unit": "m", "source": f"{src} — turning_diameter_m"})
            run.notes.append(f"A3 : obstacles considérés jusqu'à {th['turning_height_m']} m au-dessus du sol "
                             "(turning_height_m) ; emprises orientées des obstacles.")

    if "A4" in ids:
        if not (th["a4_width_m"] and th["a4_depth_m"]):
            run.notes.append("A4 non contrôlée : dimensions à paramétrer (a4_width_m, a4_depth_m).")
        elif idx is None:
            run.notes.append("A4 non contrôlée : index géométrique indisponible.")
        else:
            from ..geometry.clash import _hosts

            hosts = _hosts(idx)
            san = [g for g, e in idx.elements.items() if e["has_geometry"] and (
                _is(e["ifc_class"], ("IfcSanitaryTerminal",)) or (
                    _is(e["ifc_class"], ("IfcFlowTerminal", "IfcBuildingElementProxy", "IfcFurnishingElement"))
                    and SANITARY_RE.search(f"{e['name']} {e.get('type_name') or ''}")))]
            for g in san:
                run.seen("A4", g)
                side, zone = equipment_zone(idx, g, depth_m=th["a4_depth_m"], width_m=th["a4_width_m"], height_m=1.0)
                for o, depth, bbox in _obstacles_in(idx, zone, g, obstacles=None, tol=th["clearance_tolerance_m"],
                                                    hosts=hosts):
                    eo = idx.elements[o]
                    run.add("A4", g, f"Espace d'usage {side} encombré par « {eo['name']} » : empiètement {depth:.3f} m",
                            actual=round(depth, 4), expected="espace d'usage libre",
                            measure={"value": round(depth, 4), "unit": "m"},
                            threshold={"value": th["clearance_tolerance_m"], "unit": "m", "source": f"{src} — a4_*"},
                            bbox=bbox, elements=[{"uuid": g, "role": "équipement"}, {"uuid": o, "role": "obstacle"}])
            if not san:
                run.notes.append("A4 : aucun WC ni lavabo reconnu (IfcSanitaryTerminal ou nom WC / lavabo…).")

    if "A5" in ids:
        from .geometry_rules import RISER_FIELDS, stair_values

        for rec in (r for r in records.values() if r.ifc_class in ("IfcStair", "IfcStairFlight")):
            parent = (idx.elements.get(rec.uuid) or {}).get("parent") if idx else None
            prec = records.get(parent) if parent else None
            if prec is not None and _first_number(prec, RISER_FIELDS)[0] is not None:
                continue  # volée ou sous-escalier : valeurs de l'escalier parent
            riser, rs, tread, ts = stair_values(rec, idx)
            if riser is None and tread is None:
                continue
            if "maillage" in rs + ts:  # estimation de repli : pas de verdict PMR dessus
                run.notes.append(f"A5 : « {rec.name} » sans cotes lisibles — hauteur et giron estimés sur le maillage "
                                 "seulement (voir G7), non contrôlés.")
                continue
            run.seen("A5", rec.uuid)
            if riser is not None and riser > th["riser_max_m"] + 1e-6:
                run.add("A5", rec.uuid, f"Hauteur de marche {riser:.3f} m > {th['riser_max_m']} m", status="sous_seuil",
                        actual=round(riser, 4), expected=f"≤ {th['riser_max_m']} m ({rs})",
                        measure={"value": round(riser, 4), "unit": "m"},
                        threshold={"value": th["riser_max_m"], "unit": "m", "source": f"{src} — riser_max_m"})
            if tread is not None and tread < th["tread_min_m"] - 1e-6:
                run.add("A5", rec.uuid, f"Giron {tread:.3f} m < {th['tread_min_m']} m", status="sous_seuil",
                        actual=round(tread, 4), expected=f"≥ {th['tread_min_m']} m ({ts})",
                        measure={"value": round(tread, 4), "unit": "m"},
                        threshold={"value": th["tread_min_m"], "unit": "m", "source": f"{src} — tread_min_m"})

    if "A6" in ids and not scoped:  # sans périmètre désigné : une ligne de synthèse, pas une par élément
        pool = [r for r in records.values() if r.ifc_class.startswith("IfcDoor") or r.ifc_class == "IfcSpace"]
        found = [r for r in pool if resolve_field(r, "*.HandicapAccessible").found]
        run.seen("A6", None)
        if not found:
            n_d = sum(1 for r in pool if r.ifc_class.startswith("IfcDoor"))
            run.add("A6", "", f"HandicapAccessible absente de toute la maquette ({n_d} portes, {len(pool) - n_d} "
                    "locaux) — à renseigner au moins sur le périmètre accessible ; contrôle élément par élément avec "
                    "scope", status="absent", ifc_class="(maquette)", name="HandicapAccessible",
                    expected="Pset_DoorCommon / Pset_SpaceCommon.HandicapAccessible")
        else:
            run.notes.append(f"A6 : HandicapAccessible renseignée sur {len(found)} porte(s) ou local(aux) sur "
                             f"{len(pool)} ; contrôle élément par élément sur le périmètre désigné (scope).")
    if "A6" in ids and scoped:
        for g in list(doors) + list(designated):
            rec = by.get(g)
            if rec is None:
                continue
            run.seen("A6", g)
            fv = resolve_field(rec, "*.HandicapAccessible")
            if not fv.found:
                run.add("A6", g, "HandicapAccessible non renseignée", status="absent",
                        expected="Pset_DoorCommon / Pset_SpaceCommon.HandicapAccessible")
            elif as_bool(fv.value) is False:
                run.add("A6", g, "Déclaré non accessible (HandicapAccessible = FALSE)", status="incoherent",
                        severity="info", actual=fv.value, expected="TRUE pour un élément désigné accessible")
    if "A7" in ids:
        if idx is None:
            run.notes.append("A7 non contrôlée : index géométrique indisponible.")
        else:
            _threshold_steps(run, idx, doors, th["threshold_step_max_m"], src)
    return run.result(), run.notes, {"seuils": dict(th), "locaux_designes": len(designated)}


def door_side_points(idx: GeometryIndex, g: str, *, dist: float = 0.3,
                     dz: float = 0.3) -> tuple[dict[str, np.ndarray], float]:
    """Points à ``dist`` des faces du mur hôte, côté +Y et côté −Y de la porte, à ``dz`` au-dessus du seuil."""
    from ..geometry.clash import _hosts

    axes, lo, hi = _frame(idx, g)
    host = _hosts(idx).get(g)
    if host and idx.mesh(host) is not None:
        hl = idx.mesh(host)[0] @ axes.T
        near = (hl[:, 0] >= lo[0] - 0.5) & (hl[:, 0] <= hi[0] + 0.5)
        if near.any() and (hl[near, 1].max() - hl[near, 1].min()) < 1.0:
            lo, hi = lo.copy(), hi.copy()
            lo[1], hi[1] = min(lo[1], hl[near, 1].min()), max(hi[1], hl[near, 1].max())
    c = (lo + hi) / 2
    return {"+Y": np.array([c[0], hi[1] + dist, lo[2] + dz]) @ axes,
            "−Y": np.array([c[0], lo[1] - dist, lo[2] + dz]) @ axes}, lo[2]


def _threshold_steps(run: _Run, idx: GeometryIndex, doors: list[str], tol: float, src: str) -> None:
    """A7 : dessus du sol de chaque côté (rayon descendant sur dalles et revêtements) contre le seuil de la porte."""
    down = np.array([0.0, 0.0, -1.0])
    for g in doors:
        e = idx.elements.get(g)
        if not e or not e["has_geometry"]:
            continue
        pts, base = door_side_points(idx, g, dz=0.5)
        steps = {}
        for side, p in pts.items():
            top = None
            for o in idx.query_box([p[0] - 0.01, p[1] - 0.01, base - 0.5, p[0] + 0.01, p[1] + 0.01, p[2]]):
                eo = idx.elements.get(o)
                if not eo or not _is(eo["ifc_class"], ("IfcSlab", "IfcCovering")) or idx.mesh(o) is None:
                    continue
                v, f = idx.mesh(o)
                _, t = M.ray_hits(p[None, :], down, v, f)
                if np.isfinite(t[0]) and (top is None or p[2] - t[0] > top):
                    top = float(p[2] - t[0])
            if top is not None and top >= base - 0.5:
                steps[side] = top - base
        run.seen("A7", g)
        for side, st in steps.items():
            if abs(st) > tol + 1e-6:
                run.add("A7", g, f"Ressaut de {abs(st) * 100:.0f} cm côté {side} : sol {'au-dessus' if st > 0 else 'en dessous'}"
                        " du seuil (à vérifier : peut venir de la modélisation des sols)", status="incoherent",
                        actual=round(st, 3), expected=f"≤ {tol * 100:.0f} cm",
                        measure={"value": round(st, 3), "unit": "m", "cote": side},
                        threshold={"value": tol, "unit": "m", "source": f"{src} — threshold_step_max_m"})


def door_sides(idx: GeometryIndex, records: dict[str, Record], g: str, loc) -> dict[str, tuple[str | None, str]]:
    """Local de chaque côté d'une porte : {'+Y': (guid, libellé), '−Y': …} (guid None = extérieur / aucun local)."""
    pts, _ = door_side_points(idx, g)
    out = {}
    for side, p in pts.items():
        sp = loc.at(p)
        rec = records.get(sp) if sp else None
        out[side] = (sp, str((rec.attrs.get("LongName") if rec else None) or (idx.elements[sp]["name"] if sp else "")))
    return out


def is_balcony_door(idx, records, g, loc) -> bool:
    return any(BALCONY_RE.search(lbl) for _, lbl in door_sides(idx, records, g, loc).values())


#: Niveaux d'accès par défaut : étages dont le nom — ou une des parties d'un nom d'étage combiné, séparées par « & »
#: (« R+2_C & RDC_E/F & TOITURE D ») — commence par RDC, Rez ou Ground (RDC_A/B, Rez-de-chaussée, Ground Floor…).
#: Les bâtiments en escalier en ont plusieurs.
ACCESS_STOREY_RE = re.compile(r"(?:^|&)\s*(RDC|REZ|GROUND)", re.IGNORECASE)


def access_levels_of(idx: GeometryIndex, access_storeys: str | list[str] | None = None) -> list[dict]:
    return access_levels(idx.ifc.get("storeys") or [], access_storeys)[0]


def access_levels(storeys: list[dict], access_storeys: str | list[str] | None = None) -> tuple[list[dict], str]:
    """Niveaux d'accès retenus et leur origine : désignés, par nom (RDC / Rez / Ground), sinon l'étage d'altitude la
    plus proche de 0."""
    if access_storeys:
        names = [access_storeys] if isinstance(access_storeys, str) else list(access_storeys)
        out = []
        for n in names:
            acc = next((s for s in storeys if s["name"].strip().lower() == str(n).strip().lower()), None)
            if acc is None:
                raise ValueError(f"Niveau d'accès « {n} » introuvable ({', '.join(s['name'] for s in storeys)}).")
            if acc not in out:
                out.append(acc)
        return out, "désignés"
    by_name = [s for s in storeys if ACCESS_STOREY_RE.search(s.get("name") or "")]
    if by_name:
        return sorted(by_name, key=lambda s: s.get("z") or 0), "nom commençant par RDC, Rez ou Ground"
    acc = min((s for s in storeys if s.get("elevation") is not None), key=lambda s: abs(s["elevation"]), default=None)
    return ([acc] if acc else []), "altitude la plus proche de 0"


#: Largeur en deçà de laquelle une porte n'est pas un dégagement (porte de placard, trappe technique).
EXIT_MIN_WIDTH_M = 0.60


def _open_stairs_to_access(idx: GeometryIndex, opens: list[str], stair_spaces: set[str], access: set[str]) -> list[str]:
    """Escaliers ouverts dont la cage descend (ou monte) jusqu'à un niveau d'accès : un local d'escalier superposé
    en plan à chaque étage entre les deux."""
    order = [s["guid"] for s in sorted(idx.ifc.get("storeys") or [], key=lambda s: s.get("z") or 0)]
    pos = {g: k for k, g in enumerate(order)}
    fps = {g: Z.footprint(idx, g) for g in stair_spaces | set(opens)}
    by_storey: dict[str | None, list[str]] = defaultdict(list)
    for g in fps:
        by_storey[idx.elements[g].get("storey")].append(g)
    out = []
    for s in opens:
        k = pos.get(idx.elements[s].get("storey"))
        if k is None or fps.get(s) is None:
            continue
        if order[k] in access:
            out.append(s)
            continue
        for step in (-1, 1):
            j, ok = k + step, False
            while 0 <= j < len(order):
                above = [g for g in by_storey.get(order[j], []) if fps.get(g) is not None
                         and Z.footprints_overlap(fps[s], fps[g])]
                if not above:
                    break
                if order[j] in access:
                    ok = True
                    break
                j += step
            if ok:
                out.append(s)
                break
    return sorted(out)


def auto_exits(idx: GeometryIndex, records: dict[str, Record], *, access_storeys: str | list[str] | None = None,
               stair_door_distance_m: float | None = None) -> tuple[list[str], dict]:
    """Sorties : portes extérieures des niveaux d'accès + portes d'accès aux escaliers de chaque étage.

    Niveaux d'accès : ceux désignés (``access_storeys``), sinon les étages dont le nom commence par RDC, Rez ou
    Ground (plusieurs pour un bâtiment en escalier), sinon l'étage d'altitude la plus proche de 0. Ils sont listés
    dans le résultat pour être confirmés.

    Porte d'accès à un escalier, par la géométrie : porte qui donne sur un local contenant un IfcStair /
    IfcStairFlight. Critère de distance (porte à moins de ``stair_door_distance_m`` en plan d'un escalier qui n'est
    dans aucun local) **désactivé par défaut** : mesuré, il ajoutait 79 portes de gaines, WC et paliers sur la
    maquette test (3 m) et 185 portes d'escaliers privés de duplex sur une autre (1,5 m). Le nom du local (STAIR,
    ESCALIER, CAGE) n'est qu'un indice de plus. Une porte de balcon n'est jamais une sortie ; un étage sans sortie
    détectée est listé (« etages_sans_sortie ») pour être déclaré non contrôlé.
    """
    loc = Z.SpaceLocator(idx)
    storeys = idx.ifc.get("storeys") or []
    accs, acc_src = access_levels(storeys, access_storeys)
    acc_guids = {s["guid"] for s in accs}
    stairs = [g for g, e in idx.elements.items() if e["ifc_class"] in ("IfcStair", "IfcStairFlight")
              and e["has_geometry"] and not (e.get("parent") and (idx.elements.get(e["parent"]) or {}).get("from_parts"))]
    stair_space = {g: loc.of_element(g) for g in stairs}
    spaces_with_stair = {sp for sp in stair_space.values() if sp}
    loose = [g for g in stairs if not stair_space[g]]  # escaliers hors de tout local : critère de distance
    stair_fp = {g: Z.footprint(idx, g) for g in loose}
    named = {g for g, e in idx.elements.items() if e["ifc_class"] == "IfcSpace" and records.get(g)
             and STAIR_SPACE_RE.search(str(records[g].attrs.get("LongName") or ""))}
    stair_spaces = spaces_with_stair | named
    exits: list[str] = []
    info = {"niveaux_acces": [s["name"] for s in accs], "origine_niveaux_acces": acc_src,
            "exterieures_par_niveau": Counter(), "exterieures": 0, "escaliers": 0, "balcons_exclus": 0,
            "gaines_exclues": 0, "etroites_exclues": 0, "par_critere": Counter()}
    shaft = re.compile(SHAFT_PATTERN, re.IGNORECASE)
    served: set[str] = set()  # locaux desservis par au moins une porte
    for g, e in idx.elements.items():
        if not e["ifc_class"].startswith("IfcDoor") or not e["has_geometry"]:
            continue
        w = _num(resolve_field(records[g], "OverallWidth").value) if records.get(g) else None
        if w is not None and w < EXIT_MIN_WIDTH_M - 1e-6:  # porte de placard ou de gaine : pas un dégagement
            info["etroites_exclues"] += 1
            continue
        sides = door_sides(idx, records, g, loc)
        if any(BALCONY_RE.search(lbl) for _, lbl in sides.values()):
            info["balcons_exclus"] += 1
            continue
        side_sp = [sp for sp, _ in sides.values()]
        served.update(sp for sp in side_sp if sp)
        if side_sp and all(sp in stair_spaces for sp in side_sp if sp) and all(side_sp):
            continue  # porte intérieure à la cage d'escalier
        crit = "local contenant un escalier" if any(sp in spaces_with_stair for sp in side_sp) else None
        if crit is None and loose and stair_door_distance_m:
            z = e["aabb"][2]
            fp = Z.footprint(idx, g)
            for st in loose:
                sb = idx.elements[st]["aabb"]
                if sb[2] - 0.5 <= z <= sb[5] + 0.5 and Z.horizontal_distance(fp, stair_fp[st]) <= stair_door_distance_m:
                    crit = "proche d'un escalier hors local"
                    break
        if crit is None and any(sp in named for sp in side_sp):
            crit = "nom du local"
        if crit and any(sp not in stair_spaces and shaft.search(lbl) for sp, lbl in sides.values() if sp):
            info["gaines_exclues"] += 1  # trappe de gaine technique ouvrant sur la cage : pas une sortie
            continue
        if crit:
            exits.append(g)
            info["escaliers"] += 1
            info["par_critere"][crit] += 1
            continue
        ext = as_bool(resolve_field(records[g], "*.IsExternal").value) if records.get(g) else None
        if e.get("storey") in acc_guids and (ext or any(sp is None for sp in side_sp)):
            exits.append(g)
            info["exterieures"] += 1
            info["exterieures_par_niveau"][next(s["name"] for s in accs if s["guid"] == e["storey"])] += 1
    with_exit = {idx.elements[x].get("storey") for x in exits}
    with_space = {e.get("storey") for e in idx.elements.values() if e["ifc_class"] == "IfcSpace"}
    ordered = sorted(storeys, key=lambda s: s.get("z") or 0)
    # sans sortie mais avec des locaux : non contrôlé ; sans aucun local : sans objet (toiture, vide technique…)
    info["etages_sans_sortie"] = [s["name"] for s in ordered if s["guid"] not in with_exit and s["guid"] in with_space]
    info["etages_sans_objet"] = [s["name"] for s in ordered if s["guid"] not in with_exit and s["guid"] not in with_space]
    info["par_critere"] = dict(info["par_critere"])
    # escaliers ouverts : local contenant un escalier, sans aucune porte (ouvert sur les circulations)
    info["escaliers_ouverts"] = sorted(sp for sp in spaces_with_stair if sp not in served)
    # comptés par défaut (décision du 18/11) : escalier ouvert relié au niveau d'accès par une cage continue
    # (un local d'escalier superposé à chaque étage intermédiaire) — dégagement non protégé (30 m, CO 49)
    info["escaliers_ouverts_relies"] = _open_stairs_to_access(idx, info["escaliers_ouverts"], stair_spaces, acc_guids)
    info["exterieures_par_niveau"] = {s["name"]: info["exterieures_par_niveau"].get(s["name"], 0) for s in accs}
    return exits, info


# ── Incendie ────────────────────────────────────────────────────────────

FIRE_CLASSES = ("IfcDoor", "IfcWall", "IfcSlab", "IfcCurtainWall")
_RATING_RE = re.compile(r"(?P<letters>[REIWMCS]+)?\s*(?P<num>\d+(?:[.,]\d+)?)\s*(?P<unit>h|min|mn|')?", re.IGNORECASE)


def parse_rating(value) -> tuple[set[str], float | None]:
    """« REI 60 » → ({R, E, I}, 60) ; « 1h » → (∅, 60) ; « EI120 » → ({E, I}, 120)."""
    if value in (None, ""):
        return set(), None
    s = str(value).strip()
    m = _RATING_RE.search(s.replace(" ", " "))
    if not m:
        return set(), None
    letters = set((m.group("letters") or "").upper())
    n = float(m.group("num").replace(",", "."))
    if (m.group("unit") or "").lower() == "h":
        n *= 60
    return letters, n


def rating_ok(actual, required) -> bool:
    la, na = parse_rating(actual)
    lr, nr = parse_rating(required)
    if na is None:
        return False
    return (not lr or lr <= la or not la) and (nr is None or na >= nr - 1e-9)


def _fire_value(rec: Record | None):
    if rec is None:
        return None
    fv = resolve_field(rec, "*.FireRating")
    return fv.value if fv.found else None


def _required_for(cls: str, zone: str, required: dict) -> str | None:
    for key in (f"zone:{zone}",) if zone else ():
        sub = required.get(key)
        if isinstance(sub, dict):
            for c, v in sub.items():
                if _is(cls, (c,)):
                    return v
    for c, v in required.items():
        if not c.startswith("zone:") and _is(cls, (c,)):
            return v
    return None


def run_fire_rules(idx: GeometryIndex | None, records: dict[str, Record], *, required_ratings: dict | None,
                   compartments: str | None, compartment_rating: str | None, door_rating: str | None,
                   exits: list[str], stairs: list[str], max_distance_m: float | None, exit_up, stairs_up,
                   checks: list[str] | None = None, distance_method: str = "cheminement",
                   path_cell_m: float | None = None,
                   exit_spaces: list[str] | None = None, f3_referentiel: str | None = None,
                   f3_origin: str | None = None, protected_stairs: set[str] | str | None = None,
                   access_levels: set[str] | None = None, landing_doors: list[str] | None = None,
                   eas: list[str] | None = None, effectif=None, dead_end_max_m: float | None = None,
                   pmr_door_min_m: float = 0.90, step_max_m: float = 0.02) -> tuple[CheckResult, list[str], dict]:
    if distance_method not in ("cheminement", "vol_oiseau"):
        raise ValueError("distance_method : « cheminement » ou « vol_oiseau ».")
    ids = [r for r in ("F1", "F2", "F3", "F4", "F5", "F6") if not checks or r in {c.upper() for c in checks}]
    run = _new_run(ids, records, idx)
    missing: list[str] = []
    fire_recs = [r for r in records.values() if _is(r.ifc_class, FIRE_CLASSES)]
    n_rated = sum(1 for r in fire_recs if _fire_value(r) not in (None, ""))
    extra: dict = {"elements_avec_firerating": n_rated}

    if "F1" in ids:
        if n_rated == 0:
            missing.append("F1 : données absentes — aucun FireRating sur les portes, murs et dalles.")
            run.notes.append(missing[-1])
        else:
            req = required_ratings or {}
            for rec in fire_recs:
                val = _fire_value(rec)
                need = _required_for(rec.ifc_class, rec.zone, req) if req else None
                if not req:
                    run.seen("F1", rec.uuid)
                    if val in (None, ""):
                        run.add("F1", rec.uuid, "FireRating absent", status="absent", expected="FireRating renseigné")
                    continue
                if need is None:
                    continue
                run.seen("F1", rec.uuid)
                if val in (None, ""):
                    run.add("F1", rec.uuid, f"FireRating absent (exigé : {need})", status="absent", expected=need,
                            threshold={"value": need, "unit": "", "source": "required_ratings"})
                elif not rating_ok(val, need):
                    run.add("F1", rec.uuid, f"FireRating « {val} » insuffisant (exigé : {need})", status="sous_seuil",
                            actual=val, expected=need, threshold={"value": need, "unit": "", "source": "required_ratings"})

    if "F2" in ids:
        if idx is None:
            run.notes.append("F2 non contrôlée : index géométrique indisponible.")
        elif not compartments:
            run.notes.append("F2 non contrôlée : compartiments non désignés (compartments='zones' ou propriété).")
        else:
            boundaries = _compartment_boundaries(idx, records, compartments)
            extra["parois_separatives"] = len(boundaries)
            need_wall = compartment_rating or (required_ratings or {}).get("boundary")
            if n_rated == 0:
                missing.append(f"F2 : données absentes — {len(boundaries)} paroi(s) séparative(s) détectée(s) entre "
                               "compartiments, mais aucun FireRating à comparer.")
                run.notes.append(missing[-1])
            elif not need_wall:
                run.notes.append("F2 : degré exigé non fourni (compartment_rating) — parois listées sans comparaison.")
            if n_rated and need_wall:
                from ..geometry.clash import _hosts

                hosts = _hosts(idx)
                doors_of: dict[str, list[str]] = defaultdict(list)
                for d, h in hosts.items():
                    doors_of[h].append(d)
                for g, (za, zb) in boundaries.items():
                    run.seen("F2", g)
                    val = _fire_value(records.get(g))
                    if not rating_ok(val, need_wall):
                        run.add("F2", g, f"Paroi entre « {za} » et « {zb} » : FireRating « {val or 'absent'} » < {need_wall}",
                                status="absent" if val in (None, "") else "sous_seuil", actual=val, expected=need_wall,
                                threshold={"value": need_wall, "unit": "", "source": "compartment_rating"})
                    for d in doors_of.get(g, []):
                        need_d = door_rating or need_wall
                        run.seen("F2", d)
                        dv = _fire_value(records.get(d))
                        if not rating_ok(dv, need_d):
                            run.add("F2", d, f"Porte dans la paroi « {za} » / « {zb} » : FireRating « {dv or 'absent'} » "
                                    f"< {need_d}", status="absent" if dv in (None, "") else "sous_seuil", actual=dv,
                                    expected=need_d, threshold={"value": need_d, "unit": "", "source": "door_rating"},
                                    elements=[{"uuid": d, "role": "porte"}, {"uuid": g, "role": "paroi"}])

    if {"F3", "F5", "F6"} & set(ids):
        from .evacuation import F3_REFERENTIELS, run_evacuation

        ref = f3_referentiel or ("personnalise" if max_distance_m is not None else None)
        if not exits and not exit_spaces:
            run.notes.append("F3, F5, F6 non contrôlées : portes de sortie non désignées (exits).")
        elif idx is None:
            run.notes.append("F3, F5, F6 non contrôlées : index géométrique indisponible.")
        elif ref is None:
            run.notes.append("F3 non contrôlée : choisir le référentiel (f3_referentiel : "
                             + ", ".join(F3_REFERENTIELS) + ") ou une distance maximale (max_distance_m).")
        else:
            if access_levels is None:
                access_levels = {s_["guid"] for s_ in access_levels_of(idx)}
            extra["evacuation"] = run_evacuation(
                run, idx, records, ids=ids, exits=exits, exit_spaces=list(exit_spaces or ()), referentiel=ref,
                max_distance_m=max_distance_m, origin=f3_origin, protected=protected_stairs,
                access_levels=set(access_levels), landings=landing_doors, eas=eas, effectif=effectif,
                dead_end_max_m=dead_end_max_m, pmr_door_min_m=pmr_door_min_m, step_max_m=step_max_m,
                method=distance_method, cell_m=path_cell_m)
            ev = extra["evacuation"]
            if "F3" in ids:
                run.notes.append(
                    f"F3 ({ev['referentiel']}, départ : "
                    f"{'porte palière' if ev['origine'] == 'porte_paliere' else 'point le plus éloigné du local'}) : "
                    + ("distance suivant l'axe des circulations (ligne droite dans le local de départ, puis axe des "
                       f"circulations ; grille de {path_cell_m or 0.1:.2f} m, mobilier ignoré) ; distance tendue au ras "
                       "des murs, vol d'oiseau et orthogonale en complément"
                       if distance_method == "cheminement" else "distance à vol d'oiseau")
                    + ". Aide à la décision : la conformité se juge avec le bureau de contrôle et la commission de "
                      "sécurité.")
            if "F3" in ids and ref in ("ERP",) and protected_stairs is None:
                run.notes.append("F3 : nature des escaliers inconnue (protected_stairs) — entre 30 et 40 m, résultat "
                                 f"« non concluant » ({ev['non_concluants']} cas).")

    if "F4" in ids:
        if not exits and not stairs:
            run.notes.append("F4 non contrôlée : sorties et escaliers non désignés (exits, stairs).")
        for kind, items, up in (("sortie", exits, exit_up), ("escalier", stairs, stairs_up)):
            for g in items:
                rec = records.get(g)
                if rec is None:
                    continue
                n_up = (up.get(g) if isinstance(up, dict) else up) or 1
                need = 0.90 if n_up == 1 else (1.40 if n_up == 2 else 0.60 * n_up)
                if kind == "sortie":
                    w = _num(resolve_field(rec, "OverallWidth").value)
                else:
                    w, _ = _first_number(rec, ("*.Largeur", "*.Width", "*.Largeur de volée", "*.Actual Run Width"))
                run.seen("F4", g)
                if w is None:
                    run.add("F4", g, f"Largeur de l'{kind} illisible", status="absent", expected=f"≥ {need:.2f} m")
                elif w < need - 1e-6:
                    run.add("F4", g, f"Largeur {w:.2f} m < {need:.2f} m ({n_up} UP)", status="sous_seuil",
                            actual=round(w, 3), expected=f"≥ {need:.2f} m", measure={"value": round(w, 3), "unit": "m"},
                            threshold={"value": need, "unit": "m", "source": f"{n_up} UP (exit_up / stairs_up)"})
    extra["donnees_absentes"] = missing
    return run.result(), run.notes, extra


def _compartment_boundaries(idx: GeometryIndex, records: dict[str, Record], compartments: str) -> dict:
    """Murs et dalles dont les deux faces donnent sur des locaux de compartiments différents."""
    loc = Z.SpaceLocator(idx)

    def comp(space: str | None) -> str | None:
        if not space:
            return None
        rec = records.get(space)
        if rec is None:
            return None
        if compartments == "zones":
            return rec.zone or None
        fv = resolve_field(rec, compartments)
        return str(fv.value) if fv.found else None

    out = {}
    for g, e in idx.elements.items():
        if not e.get("obb") or not _is(e["ifc_class"], ("IfcWall", "IfcSlab")):
            continue
        o = e["obb"]
        c = np.asarray(o["center"])
        if _is(e["ifc_class"], ("IfcSlab",)):
            n, t = np.array([0.0, 0.0, 1.0]), o["half"][2]
        else:
            n, t = np.asarray(o["axes"][1]), o["half"][1]
        za = comp(loc.at(c + n * (t + 0.15)))
        zb = comp(loc.at(c - n * (t + 0.15)))
        if za and zb and za != zb:
            out[g] = (za, zb)
    return out


#: Nom d'un volume de surface (indice seulement : il doit aussi recouvrir d'autres locaux).
ZONE_NAME_RE = re.compile(r"^\s*(IPMS|SHAB|SDP|SHON|SHOB|SU\b|SURFACE|ZONE|AREA|GROSS)", re.IGNORECASE)


#: Écart entre deux planchers d'un même étage (étage combiné, bâtiment en escalier) au-delà duquel ils sont traités
#: séparément : chacun sa grille et ses sorties.
LEVEL_GAP_M = 0.60


def _level_groups(idx: GeometryIndex, spaces: list[str]) -> list[tuple[float, list[str]]]:
    """Locaux d'un étage regroupés par niveau de plancher (base), séparés dès que l'écart dépasse LEVEL_GAP_M."""
    order = sorted(spaces, key=lambda g: idx.elements[g]["aabb"][2])
    out: list[tuple[float, list[str]]] = []
    for g in order:
        z = idx.elements[g]["aabb"][2]
        if out and z - idx.elements[out[-1][1][-1]]["aabb"][2] <= LEVEL_GAP_M:
            out[-1][1].append(g)
        else:
            out.append((round(z, 2), [g]))
    return out


def _space_label(idx: GeometryIndex, records: dict[str, Record], g: str) -> str:
    rec = records.get(g)
    return f"« {(rec.attrs.get('LongName') if rec else None) or idx.elements[g]['name']} »"


def _doors_to_outside(idx: GeometryIndex, records: dict[str, Record], g: str, loc) -> list[str]:
    """Portes du local qui donnent sur l'extérieur (aucun local d'un côté)."""
    e = idx.elements[g]
    fp = Z.footprint(idx, g)
    out = []
    for h in idx.query_box(e["aabb"], tol=0.4):
        d = idx.elements.get(h)
        if not d or not d["ifc_class"].startswith("IfcDoor") or abs(d["aabb"][2] - e["aabb"][2]) > 0.5:
            continue
        if fp is None or Z.horizontal_distance(fp, Z.footprint(idx, h)) > 0.35:
            continue
        sides = door_sides(idx, records, h, loc)
        if any(not sp for sp, _ in sides.values()):
            out.append(f"« {d['name'].split(':')[0]} »")
    return out


