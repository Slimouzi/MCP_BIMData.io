"""Cheminement dans un étage : distance de marche vers les sorties, à travers les locaux et les portes.

Méthode (F3, version 2) :

- grille horizontale par étage et par niveau de plancher (maille ``cell`` m, 0,10 par défaut) ;
- surface praticable = empreinte des locaux de l'étage (projection des faces supérieures de leur maillage) + empreinte
  des portes de l'étage, élargie d'une maille pour franchir l'épaisseur du mur ;
- obstacles : murs, murs-rideaux, poteaux et fenêtres, coupés à 1 m au-dessus de chaque niveau de plancher des locaux
  de l'étage ; toute maille traversée par la coupe est bloquée, sauf les mailles de porte. Une cloison fine n'est donc
  jamais traversée hors d'une porte, et un passage libre (réservation sans porte, locaux jointifs sans mur) l'est :
  l'épaisseur de mur entre deux locaux (jusqu'à 0,60 m) est praticable là où la coupe ne la bloque pas ;
- on passe d'une maille praticable à sa voisine (8 voisins, sans couper les angles) ;
- coûts depuis les portes de sortie par Dijkstra (``scipy.sparse.csgraph``), une fois par groupe de sorties ; arêtes
  pénalisées près des murs pour que le chemin suive l'axe des circulations (CO 49) ;
- longueur réglementaire : ligne droite du point le plus éloigné à la porte du local de départ, puis axe des
  circulations sans les crans de la grille ; le chemin tendu au ras des murs est donné pour information.

Limites : le mobilier n'est pas un obstacle ; une sortie est atteinte dès son seuil.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .index import GeometryIndex

DEFAULT_CELL_M = 0.10
MAX_CELLS = 4_000_000
#: Classes qui bloquent la marche (coupées à ``CUT_HEIGHT_M`` au-dessus du plancher).
BLOCKING_CLASSES = ("IfcWall", "IfcCurtainWall", "IfcColumn", "IfcWindow", "IfcPlate", "IfcMember", "IfcRailing")
CUT_HEIGHT_M = 1.0
#: Demi-épaisseur de mur maximale comblée entre deux locaux pour un passage libre sans porte.
GAP_FILL_M = 0.30


def slice_segments(verts: np.ndarray, faces: np.ndarray, z: float) -> np.ndarray:
    """Segments 2D (n, 2, 2) de la coupe horizontale du maillage à l'altitude ``z``."""
    tri = verts[faces]
    d = tri[:, :, 2] - z
    d = np.where(np.abs(d) < 1e-9, 1e-9, d)  # sommet sur le plan : légèrement au-dessus
    s = np.sign(d)
    cross = ~((s[:, 0] == s[:, 1]) & (s[:, 1] == s[:, 2]))
    out = []
    for t, dd in zip(tri[cross], d[cross], strict=True):
        pts = []
        for a, b in ((0, 1), (1, 2), (2, 0)):
            if (dd[a] > 0) != (dd[b] > 0):
                u = dd[a] / (dd[a] - dd[b])
                pts.append(t[a, :2] + u * (t[b, :2] - t[a, :2]))
        if len(pts) == 2:
            out.append(pts)
    return np.array(out, float).reshape(-1, 2, 2)


def _raster_segments(segs: np.ndarray, x0: float, y0: float, cell: float, shape: tuple[int, int]) -> np.ndarray:
    """Mailles traversées par les segments (échantillonnage au quart de maille)."""
    mask = np.zeros(shape, bool)
    ny, nx = shape
    for a, b in segs:
        n = max(int(np.ceil(np.linalg.norm(b - a) / (cell / 4))), 1)
        t = np.linspace(0, 1, n + 1)[:, None]
        p = a + t * (b - a)
        i = np.floor((p[:, 0] - x0) / cell).astype(int)
        j = np.floor((p[:, 1] - y0) / cell).astype(int)
        ok = (i >= 0) & (i < nx) & (j >= 0) & (j < ny)
        mask[j[ok], i[ok]] = True
    return mask


def _raster_triangles(tris: np.ndarray, x0: float, y0: float, cell: float, shape: tuple[int, int]) -> np.ndarray:
    """Mailles dont le centre est dans au moins un des triangles 2D ``tris`` (n, 3, 2)."""
    mask = np.zeros(shape, bool)
    ny, nx = shape
    for t in tris:
        lo, hi = t.min(axis=0), t.max(axis=0)
        i0, i1 = max(int(np.floor((lo[0] - x0) / cell)), 0), min(int(np.ceil((hi[0] - x0) / cell)), nx)
        j0, j1 = max(int(np.floor((lo[1] - y0) / cell)), 0), min(int(np.ceil((hi[1] - y0) / cell)), ny)
        if i1 <= i0 or j1 <= j0:
            continue
        xs = x0 + (np.arange(i0, i1) + 0.5) * cell
        ys = y0 + (np.arange(j0, j1) + 0.5) * cell
        px, py = np.meshgrid(xs, ys)
        a, b, c = t
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        l1 = ((b[1] - c[1]) * (px - c[0]) + (c[0] - b[0]) * (py - c[1])) / d
        l2 = ((c[1] - a[1]) * (px - c[0]) + (a[0] - c[0]) * (py - c[1])) / d
        inside = (l1 >= -1e-9) & (l2 >= -1e-9) & (1 - l1 - l2 >= -1e-9)
        mask[j0:j1, i0:i1] |= inside
    return mask


def footprint_triangles(idx: GeometryIndex, guid: str) -> np.ndarray | None:
    """Triangles 2D de l'empreinte d'un élément : faces de son maillage tournées vers le haut."""
    m = idx.mesh(guid)
    if m is None:
        return None
    v, f = m
    tri = v[f]
    nz = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, 2]
    up = tri[nz > 1e-9]
    if not len(up):  # maillage sans face horizontale : toutes les faces projetées
        up = tri
    return up[:, :, :2]


#: Mesure « suivant l'axe des circulations » (CO 49) : le chemin est tenu à distance des murs. Arête pénalisée quand la
#: dégagement latéral (distance au mur le plus proche) est sous AXIS_RADIUS_M : dans un couloir, le chemin le moins
#: coûteux suit l'axe ; dans une grande salle, il longe les murs à 0,75 m au plus près.
AXIS_RADIUS_M = 0.75
AXIS_WEIGHT = 3.0


@dataclass
class StoreyPaths:
    """Grille de marche d'un étage (un niveau de plancher) et distances aux sorties, par groupe de sorties."""

    x0: float
    y0: float
    cell: float
    label: np.ndarray  # n° de local par maille (−1 : aucun)
    door: np.ndarray  # maille de porte
    dist: np.ndarray  # coût (axe des circulations) jusqu'à la sortie la plus proche, tous groupes (inf : non relié)
    walk: np.ndarray  # maille praticable (local ou porte, hors obstacle)
    spaces: list[str] = field(default_factory=list)
    zones: list[str] = field(default_factory=list)  # volumes de zone écartés (recouvrent d'autres locaux)
    exits_reached: int = 0
    ids: np.ndarray | None = None  # n° de nœud par maille (−1 : non praticable)
    nodes: np.ndarray | None = None  # (j, i) de chaque nœud
    pred: np.ndarray | None = None  # prédécesseurs vers la sortie la plus proche, tous groupes
    clear: np.ndarray | None = None  # dégagement latéral de chaque maille (m)
    door_cells: dict = field(default_factory=dict)  # porte → masque de ses mailles
    groups: dict = field(default_factory=dict)  # groupe de sorties → (coût, prédécesseurs)

    def center(self, j: int, i: int) -> tuple[float, float]:
        return self.x0 + (i + 0.5) * self.cell, self.y0 + (j + 0.5) * self.cell

    # ── chemins ──
    def cost(self, groups: list[str] | None = None) -> np.ndarray:
        """Coût jusqu'à la sortie la plus proche parmi ``groups`` (tous si None)."""
        if groups is None or not self.groups:
            return self.dist
        arrs = [self.groups[g][0] for g in groups if g in self.groups]
        return np.minimum.reduce(arrs) if arrs else np.full(self.dist.shape, np.inf)

    def nearest_group(self, j: int, i: int, groups: list[str] | None = None) -> str | None:
        keys = [g for g in (groups if groups is not None else self.groups) if g in self.groups]
        best = min(keys, key=lambda g: self.groups[g][0][j, i], default=None)
        return best if best is not None and np.isfinite(self.groups[best][0][j, i]) else None

    def grid_path(self, j: int, i: int, pred: np.ndarray | None = None) -> list[tuple[int, int]]:
        """Mailles du chemin de (j, i) à la sortie (suivi des prédécesseurs)."""
        pred = self.pred if pred is None else pred
        out = [(j, i)]
        if pred is None or self.ids is None:
            return out
        k = int(self.ids[j, i])
        while k >= 0 and pred[k] >= 0 and len(out) < 1_000_000:
            k = int(pred[k])
            out.append((int(self.nodes[k, 0]), int(self.nodes[k, 1])))
        return out

    def path_to(self, j: int, i: int, groups: list[str] | None = None) -> list[tuple[int, int]]:
        if groups is None or not self.groups:
            return self.grid_path(j, i)
        g = self.nearest_group(j, i, groups)
        return self.grid_path(j, i, self.groups[g][1]) if g else [(j, i)]

    def _visible(self, a: tuple[int, int], b: tuple[int, int], min_clear: float | None = None) -> bool:
        """Ligne droite praticable entre deux centres de maille (échantillonnée au quart de maille, sans frôler un
        obstacle en diagonale) ; ``min_clear`` : dégagement latéral minimal le long de la ligne (axe des circulations)."""
        n = max(int(np.ceil(4 * max(abs(b[0] - a[0]), abs(b[1] - a[1])))), 1)
        t = np.linspace(0.0, 1.0, n + 1)
        jj = a[0] + t * (b[0] - a[0])
        ii = a[1] + t * (b[1] - a[1])
        ny, nx = self.walk.shape
        for dj, di in ((0.0, 0.0), (0.3, 0.3), (0.3, -0.3), (-0.3, 0.3), (-0.3, -0.3)):  # épaisseur du marcheur
            if not self.walk[np.rint(jj + dj).astype(int).clip(0, ny - 1), np.rint(ii + di).astype(int).clip(0, nx - 1)].all():
                return False
        if min_clear is not None and self.clear is not None:
            if self.clear[np.rint(jj).astype(int).clip(0, ny - 1), np.rint(ii).astype(int).clip(0, nx - 1)].min() < min_clear:
                return False
        return True

    def smooth(self, cells: list[tuple[int, int]], *, axis: bool = False) -> list[tuple[int, int]]:
        """Chemin tendu : on garde un sommet seulement quand la ligne droite est bloquée (« string pulling »).

        ``axis`` : la ligne droite doit garder le dégagement latéral du chemin d'origine (à une maille près) — le
        chemin reste sur l'axe des circulations, seuls les crans de la grille disparaissent."""
        if len(cells) <= 2:
            return cells
        cl = (np.array([self.clear[c] for c in cells]) if axis and self.clear is not None else None)

        def ok(k: int, h: int) -> bool:
            thr = None if cl is None else float(cl[k:h + 1].min()) - self.cell
            return self._visible(cells[k], cells[h], thr)

        out = [cells[0]]
        k = 0
        while k < len(cells) - 1:
            lo, hi = k + 1, len(cells) - 1
            if not ok(k, hi):  # plus loin visible, par dichotomie puis balayage
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if ok(k, mid):
                        lo = mid
                    else:
                        hi = mid - 1
                while hi > k + 1 and not ok(k, hi):
                    hi -= 1
            out.append(cells[hi])
            k = hi
        return out

    def path_length(self, cells: list[tuple[int, int]]) -> float:
        p = np.array(cells, float)
        return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() * self.cell) if len(p) > 1 else 0.0

    def measure(self, cells: list[tuple[int, int]], room: int | None = None) -> dict:
        """Longueurs d'un chemin de maille : ``axe`` (réglementaire : ligne droite dans le local de départ jusqu'à sa
        porte, puis axe des circulations), ``tendu`` (au ras des murs, information), ``grille`` (brut)."""
        q = 0
        if room is not None:
            while q < len(cells) - 1 and self.label[cells[q]] == room and not self.door[cells[q]]:
                q += 1
        head = [cells[0], cells[q]] if q and self._visible(cells[0], cells[q]) else self.smooth(cells[:q + 1])
        tail = self.smooth(cells[q:], axis=True)
        axis_pts = head + tail[1:]
        taut = self.smooth(cells)
        return {"axe": self.path_length(axis_pts) + self.cell / 2, "tendu": self.path_length(taut) + self.cell / 2,
                "grille": self.path_length(cells) + self.cell / 2, "chemin": [self.center(*c) for c in axis_pts],
                "porte_local": self.center(*cells[q]) if q else None, "q": cells[q]}

    def space_distance(self, guid: str, *, refine: int = 25) -> tuple[float | None, float, tuple[float, float] | None]:
        """(distance du point le plus éloigné suivant l'axe des circulations, part non reliée, ce point)."""
        r = self.space_path(guid, refine=refine)
        return (r["distance"], r["non_relie"], r["point"]) if r else (None, 1.0, None)

    def space_path(self, guid: str, *, refine: int = 25, groups: list[str] | None = None) -> dict | None:
        """Point le plus éloigné d'un local (vers les sorties ``groups``) : distance suivant l'axe des circulations
        (``distance``), distance tendue au ras des murs (``tendu``), distance brute sur grille et polyligne."""
        if guid not in self.spaces:
            return None
        k = self.spaces.index(guid)
        cells = (self.label == k) & self.walk
        n = int(cells.sum())
        cost = self.cost(groups)
        finite = cells & np.isfinite(cost)
        if not n or not finite.any():
            return None
        unreached = 1.0 - finite.sum() / n
        d = np.where(finite, cost, -1.0)
        dmax = float(d.max())
        jj, ii = np.nonzero(d >= 0.85 * dmax)  # candidats : les plus coûteux
        order = np.argsort(-d[jj, ii])
        pick = order[np.linspace(0, len(order) - 1, min(refine, len(order))).astype(int)] if len(order) else order
        best = None
        for q in pick:
            path = self.path_to(int(jj[q]), int(ii[q]), groups)
            m = self.measure(path, room=k)
            if best is None or m["axe"] > best[0]["axe"]:
                best = (m, path)
        m, path = best
        return {"distance": m["axe"], "tendu": m["tendu"], "grille": m["grille"], "non_relie": float(unreached),
                "point": self.center(*path[0]), "chemin": m["chemin"], "porte_local": m["porte_local"],
                "cellule_porte": m["q"], "groupe": self.nearest_group(*path[0], groups) if self.groups else None}

    def door_path(self, guid: str, groups: list[str] | None = None) -> dict | None:
        """Distance depuis une porte (porte palière d'un logement), côté circulation."""
        m = self.door_cells.get(guid)
        if m is None:
            return None
        cost = self.cost(groups)
        cand = m & self.walk & np.isfinite(cost)
        if not cand.any():
            return None
        j, i = np.unravel_index(int(np.argmin(np.where(cand, cost, np.inf))), cost.shape)
        path = self.path_to(int(j), int(i), groups)
        r = self.measure(path)
        return {"distance": r["axe"], "tendu": r["tendu"], "grille": r["grille"], "point": self.center(j, i),
                "chemin": r["chemin"], "groupe": self.nearest_group(int(j), int(i), groups) if self.groups else None,
                "cellule": (int(j), int(i))}

    def dead_end(self, j: int, i: int, groups: list[str] | None = None) -> tuple[float, str]:
        """Longueur de cul-de-sac depuis (j, i) : chemin commun aux deux groupes de sorties les plus proches, jusqu'au
        point où deux sorties distinctes deviennent possibles. Un seul groupe atteignable : tout le chemin
        (« issue unique »)."""
        keys = [g for g in (groups if groups is not None else self.groups) if g in self.groups
                and np.isfinite(self.groups[g][0][j, i])]
        if not keys:
            return 0.0, "non relié"
        keys.sort(key=lambda g: self.groups[g][0][j, i])
        c1, p1 = self.groups[keys[0]]
        path = self.grid_path(j, i, p1)
        if len(keys) == 1:
            return self.measure(path)["axe"], "issue unique"
        c2 = self.groups[keys[1]][0]
        last = 0
        patience = max(int(1.0 / self.cell), 3)  # 1 m d'écart toléré (chemins parallèles d'une maille)
        for t, c in enumerate(path):
            s = c1[j, i] - c1[c]
            if c2[j, i] - c2[c] >= s - max(0.5, 0.05 * s):  # encore sur un plus court chemin vers la 2e sortie
                last = t
            elif t - last > patience:
                break
        return (self.measure(path[:last + 1])["axe"] - self.cell / 2 if last else 0.0), "cul-de-sac"


def floor_levels(idx: GeometryIndex, spaces: list[str]) -> list[float]:
    """Niveaux de plancher distincts des locaux (bases arrondies à 0,10 m) : un étage combiné en a plusieurs."""
    return sorted({round(idx.elements[g]["aabb"][2], 1) for g in spaces if idx.elements[g].get("aabb")})


def blockers_for(idx: GeometryIndex, spaces: list[str]) -> list[str]:
    """Obstacles candidats : éléments des classes bloquantes dont la boîte touche celle des locaux."""
    boxes = [idx.elements[g]["aabb"] for g in spaces if idx.elements[g].get("aabb")]
    if not boxes:
        return []
    box = [min(b[i] for b in boxes) for i in range(3)] + [max(b[i] for b in boxes) for i in range(3, 6)]
    out = []
    for g in idx.query_box(box):
        e = idx.elements.get(g)
        if e and e["has_geometry"] and not e.get("from_parts") and e["ifc_class"].startswith(BLOCKING_CLASSES):
            out.append(g)
    return out


def storey_paths(idx: GeometryIndex, spaces: list[str], doors: list[str], exits: list[str], *,
                 cell: float = DEFAULT_CELL_M, blockers: list[str] | None = None,
                 zone_names: set[str] | None = None, ipms: set[str] | None = None,
                 exit_spaces: list[str] | None = None, groups: dict | None = None,
                 extra_blocked: list[str] | None = None, step_max_m: float | None = None,
                 closed_doors: list[str] | None = None, axis: bool = True) -> StoreyPaths | None:
    """Grille de marche des ``spaces`` reliés par les ``doors`` et coûts depuis les ``exits`` (portes).

    ``blockers`` : obstacles coupés à 1 m au-dessus de chaque niveau de plancher (défaut : :func:`blockers_for`).
    ``exit_spaces`` : locaux qui comptent comme sorties (escaliers ouverts, sans porte).
    ``groups`` : {nom: (portes, locaux)} — une distance par groupe de sorties (F3 par cible, F5 culs-de-sac).
    ``extra_blocked`` : éléments infranchissables en entier (escaliers, pour le cheminement PMR).
    ``step_max_m`` : marche maximale entre deux locaux voisins (PMR) ; au-delà, le passage est coupé.
    ``closed_doors`` : portes infranchissables (trop étroites en PMR) — leur baie est bloquée, pas un passage libre.
    ``axis`` : chemin tenu dans l'axe des circulations (défaut) ; False : plus court chemin au ras des murs."""
    from scipy.ndimage import binary_closing, distance_transform_edt
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra

    sp_tris = {g: t for g in spaces if (t := footprint_triangles(idx, g)) is not None and len(t)}
    if not sp_tris:
        return None
    grp_doors = {d for ds, _ in (groups or {}).values() for d in ds}
    door_tris = {g: t for g in set(doors) | set(exits) | grp_doors
                 if (t := footprint_triangles(idx, g)) is not None and len(t)}
    allp = np.concatenate([t.reshape(-1, 2) for t in [*sp_tris.values(), *door_tris.values()]])
    lo, hi = allp.min(axis=0) - 2 * cell, allp.max(axis=0) + 2 * cell
    while ((hi - lo) / cell).prod() > MAX_CELLS:
        cell *= 1.5
    nx, ny = (int(np.ceil(v)) for v in (hi - lo) / cell)
    x0, y0 = float(lo[0]), float(lo[1])
    shape = (ny, nx)
    masks = {g: _raster_triangles(t, x0, y0, cell, shape) for g, t in sp_tris.items()}
    zones = zone_volumes(masks, zone_names, ipms)
    label = np.full(shape, -1, np.int32)
    names = [g for g in sp_tris if g not in zones]
    for k, g in enumerate(names):
        label[masks[g] & (label < 0)] = k
    door = np.zeros(shape, bool)
    door_cells: dict[str, np.ndarray] = {}
    for g, t in door_tris.items():
        # traversée du mur : boîte de la porte prolongée de DOOR_REACH_M de part et d'autre, perpendiculairement au
        # mur (un vantail affleurant une face du mur ne touche pas le local de l'autre côté)
        m = _raster_triangles(np.concatenate([t, door_passage(idx.elements[g])]), x0, y0, cell, shape)
        if not m.any():  # porte plus fine que la maille : la maille de son centre
            c = t.reshape(-1, 2).mean(axis=0)
            m[min(int((c[1] - y0) / cell), ny - 1), min(int((c[0] - x0) / cell), nx - 1)] = True
        m = _dilate(m)
        door_cells[g] = m
        door |= m
    levels = floor_levels(idx, list(sp_tris))
    blocked = np.zeros(shape, bool)
    for g in blockers_for(idx, list(sp_tris)) if blockers is None else blockers:
        e = idx.elements.get(g)
        m = idx.mesh(g) if e else None
        if m is None:
            continue
        for z0 in levels:
            z = z0 + CUT_HEIGHT_M
            if e["aabb"][2] < z < e["aabb"][5]:
                segs = slice_segments(m[0], m[1], z)
                if len(segs):
                    blocked |= _raster_segments(segs, x0, y0, cell, shape)
    hard = np.zeros(shape, bool)  # infranchissable même par une porte (escalier en PMR)
    for g in extra_blocked or ():
        t = footprint_triangles(idx, g)
        if t is not None and len(t):
            hard |= _raster_triangles(t, x0, y0, cell, shape)
    for g in closed_doors or ():
        t = footprint_triangles(idx, g)
        if t is not None and len(t):
            hard |= _dilate(_raster_triangles(np.concatenate([t, door_passage(idx.elements[g])]), x0, y0, cell, shape))
    # épaisseur des murs entre deux locaux (jusqu'à 2 × GAP_FILL_M) : praticable si la coupe ne la bloque pas
    # (passage libre) — fermeture morphologique, qui n'étend pas le contour extérieur des locaux
    r = max(int(np.ceil(GAP_FILL_M / cell)), 1)
    filled = binary_closing(label >= 0, structure=np.ones((3, 3), bool), iterations=r, border_value=0)
    walk = ((filled & ~blocked) | door) & ~hard
    clear = distance_transform_edt(walk) * cell
    zlab = np.array([idx.elements[g]["aabb"][2] for g in names] + [np.nan])  # plancher de chaque local (PMR)
    ids = np.full(shape, -1, np.int64)
    ids[walk] = np.arange(int(walk.sum()))
    rows, cols, w = [], [], []
    for dj, di in ((0, 1), (1, 0), (1, 1), (1, -1)):
        a = _shift_pair(walk, dj, di)
        if a is None:
            continue
        src, dst = a
        ok = np.ones(len(src[0]), bool)
        if dj and di:  # diagonale : pas d'angle coupé
            ok &= walk[src[0], src[1] + di] & walk[src[0] + dj, src[1]]
        if step_max_m is not None:  # marche entre deux locaux voisins de planchers différents
            za, zb = zlab[label[src]], zlab[label[dst]]
            ok &= ~(np.abs(za - zb) > step_max_m + 1e-9)
        src, dst = (src[0][ok], src[1][ok]), (dst[0][ok], dst[1][ok])
        length = cell * (np.sqrt(2) if dj and di else 1.0)
        c = np.minimum(clear[src], clear[dst])
        pen = AXIS_WEIGHT * np.clip(AXIS_RADIUS_M - c, 0, None) / AXIS_RADIUS_M if axis else 0.0
        rows.append(ids[src])
        cols.append(ids[dst])
        w.append(length * (1.0 + pen))
    n = int(walk.sum())
    graph = (coo_matrix((np.concatenate(w), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n)).tocsr()
             if rows and n else None)

    def run(src_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        cost = np.full(shape, np.inf)
        sources = ids[src_mask & walk]
        if not len(sources):
            return cost, None
        if graph is None:
            cost[src_mask & walk] = 0.0
            return cost, None
        d, pred, _ = dijkstra(graph, directed=False, indices=sources, min_only=True, return_predecessors=True)
        cost[walk] = d
        return cost, pred

    def mask_of(dlist, slist) -> np.ndarray:
        m = np.zeros(shape, bool)
        for g in dlist:
            if g in door_cells:
                m |= door_cells[g]
        for g in slist:
            if g in names:
                m |= label == names.index(g)
        return m

    exit_mask = mask_of(exits, exit_spaces or ())
    dist, pred = run(exit_mask)
    gfields = {name: run(mask_of(ds, ss)) for name, (ds, ss) in (groups or {}).items()}
    return StoreyPaths(x0=x0, y0=y0, cell=cell, label=label, door=door, dist=dist, walk=walk, spaces=names,
                       zones=sorted(zones), exits_reached=len([g for g in exits if g in door_tris]), ids=ids,
                       nodes=np.argwhere(walk), pred=pred, clear=clear, door_cells=door_cells, groups=gfields)


def zone_volumes_for(idx: GeometryIndex, spaces: list[str], labels: dict[str, str], cell: float = 0.2) -> set[str]:
    """Volumes de zone (surfaces IPMS, SHAB, « Surface »…) parmi ``spaces``, étage par étage — même détecteur que F3.
    ``labels`` : nom affiché de chaque local (LongName), pour l'indice par le nom."""
    import re

    zone_re = re.compile(r"^\s*(IPMS|SHAB|SDP|SHON|SHOB|SU\b|SURFACE|ZONE|AREA|GROSS)", re.IGNORECASE)
    by_storey: dict = {}
    for g in spaces:
        by_storey.setdefault(idx.elements[g].get("storey"), []).append(g)
    out: set[str] = set()
    for sps in by_storey.values():
        tris = {g: t for g in sps if (t := footprint_triangles(idx, g)) is not None and len(t)}
        if len(tris) < 2:
            continue
        allp = np.concatenate([t.reshape(-1, 2) for t in tris.values()])
        lo, hi = allp.min(axis=0) - cell, allp.max(axis=0) + cell
        c = cell
        while ((hi - lo) / c).prod() > MAX_CELLS:
            c *= 1.5
        nx, ny = (int(np.ceil(v)) for v in (hi - lo) / c)
        masks = {g: _raster_triangles(t, float(lo[0]), float(lo[1]), c, (ny, nx)) for g, t in tris.items()}
        named = {g for g in tris if zone_re.search(labels.get(g) or "")}
        out |= zone_volumes(masks, named, {g for g in named if (labels.get(g) or "").strip().upper().startswith("IPMS")})
    return out


#: Volume de zone : local qui recouvre au moins la moitié d'un autre local au moins 1,5 fois plus petit, et dont au
#: moins la moitié de la surface est couverte par d'autres locaux (surfaces IPMS, SHAB, « Surface » posées sur les
#: pièces). Une chambre dessinée par-dessus son placard n'en est pas un. Écarté de la grille et de F3.
ZONE_COVER_RATIO = 0.5
ZONE_SIZE_RATIO = 1.5


def zone_volumes(masks: dict[str, np.ndarray], named: set[str] | None = None,
                 ipms: set[str] | None = None) -> set[str]:
    """Volumes de zone. ``named`` : locaux dont le nom évoque une surface (IPMS, SHAB…) — zones dès qu'ils recouvrent
    d'autres locaux sur au moins 10 % de leur surface ; ``ipms`` : surfaces IPMS, jamais des pièces (IPMS 2-B = éléments
    de structure, posés sur les murs)."""
    area = {g: int(m.sum()) for g, m in masks.items()}
    boxes = {}
    for g, m in masks.items():
        if area[g]:
            jj, ii = np.nonzero(m)
            boxes[g] = (jj.min(), jj.max(), ii.min(), ii.max())
    out: set[str] = set()
    keys = list(boxes)
    for x, a in enumerate(keys):
        for b in keys[x + 1:]:
            ba, bb = boxes[a], boxes[b]
            if ba[1] < bb[0] or bb[1] < ba[0] or ba[3] < bb[2] or bb[3] < ba[2]:
                continue
            big, small = (a, b) if area[a] >= area[b] else (b, a)
            if area[big] < ZONE_SIZE_RATIO * area[small]:
                continue
            if (masks[big] & masks[small]).sum() >= ZONE_COVER_RATIO * area[small]:
                out.add(big)
    keep = set()
    for z in out:
        others = np.zeros_like(masks[z])
        for g in keys:
            if g != z and g not in out:
                others |= masks[g]
        if (masks[z] & others).sum() >= ZONE_COVER_RATIO * area[z]:
            keep.add(z)
    for z in named or ():
        if z not in masks or not area.get(z):
            continue
        others = np.zeros_like(masks[z])
        for g in keys:
            if g != z and g not in (named or ()):
                others |= masks[g]
        if (masks[z] & others).sum() >= 0.1 * area[z] or z in (ipms or ()):
            keep.add(z)
    return keep


#: Prolongement d'une porte de part et d'autre de son épaisseur, pour traverser son mur hôte.
DOOR_REACH_M = 0.30


def door_passage(e: dict) -> np.ndarray:
    """Deux triangles 2D : la boîte orientée de la porte, prolongée de ``DOOR_REACH_M`` de chaque côté dans le sens
    de l'épaisseur."""
    obb = e.get("obb")
    if not obb:
        return np.zeros((0, 3, 2))
    c = np.asarray(obb["center"], float)[:2]
    ax = np.asarray(obb["axes"], float)
    half = np.asarray(obb["half"], float)
    u, v = ax[0][:2], ax[1][:2]  # axes[0] : plus grande dimension horizontale (largeur de la porte)
    hu, hv = half[0], half[1] + DOOR_REACH_M
    p = [c - u * hu - v * hv, c + u * hu - v * hv, c + u * hu + v * hv, c - u * hu + v * hv]
    return np.array([[p[0], p[1], p[2]], [p[0], p[2], p[3]]])


def _dilate(m: np.ndarray) -> np.ndarray:
    out = m.copy()
    out[1:, :] |= m[:-1, :]
    out[:-1, :] |= m[1:, :]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def _shift_pair(walk, dj: int, di: int):
    """Couples de mailles voisines (décalage dj, di) entre lesquelles on peut marcher."""
    ny, nx = walk.shape
    j0, j1 = 0, ny - dj
    i0, i1 = max(0, -di), nx - max(0, di)
    if j1 <= j0 or i1 <= i0:
        return None
    a = (slice(j0, j1), slice(i0, i1))
    b = (slice(j0 + dj, j1 + dj), slice(i0 + di, i1 + di))
    ok = walk[a] & walk[b]
    jj, ii = np.nonzero(ok)
    return (jj + j0, ii + i0), (jj + j0 + dj, ii + i0 + di)
