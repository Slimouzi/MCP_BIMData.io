"""Règles géométriques du lot 2 : G2 (locaux qui se chevauchent), G4 (doublons), G5 (murs non
raccordés), G6 (mur sans appui), G7 (escaliers : marche, giron, 2h + g, échappée).

Appelées par :func:`bimdata_mcp.quality.rules.run_geometry_rules`, qui fournit le collecteur
d'anomalies (``_Run``), l'index géométrique et les seuils.
"""

from __future__ import annotations

from collections import Counter

import numpy as np

from ..geometry import mesh as M
from ..geometry.clash import ClashOptions, find_clashes
from ..geometry.index import GeometryIndex
from ..model_view import Record

WALLS = ("IfcWall", "IfcCurtainWall")
JOINS = WALLS + ("IfcColumn",)
SUPPORTS = ("IfcSlab", "IfcFooting", "IfcBeam", "IfcWall", "IfcCovering", "IfcRoof", "IfcPile", "IfcRamp",
            "IfcStair")
#: Obstacles en sous-face uniquement (pas les plinthes ni habillages qui longent la volée).
HEADROOM_OBSTACLES = ("IfcSlab", "IfcBeam", "IfcRoof", "IfcCovering", "IfcFlowSegment", "IfcDuctSegment",
                      "IfcPipeSegment", "IfcCableCarrierSegment")
#: En dessous, ce n'est plus une échappée insuffisante mais un conflit (relève du clash).
HEADROOM_MIN_GAP_M = 0.5
def measure_steps(idx: GeometryIndex, g: str) -> tuple[float | None, float | None]:
    """Hauteur de marche et giron mesurés sur le maillage (repli) : niveaux des faces horizontales tournées vers le
    haut (marches), écart médian entre niveaux successifs, profondeur médiane d'une marche dans le sens de la montée."""
    m = idx.mesh(g) if idx is not None else None
    if m is None:
        return None, None
    v, f = m
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(n, axis=1) / 2
    up = (n[:, 2] > 0) & (np.abs(n[:, 2]) >= 0.99 * np.linalg.norm(n, axis=1)) & (area > 1e-6)
    if up.sum() < 3:
        return None, None
    z = np.round(tri[up][:, :, 2].mean(axis=1) / 0.005) * 0.005
    levels = {}
    for zz, t, a in zip(z, tri[up], area[up], strict=True):
        levels.setdefault(float(zz), []).append((t, a))
    keep = sorted(zz for zz, items in levels.items() if sum(a for _, a in items) >= 0.05)
    if len(keep) < 3:
        return None, None
    dz = np.diff(keep)
    risers = dz[(dz > 0.08) & (dz < 0.30)]
    riser = float(np.median(risers)) if len(risers) >= 2 else None
    cents = np.array([np.concatenate([t[:, :2] for t, _ in levels[zz]]).mean(axis=0) for zz in keep])
    d = cents - cents.mean(axis=0)
    axis = np.linalg.svd(d, full_matrices=False)[2][0] if len(cents) >= 2 else np.array([1.0, 0.0])
    depths = []
    for zz in keep:
        pts = np.concatenate([t[:, :2] for t, _ in levels[zz]])
        proj = pts @ axis
        depths.append(float(proj.max() - proj.min()))
    depths = np.array(depths)
    med = float(np.median(depths)) if len(depths) else None
    tread = float(np.median(depths[depths <= 2 * med])) if med else None  # paliers écartés
    return riser, (tread if tread and 0.15 < tread < 0.60 else None)


def stair_values(rec, idx) -> tuple[float | None, str, float | None, str]:
    """Hauteur de marche et giron : Pset_StairFlightCommon / Pset_StairCommon, puis cotes Revit (Hauteur actuelle de
    la contremarche, Profondeur actuelle du giron), puis mesure sur le maillage en dernier recours."""
    from .rules import _first_number

    riser, rsrc = _first_number(rec, RISER_FIELDS)
    tread, tsrc = _first_number(rec, TREAD_FIELDS)
    if riser is None and tread is None and idx is not None and rec.uuid in idx.elements:
        mr, mt = measure_steps(idx, rec.uuid)
        if mr is not None:
            riser, rsrc = mr, "mesure sur le maillage"
        if mt is not None:
            tread, tsrc = mt, "mesure sur le maillage"
    return riser, rsrc, tread, tsrc


RISER_FIELDS = ("Pset_StairFlightCommon.RiserHeight", "Pset_StairCommon.RiserHeight",
                "*.Hauteur actuelle de la contremarche", "*.Actual Riser Height", "*.RiserHeight")
TREAD_FIELDS = ("Pset_StairFlightCommon.TreadLength", "Pset_StairCommon.TreadLength",
                "*.Profondeur actuelle du giron", "*.Actual Tread Depth", "*.TreadLength")


def _is(cls: str, bases: tuple[str, ...]) -> bool:
    return any(cls == b or cls.startswith(b) for b in bases)


def run_lot2_rules(run, idx: GeometryIndex | None, scope: list[Record], ids: set[str], th: dict) -> None:
    from .rules import _first_number, _round

    if idx is None:
        run.notes.append("G2, G4 à G7 : index géométrique indisponible, règles non contrôlées "
                         "(G7 : hauteur de marche et giron contrôlés sur les propriétés seulement).")
    uuids = {r.uuid for r in scope}
    # GlobalId → clés d'index (un GlobalId en double donne plusieurs occurrences, toutes contrôlées)
    in_idx = [g for g in (idx.resolve(uuids) if idx else []) if idx.elements[g]["has_geometry"]]

    # G2 — locaux qui se chevauchent
    if "G2" in ids and idx is not None:
        spaces = [g for g in in_idx if idx.elements[g]["ifc_class"] == "IfcSpace"]
        pat = th.get("space_overlap_exclude_pattern")
        if pat:  # ex. surfaces de mesurage (IPMS) qui recouvrent les pièces par construction
            import re

            rx = re.compile(pat, re.IGNORECASE)
            by = {r.uuid: r for r in scope}
            kept = [g for g in spaces if not rx.search(str((by.get(g).attrs.get("LongName") if by.get(g) else "")
                                                           or idx.elements[g]["name"] or ""))]
            if len(kept) < len(spaces):
                run.notes.append(f"G2 : {len(spaces) - len(kept)} local(aux) exclu(s) par space_overlap_exclude_pattern.")
            spaces = kept
        for g in spaces:
            run.seen("G2", g)
        opts = ClashOptions(tol_h=0.0, tol_v=0.0, types=frozenset({"overlap", "inside", "duplicate"}),
                            exceptions={})
        clashes, _ = find_clashes(idx, spaces, spaces, opts)
        thr = th["space_overlap_min_m3"]
        from ..geometry.paths import zone_volumes_for

        by_rec = {r.uuid: r for r in scope}
        labels = {g: str((by_rec[g].attrs.get("LongName") if g in by_rec else None) or idx.elements[g]["name"] or "")
                  for g in spaces}
        zones = zone_volumes_for(idx, spaces, labels)  # surfaces IPMS, SHAB… : se superposent aux pièces par nature
        if zones:
            run.notes.append(f"G2 : {len(zones)} volume(s) de zone (surfaces IPMS, SHAB…, même détecteur que F3) — "
                             "leurs chevauchements sont en information.")
        for c in clashes:
            vol = c.volume
            if vol is None:  # maillage ouvert : on juge sur les pénétrations
                if min(c.depth_h or 0, c.depth_v or 0) <= 0.05 and c.clash_type == "overlap":
                    continue
            elif vol <= thr:
                continue
            nb = idx.elements[c.b]
            if c.a in zones or c.b in zones:
                z, other = (c.a, c.b) if c.a in zones else (c.b, c.a)
                run.add("G2", z, f"Volume de zone « {labels.get(z) or idx.elements[z]['name']} » posé sur le local "
                        f"« {labels.get(other) or idx.elements[other]['name']} »"
                        + (f" ({vol:.3f} m³ en commun)" if vol is not None else "") + " : superposition normale",
                        status="exclu", severity="info", actual=None if vol is None else round(vol, 4),
                        elements=[{"uuid": z, "role": "volume de zone"}, {"uuid": other, "role": "local"}])
                continue
            run.add("G2", c.a, f"Chevauche le local « {nb['name']} »"
                    + (f" : {vol:.3f} m³ en commun" if vol is not None else " (volume non mesurable : maillage ouvert)"),
                    actual=None if vol is None else round(vol, 4), expected=f"≤ {thr} m³ en commun",
                    measure={"value": None if vol is None else round(vol, 4), "unit": "m³"},
                    threshold={"value": thr, "unit": "m³", "source": "space_overlap_min_m3"}, bbox=c.inter_aabb,
                    elements=[{"uuid": c.a, "role": "local"}, {"uuid": c.b, "role": "local"}])

    # G4 — objets en double dans une même classe
    if "G4" in ids and idx is not None:
        phys = [g for g in in_idx if idx.elements[g]["ifc_class"] != "IfcSpace" and not idx.elements[g].get("from_parts")]
        for g in phys:
            run.seen("G4", g)
        opts = ClashOptions(types=frozenset({"duplicate"}), same_class_only=True, exceptions={})
        clashes, _ = find_clashes(idx, phys, phys, opts)
        from ..geometry.clash import connected_groups

        # une ligne par groupe superposé (13 compteurs empilés = 1 ligne), pas une par paire
        for grp in connected_groups([(c.a, c.b) for c in clashes]):
            e0 = idx.elements[grp[0]]
            boxes = [idx.elements[g]["aabb"] for g in grp]
            bbox = [min(b[i] for b in boxes) for i in range(3)] + [max(b[i] for b in boxes) for i in range(3, 6)]
            kinds = Counter((idx.elements[g]["name"] or "").rsplit(":", 1)[0] for g in grp)
            what = ", ".join(f"« {k} »" + (f" × {n}" if n > 1 else "") for k, n in kinds.most_common(3))
            run.add("G4", grp[0], f"{len(grp)} éléments {e0['ifc_class']} superposés au même emplacement (même emprise "
                    f"et même volume) : {what} — en garder un seul", bbox=bbox,
                    expected="un seul élément à cet emplacement",
                    measure={"value": len(grp), "unit": "éléments", "n_elements": len(grp) - 1},
                    elements=[{"uuid": g, "role": "élément" if k == 0 else "doublon"} for k, g in enumerate(grp)])

    # G5 — murs non raccordés
    if "G5" in ids and idx is not None:
        gap = th["wall_join_gap_m"]
        for g in in_idx:
            e = idx.elements[g]
            if not _is(e["ifc_class"], WALLS) or not e.get("obb") or e["obb"]["half"][0] < 0.15:
                continue
            run.seen("G5", g)
            o = e["obb"]
            c, u, w = np.asarray(o["center"]), np.asarray(o["axes"][0]), np.asarray(o["axes"][1])
            h, t, hz = o["half"][0], o["half"][1], o["half"][2]
            free = []
            for side, sign in (("début", -1.0), ("fin", 1.0)):
                # Face d'extrémité entière (deux arêtes + milieu, à deux hauteurs) : dans un raccord en L à
                # recouvrement, le mur traversant ne touche son voisin que par l'arête intérieure de sa face d'about.
                end = c + sign * u * h
                pts = np.array([end + w * t * f + np.array([0, 0, dz]) for f in (-1.0, 0.0, 1.0)
                                for dz in (0.0, -hz + min(0.05, hz))])
                p = end
                box = [*(p - 0.5 - t), *(p + 0.5 + t)]
                best = np.inf
                for other in idx.query_box(box):
                    if other == g:
                        continue
                    eo = idx.elements.get(other)
                    if not eo or not eo.get("obb") or not _is(eo["ifc_class"], JOINS):
                        continue
                    best = min(best, float(M.point_obb_distance(pts, eo["obb"]).min()))
                if best > gap:
                    free.append((side, best, p))
            for side, d, p in free:
                dist = "aucun mur ni poteau à moins de 0,50 m" if not np.isfinite(d) else f"plus proche à {d:.3f} m"
                run.add("G5", g, f"Extrémité ({side}) non raccordée : {dist}", actual=None if not np.isfinite(d)
                        else round(d, 3), expected=f"≤ {gap} m d'un mur ou d'un poteau",
                        measure={"value": None if not np.isfinite(d) else round(d, 3), "unit": "m"},
                        threshold={"value": gap, "unit": "m", "source": "wall_join_gap_m"},
                        bbox=[*(p - 0.05), *(p + 0.05)])

    # G6 — mur qui ne repose pas sur son support
    if "G6" in ids and idx is not None:
        tol, search = th["wall_support_gap_m"], th["wall_support_search_m"]
        screed_max, level_tol = th["wall_screed_max_m"], th["wall_level_tolerance_m"]
        storey_z = {s["guid"]: s["z"] for s in idx.ifc.get("storeys") or [] if s.get("z") is not None}
        no_support = 0
        for g in in_idx:
            e = idx.elements[g]
            if not _is(e["ifc_class"], WALLS) or not e.get("obb"):
                continue
            run.seen("G6", g)
            o = e["obb"]
            base = e["aabb"][2]
            c, u, h = np.asarray(o["center"]), np.asarray(o["axes"][0]), o["half"][0]
            pts = np.array([c + u * h * f for f in (-0.8, 0.0, 0.8)])
            pts[:, 2] = base
            box = [*(pts.min(axis=0) - [0.05, 0.05, search]), *(pts.max(axis=0) + [0.05, 0.05, 0.05])]
            supports = [x for x in idx.query_box(box) if x != g and idx.elements.get(x)
                        and _is(idx.elements[x]["ifc_class"], SUPPORTS) and idx.mesh(x) is not None]
            gaps = []  # (écart, classe du support) par point de base
            for p in pts:
                best, who = np.inf, None
                for x in supports:
                    v, f = idx.mesh(x)
                    if idx.elements[x].get("is_closed") and M.points_inside((p - [0, 0, 0.01])[None, :], v, f)[0]:
                        best, who = 0.0, x
                        break
                    _, t = M.ray_hits((p + [0, 0, 1e-4])[None, :], np.array([0.0, 0.0, -1.0]), v, f)
                    d = float(t[0]) - 1e-4
                    is_slab = _is(idx.elements[x]["ifc_class"], ("IfcSlab",))
                    # à hauteur égale (± 5 mm), la dalle l'emporte sur un mur ou une poutre du dessous
                    if t[0] < np.inf and (d < best - 0.005 or (abs(d - best) <= 0.005 and is_slab)):
                        best, who = min(d, best) if abs(d - best) <= 0.005 else d, x
                gaps.append((best, who))
            measured = [x for x in gaps if x[0] <= search]
            if not measured:
                no_support += 1
                continue
            gmin, sup = min(measured, key=lambda x: x[0])
            if gmin > tol:
                level = storey_z.get(e.get("storey"))
                # bas du mur au niveau de l'étage, ou au niveau fini (jusqu'à screed_max au-dessus)
                on_level = level is not None and level - level_tol <= base <= level + screed_max + 1e-6
                sup_e = idx.elements[sup] if sup is not None else None
                slab = sup_e is not None and _is(sup_e["ifc_class"], ("IfcSlab",))
                # support = structure de l'étage du dessous (mur, poteau, poutre) arrêtée sous le niveau : la dalle
                # n'est pas modélisée au droit du mur
                below = sup_e is not None and not slab and level is not None and sup_e["aabb"][5] <= level + level_tol
                if on_level and (slab or below) and gmin <= screed_max + 1e-6:
                    cause = (f"dalle {gmin:.3f} m dessous (« {sup_e['name']} »)" if slab else
                             f"pas de dalle au droit du mur, structure du dessous à {gmin:.3f} m (« {sup_e['name']} »)")
                    lvl = f"fini ({base - level:+.2f} m)" if base > level + level_tol else "de l'étage"
                    run.add("G6", g, f"Chape non modélisée : bas du mur au niveau {lvl}, {cause}", status="exclu",
                            severity="info",
                            actual=round(gmin, 3), expected=f"≤ {tol} m, ou chape ≤ {screed_max} m sous le niveau",
                            measure={"value": round(gmin, 3), "unit": "m"},
                            threshold={"value": screed_max, "unit": "m", "source": "wall_screed_max_m"},
                            elements=[{"uuid": g, "role": "mur"}, {"uuid": sup, "role": "dalle" if slab else "support"}])
                else:
                    run.add("G6", g, f"Mur flottant : bas du mur à {gmin:.3f} m au-dessus du support le plus proche",
                            actual=round(gmin, 3), expected=f"≤ {tol} m", measure={"value": round(gmin, 3), "unit": "m"},
                            threshold={"value": tol, "unit": "m", "source": "wall_support_gap_m"},
                            elements=[{"uuid": g, "role": "mur"}] + ([{"uuid": sup, "role": "support"}] if sup else []))
        if no_support:
            run.notes.append(f"G6 : {no_support} mur(s) sans dalle, fondation ni poutre à moins de {search} m "
                             "sous leur base (murs extérieurs sur terrain, par exemple) : non contrôlés.")

    # G7 — escaliers
    if "G7" in ids:
        rmax, tmin = th["stair_riser_max_m"], th["stair_tread_min_m"]
        bmin, bmax, hmin = th["stair_blondel_min_m"], th["stair_blondel_max_m"], th["stair_headroom_min_m"]
        no_data = 0
        by_uuid = {r.uuid: r for r in scope}

        def has_data(r):
            return r is not None and (_first_number(r, RISER_FIELDS)[0] is not None
                                      or _first_number(r, TREAD_FIELDS)[0] is not None)

        for rec in (r for r in scope if r.ifc_class in ("IfcStair", "IfcStairFlight")):
            parent = ((idx.elements.get(rec.uuid) or {}).get("parent") if idx else None)
            if parent and has_data(by_uuid.get(parent)):
                continue  # volée ou sous-escalier qui reprend les valeurs de son escalier : une anomalie par escalier
            riser, rsrc, tread, tsrc = stair_values(rec, idx)
            if riser is None and tread is None:
                no_data += 1
            else:
                run.seen("G7", rec.uuid)
            est = "maillage" in rsrc + tsrc  # mesure de repli, approximative : information à vérifier
            sev = {"severity": "info"} if est else {}
            tag = " (estimation sur le maillage, à vérifier)" if est else ""
            if riser is not None and riser > rmax + 1e-6:
                run.add("G7", rec.uuid, f"Hauteur de marche {riser:.3f} m > {rmax:.2f} m{tag}", status="sous_seuil", **sev,
                        actual=round(riser, 4), expected=f"≤ {rmax} m ({rsrc})",
                        measure={"value": round(riser, 4), "unit": "m", "source": rsrc},
                        threshold={"value": rmax, "unit": "m", "source": "stair_riser_max_m"})
                run.issues[-1].method = "Données"
            if tread is not None and tread < tmin - 1e-6:
                run.add("G7", rec.uuid, f"Giron {tread:.3f} m < {tmin:.2f} m{tag}", status="sous_seuil", **sev,
                        actual=round(tread, 4), expected=f"≥ {tmin} m ({tsrc})",
                        measure={"value": round(tread, 4), "unit": "m", "source": tsrc},
                        threshold={"value": tmin, "unit": "m", "source": "stair_tread_min_m"})
                run.issues[-1].method = "Données"
            if riser is not None and tread is not None:
                bl = 2 * riser + tread
                if not (bmin - 1e-6 <= bl <= bmax + 1e-6):
                    run.add("G7", rec.uuid, f"2h + g = {bl:.3f} m hors de [{bmin:.2f} ; {bmax:.2f}] m{tag}",
                            status="incoherent", severity="info" if est else "mineur", actual=round(bl, 4),
                            expected=f"entre {bmin} et {bmax} m",
                            measure={"value": round(bl, 4), "unit": "m"},
                            threshold={"value": f"{bmin}–{bmax}", "unit": "m",
                                       "source": "stair_blondel_min_m / stair_blondel_max_m"})
                    run.issues[-1].method = "Données"
        if no_data:
            run.notes.append(f"G7 : {no_data} escalier(s) ou volée(s) sans hauteur de marche ni giron lisibles.")
        if idx is not None:
            _headroom(run, idx, uuids, hmin, _round)


def _headroom(run, idx: GeometryIndex, uuids: set[str], hmin: float, _round) -> None:
    """Échappée : grille de points (pas ≤ 0,10 m) sur l'emprise commune escalier / obstacle ; en chaque point, un
    rayon descendant trouve le dessus des marches, un rayon montant la sous-face de l'obstacle."""
    down, up = np.array([0.0, 0.0, -1.0]), np.array([0.0, 0.0, 1.0])
    for g in uuids:
        e = idx.elements.get(g)
        if not e or e["ifc_class"] not in ("IfcStair", "IfcStairFlight") or not e["has_geometry"] or e.get("from_parts"):
            continue
        sv, sf = idx.mesh(g)
        b = e["aabb"]
        box = [b[0], b[1], b[2] + 0.05, b[3], b[4], b[5] + hmin + 0.5]
        family = {g, e.get("parent")} | {x for x, o in idx.elements.items() if o.get("parent") in (g, e.get("parent"))
                                         and o.get("parent")}
        best, who = np.inf, None
        run.seen("G7", g)
        for o in idx.query_box(box):
            eo = idx.elements.get(o)
            if o in family or not eo or not _is(eo["ifc_class"], HEADROOM_OBSTACLES) or idx.mesh(o) is None:
                continue
            ob = eo["aabb"]
            x0, x1, y0, y1 = max(b[0], ob[0]), min(b[3], ob[3]), max(b[1], ob[1]), min(b[4], ob[4])
            if x1 <= x0 or y1 <= y0 or ob[2] <= b[2] + 0.05:
                continue
            # Pas de 0,10 m au plus, élargi pour plafonner la grille à ~4 000 points.
            step = max(min(0.10, (x1 - x0) / 2, (y1 - y0) / 2), ((x1 - x0) * (y1 - y0) / 4000) ** 0.5, 0.01)
            xs = np.arange(x0 + step / 2, x1, step)
            ys = np.arange(y0 + step / 2, y1, step)
            grid = np.array([[x, y, b[5] + 0.1] for x in xs for y in ys])
            if len(grid) == 0:
                continue
            _, t_stair = M.ray_hits(grid, down, sv, sf)
            ok = np.isfinite(t_stair)
            if not ok.any():
                continue
            top = grid[ok].copy()
            top[:, 2] -= t_stair[ok] - 1e-3  # dessus des marches (+1 mm)
            v, f = idx.mesh(o)
            _, t = M.ray_hits(top, up, v, M.faces_in_box(v, f, box))
            t = t[np.isfinite(t)] + 1e-3
            t = t[t > HEADROOM_MIN_GAP_M]
            if len(t) and t.min() < best:
                best, who = float(t.min()), o
        if who is not None and best < hmin - 1e-6:
            eo = idx.elements[who]
            run.add("G7", g, f"Échappée {best:.2f} m < {hmin:.2f} m sous « {eo['name']} » ({eo['ifc_class']})",
                    status="sous_seuil", actual=round(best, 3), expected=f"≥ {hmin} m",
                    measure={"value": round(best, 3), "unit": "m"},
                    threshold={"value": hmin, "unit": "m", "source": "stair_headroom_min_m"},
                    elements=[{"uuid": g, "role": "escalier"}, {"uuid": who, "role": "obstacle"}])
            run.issues[-1].method = "Maillage"
