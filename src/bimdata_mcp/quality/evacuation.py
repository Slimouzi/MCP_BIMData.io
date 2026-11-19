"""Évacuation : F3 (distance à la sortie, par référentiel), F5 (culs-de-sac), F6 (espaces d'attente sécurisés).

Distance mesurée suivant l'axe des circulations (CO 49) sur la grille de marche de :mod:`..geometry.paths` : ligne
droite du point le plus éloigné à la porte du local de départ, puis axe des circulations. Le chemin tendu au ras des
murs est donné pour information.

Référentiels (``f3_referentiel``) — seuils indicatifs, la conformité se juge avec le bureau de contrôle et la
commission de sécurité selon le type et la catégorie de l'établissement :

- ``ERP`` : étages et sous-sols (CO 49) — vers un escalier protégé, 40 m, 30 m en cul-de-sac, 30 m vers un escalier
  non protégé ; niveau d'accès (CO 43) — vers une sortie sur l'extérieur ou un dégagement protégé, 50 m si le choix
  existe entre plusieurs sorties, 30 m sinon ; culs-de-sac de 10 m au plus (F5 ; ERP de 5e catégorie) ;
- ``ERP_J`` : vers un escalier, 40 m, 30 m en cul-de-sac ; niveau d'accès comme l'ERP ;
- ``habitation_3A`` : de la porte palière du logement le plus éloigné à l'escalier, 10 m ;
- ``habitation_3B_4`` : de la porte du logement à l'escalier (circulations à l'air libre), 25 m ;
- ``code_travail`` : du poste de travail à l'escalier, 40 m en étage ou sous-sol ; 10 m en cul-de-sac ;
- ``personnalise`` : ``max_distance_m`` vers toutes les sorties.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np

from ..geometry import zones as Z
from ..geometry.index import GeometryIndex
from ..model_view import Record, resolve_field
from ..selection import _num

F3_REFERENTIELS: dict[str, dict] = {
    "ERP": {
        "label": "ERP (CO 43 au niveau d'accès, CO 49 en étage et sous-sol)", "origine": "local",
        "etage": {"cible": "escalier", "seuil": 40.0, "cul_de_sac": 30.0, "non_protege": 30.0, "source": "CO 49"},
        "acces": {"cible": "exterieure", "seuil": 50.0, "cul_de_sac": 30.0, "source": "CO 43"},
        "cul_de_sac_max": 10.0, "cds_source": "ERP de 5e catégorie (PE) — à confirmer pour les autres catégories",
    },
    "ERP_J": {
        "label": "ERP de type J (personnes âgées ou handicapées)", "origine": "local",
        "etage": {"cible": "escalier", "seuil": 40.0, "cul_de_sac": 30.0, "source": "type J"},
        "acces": {"cible": "exterieure", "seuil": 50.0, "cul_de_sac": 30.0, "source": "CO 43"},
        "cul_de_sac_max": None,
    },
    "habitation_3A": {
        "label": "Habitation, 3e famille A", "origine": "porte_paliere",
        "etage": {"cible": "escalier", "seuil": 10.0, "source": "habitation 3e famille A"},
        "acces": {"cible": "toutes", "seuil": 10.0, "source": "habitation 3e famille A"},
        "cul_de_sac_max": None,
    },
    "habitation_3B_4": {
        "label": "Habitation, 3e famille B et 4e famille (circulations à l'air libre)", "origine": "porte_paliere",
        "etage": {"cible": "escalier", "seuil": 25.0, "source": "habitation 3e famille B / 4e famille"},
        "acces": {"cible": "toutes", "seuil": 25.0, "source": "habitation 3e famille B / 4e famille"},
        "cul_de_sac_max": None,
    },
    "code_travail": {
        "label": "Code du travail", "origine": "local",
        "etage": {"cible": "escalier", "seuil": 40.0, "cul_de_sac": 10.0, "source": "Code du travail"},
        "acces": {"cible": "exterieure", "seuil": None, "cul_de_sac": 10.0, "source": "Code du travail"},
        "cul_de_sac_max": None,
    },
    "personnalise": {
        "label": "Personnalisé (max_distance_m)", "origine": "local",
        "etage": {"cible": "toutes", "seuil": None, "source": "max_distance_m"},
        "acces": {"cible": "toutes", "seuil": None, "source": "max_distance_m"},
        "cul_de_sac_max": None,
    },
}

#: Cul-de-sac : en deçà, deux sorties distinctes sont possibles (bruit de la grille).
DEAD_END_TOL_M = 1.0
#: Porte palière (habitation) : nom du type, sinon porte entre un logement et une circulation commune.
LANDING_RE = re.compile(r"pali[eè]re|\bppal\b|^\s*ppal|entr[ée]e (du )?logement", re.IGNORECASE)
COMMON_RE = re.compile(r"palier|coursive|circulation|\bhall\b|couloir commun|d[ée]gagement commun|cage",
                       re.IGNORECASE)
#: EAS : une place de fauteuil roulant = 0,80 × 1,30 m ; 2 places jusqu'à 50 personnes, + 1 par tranche de 50.
EAS_PLACE_M2 = 0.80 * 1.30


def eas_places(effectif: int) -> int:
    return 2 + max(0, math.ceil((effectif - 50) / 50))


def _center2(idx: GeometryIndex, g: str) -> np.ndarray:
    b = idx.elements[g]["aabb"]
    return np.array([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2])


def classify_exits(idx: GeometryIndex, records: dict[str, Record], exits: list[str], open_stairs: list[str],
                   protected: set[str] | str | None) -> dict[str, dict]:
    """Groupes de sorties : un par cage d'escalier (portes qui y donnent + escalier ouvert), un par sortie
    extérieure (portes à moins de 3 m regroupées). ``protected`` : « all », « none », None (inconnu) ou ensemble de
    locaux d'escalier / portes / circulations protégés."""
    from .lot3 import door_sides

    loc = Z.SpaceLocator(idx)
    stairs = [g for g, e in idx.elements.items() if e["ifc_class"] in ("IfcStair", "IfcStairFlight") and e["has_geometry"]]
    with_stair = {loc.of_element(g) for g in stairs} - {None}
    groups: dict[str, dict] = {}
    ext: list[tuple[str, np.ndarray]] = []
    for d in exits:
        sides = door_sides(idx, records, d, loc)
        sps = [sp for sp, _ in sides.values()]
        st = next((sp for sp in sps if sp in with_stair), None)
        if st:
            gname = f"escalier:{st}"
            grp = groups.setdefault(gname, {"kind": "escalier", "doors": [], "spaces": [], "stair": st})
            grp["doors"].append(d)
        else:
            ext.append((d, _center2(idx, d)))
    for d, c in ext:  # sorties extérieures voisines regroupées (portes doubles)
        near = next((k for k, grp in groups.items() if grp["kind"] == "exterieure"
                     and min(np.linalg.norm(_center2(idx, x) - c) for x in grp["doors"]) < 3.0), None)
        if near:
            groups[near]["doors"].append(d)
        else:
            groups[f"exterieure:{d}"] = {"kind": "exterieure", "doors": [d], "spaces": [], "stair": None}
    for sp in open_stairs:
        groups.setdefault(f"escalier:{sp}", {"kind": "escalier", "doors": [], "spaces": [], "stair": sp})
        groups[f"escalier:{sp}"]["spaces"].append(sp)
        groups[f"escalier:{sp}"]["ouvert"] = True
    for grp in groups.values():
        if grp["kind"] != "escalier":
            grp["protege"] = None
        elif grp.get("ouvert"):
            grp["protege"] = False  # escalier ouvert : jamais protégé
        elif protected == "all":
            grp["protege"] = True
        elif protected == "none":
            grp["protege"] = False
        elif isinstance(protected, set):
            grp["protege"] = grp["stair"] in protected or any(d in protected for d in grp["doors"])
        else:
            grp["protege"] = None
    if isinstance(protected, set):  # circulations horizontales protégées désignées : cibles comme un escalier protégé
        for sp in protected:
            e = idx.elements.get(sp)
            if e and e["ifc_class"] == "IfcSpace" and sp not in with_stair:
                groups[f"protege:{sp}"] = {"kind": "circulation_protegee", "doors": [], "spaces": [sp],
                                           "stair": None, "protege": True}
    return groups


def landing_doors(idx: GeometryIndex, records: dict[str, Record]) -> list[str]:
    """Portes palières : nom du type (palière, Ppal, entrée du logement), sinon porte entre une circulation commune et
    un local qui n'en est pas une."""
    from .lot3 import door_sides

    loc = Z.SpaceLocator(idx)
    out = []
    for g, e in idx.elements.items():
        if not e["ifc_class"].startswith("IfcDoor") or not e["has_geometry"]:
            continue
        if LANDING_RE.search(e.get("name") or ""):
            out.append(g)
            continue
        sides = list(door_sides(idx, records, g, loc).values())
        if len(sides) == 2 and all(sp for sp, _ in sides):
            common = [bool(COMMON_RE.search(lbl or "")) for _, lbl in sides]
            if sum(common) == 1:
                out.append(g)
    return out


def _door_width(records: dict[str, Record], g: str) -> float | None:
    rec = records.get(g)
    return _num(resolve_field(rec, "OverallWidth").value) if rec else None


def run_evacuation(run, idx: GeometryIndex, records: dict[str, Record], *, ids: list[str], exits: list[str],
                   exit_spaces: list[str], referentiel: str | None, max_distance_m: float | None, origin: str | None,
                   protected: set[str] | str | None, access_levels: set[str], landings: list[str] | None,
                   eas: list[str] | None, effectif, dead_end_max_m: float | None, pmr_door_min_m: float,
                   step_max_m: float, method: str, cell_m: float | None) -> dict:
    """F3, F5, F6 sur chaque étage et chaque niveau de plancher. Renvoie un résumé pour le rapport."""
    from ..geometry.paths import DEFAULT_CELL_M, storey_paths
    from .lot3 import (
        LEVEL_GAP_M,
        SHAFT_PATTERN,
        ZONE_NAME_RE,
        _doors_to_outside,
        _level_groups,
        _space_label,
        door_sides,
    )

    if referentiel is None:
        referentiel = "personnalise"
    if referentiel not in F3_REFERENTIELS:
        raise ValueError(f"Référentiel F3 « {referentiel} » inconnu : {', '.join(F3_REFERENTIELS)}.")
    ref = F3_REFERENTIELS[referentiel]
    origin = origin or ref["origine"]
    cds_max = dead_end_max_m if dead_end_max_m is not None else ref.get("cul_de_sac_max")
    cell = cell_m or DEFAULT_CELL_M
    names = idx.storey_names()
    groups_all = classify_exits(idx, records, exits, exit_spaces, protected)
    shaft = re.compile(SHAFT_PATTERN, re.IGNORECASE)
    spaces_of: dict[str | None, list[str]] = defaultdict(list)
    for g, e in idx.elements.items():
        if e["ifc_class"] == "IfcSpace" and e["has_geometry"]:
            rec = records.get(g)
            if rec is None or not shaft.search(str(rec.attrs.get("LongName") or "")):
                spaces_of[e.get("storey")].append(g)
    doors_of: dict[str | None, list[str]] = defaultdict(list)
    for g, e in idx.elements.items():
        if e["ifc_class"].startswith("IfcDoor") and e["has_geometry"]:
            doors_of[e.get("storey")].append(g)
    landing_set = set(landings) if landings is not None else (set(landing_doors(idx, records))
                                                               if origin == "porte_paliere" else set())
    eas_set = set(eas or ())
    summary = {"referentiel": ref["label"], "origine": origin, "groupes_de_sorties": Counter(
        grp["kind"] for grp in groups_all.values()), "escaliers_proteges": Counter(
        str(grp["protege"]) for grp in groups_all.values() if grp["kind"] == "escalier"),
        "portes_palieres": len(landing_set), "culs_de_sac": 0, "non_concluants": 0}
    no_exit: Counter = Counter()
    unlinked: list[tuple[str, float]] = []
    zones: list[str] = []
    eas_levels: dict[tuple, float] = {}

    def at_level(g, grp):
        z = idx.elements[g]["aabb"][2]
        lo = min(idx.elements[s]["aabb"][2] for s in grp) - LEVEL_GAP_M
        hi = max(idx.elements[s]["aabb"][2] for s in grp) + LEVEL_GAP_M
        return lo <= z <= hi

    for st, sps_all in spaces_of.items():
        levels = _level_groups(idx, sps_all)
        multi = len(levels) > 1
        kind = "acces" if st in access_levels else "etage"
        cfg = ref[kind]
        seuil = cfg["seuil"] if cfg["seuil"] is not None else max_distance_m
        for lv, sps in levels:
            lv_key = (st, lv if multi else None)
            doors = [d for d in doors_of.get(st, []) if at_level(d, sps)]
            lvl_groups = {}
            for name, grp in groups_all.items():
                ds = [d for d in grp["doors"] if d in set(doors) or (idx.elements[d].get("storey") == st and at_level(d, sps))]
                ss = [s for s in grp["spaces"] if s in set(sps)]
                if ds or ss:
                    lvl_groups[name] = {**grp, "doors": ds, "spaces": ss}
            open_sp = {s for grp in lvl_groups.values() for s in grp["spaces"]}
            sps = [g for g in sps if g not in open_sp]

            def subset(cible, lvl_groups=lvl_groups):
                if cible == "escalier":
                    return [k for k, grp in lvl_groups.items() if grp["kind"] in ("escalier", "circulation_protegee")]
                if cible == "exterieure":
                    return [k for k, grp in lvl_groups.items() if grp["kind"] == "exterieure" or grp.get("protege")]
                return list(lvl_groups)

            targets = subset(cfg["cible"])
            if ("F3" in ids or "F5" in ids) and not targets:
                no_exit[lv_key] += len(sps)
            lbl = {g: str((records[g].attrs.get("LongName") if g in records else None) or idx.elements[g]["name"] or "")
                   for g in sps}
            named = {g for g in sps if ZONE_NAME_RE.search(lbl[g])}
            ipms = {g for g in named if lbl[g].strip().upper().startswith("IPMS")}
            ex_doors = [d for grp in lvl_groups.values() for d in grp["doors"]]
            paths = None
            if targets and ("F3" in ids or "F5" in ids):
                paths = storey_paths(idx, sps + sorted(open_sp), doors, ex_doors, cell=cell, exit_spaces=sorted(open_sp),
                                     zone_names=named, ipms=ipms,
                                     groups={k: (grp["doors"], grp["spaces"]) for k, grp in lvl_groups.items()})
                if paths is not None:
                    zones += paths.zones
            if paths is not None:
                _f3_level(run, idx, records, paths, sps, targets, subset, lvl_groups, cfg, seuil, kind, ref,
                          referentiel, origin, landing_set, doors, cds_max, ids, method, unlinked, summary)
            # F6 : espaces d'attente sécurisés
            if "F6" in ids and eas_set:
                _f6_level(run, idx, records, st, lv_key, sps, doors, lvl_groups, kind, seuil, eas_set, effectif,
                          pmr_door_min_m, step_max_m, cell, named, ipms, names, eas_levels, door_sides)
    loc = Z.SpaceLocator(idx) if unlinked else None
    for g, far_e in unlinked:
        outside = _doors_to_outside(idx, records, g, loc)
        around = {idx.elements[h]["ifc_class"] for h in idx.query_box(idx.elements[g]["aabb"], tol=0.3) if h != g}
        isolated = not any(c.startswith(("IfcWall", "IfcDoor", "IfcCurtainWall", "IfcSlab", "IfcColumn"))
                           for c in around)
        why = (f"sa seule issue est une porte vers l'extérieur hors niveau d'accès ({', '.join(outside)}) : sortie à "
               "confirmer (exits ou access_storeys)" if outside else
               "local isolé, sans mur, dalle ni porte autour (hors du bâtiment ? local à supprimer ?)" if isolated else
               "porte manquante, local enclavé (gaine) ou sortie sur un autre étage")
        run.add("F3", g, f"Local relié à aucune sortie par les locaux et les portes de son étage (à vol d'oiseau : "
                f"{far_e:.1f} m) : {why}", status="incoherent", severity="info", actual=round(far_e, 2))
    if zones:
        run.notes.append(f"F3 : {len(set(zones))} volume(s) de zone écarté(s) (local qui recouvre d'autres locaux : "
                         "surfaces IPMS, SHAB…) — " + ", ".join(sorted({_space_label(idx, records, z) for z in zones}))
                         + ".")
    if no_exit:
        run.notes.append("F3 : aucune sortie cible (" + ref["label"] + ") sur " + ", ".join(
            f"« {names.get(s, '?')} »" + (f" (plancher à {lv:.2f} m)" if lv is not None else "") + f" ({n} local/aux)"
            for (s, lv), n in no_exit.items()) + " — locaux non contrôlés.")
    if "F6" in ids and not eas_set:
        run.notes.append("F6 non contrôlée : espaces d'attente sécurisés à désigner (eas), ou locaux du compartiment "
                         "voisin en cas de transfert horizontal.")
    summary["groupes_de_sorties"] = dict(summary["groupes_de_sorties"])
    summary["escaliers_proteges"] = dict(summary["escaliers_proteges"])
    return summary


def _straight(idx, g, groups, lvl_groups):
    """Vol d'oiseau et orthogonale du point le plus éloigné du local aux portes cibles."""
    T = [_center2(idx, d) for k in groups for d in lvl_groups[k]["doors"]] + \
        [_center2(idx, s) for k in groups for s in lvl_groups[k]["spaces"]]
    if not T:
        return None, None
    T = np.array(T)
    e = idx.elements[g]
    v, _ = idx.mesh(g)
    pts = np.unique(np.round(v[:, :2], 3), axis=0)
    d_e = np.linalg.norm(pts[:, None, :] - T[None], axis=2).min(axis=1)
    ax = np.asarray(e["obb"]["axes"])[:2, :2]
    d_o = np.abs((pts[:, None, :] - T[None]) @ ax.T).sum(axis=2).min(axis=1)
    return float(d_e.max()), float(d_o.max())


def _f3_level(run, idx, records, paths, sps, targets, subset, lvl_groups, cfg, seuil, kind, ref, referentiel, origin,
              landing_set, doors, cds_max, ids, method, unlinked, summary):
    """F3 et F5 pour un niveau de plancher."""
    prot_known = all(lvl_groups[k].get("protege") is not None for k in targets if lvl_groups[k]["kind"] == "escalier")
    prot = [k for k in targets if lvl_groups[k].get("protege")]
    origins: list[tuple[str, str]] = []  # (élément, type) : local ou porte palière
    if origin == "porte_paliere":
        origins = [(d, "porte") for d in doors if d in landing_set]
    else:
        origins = [(g, "local") for g in sps if g not in paths.zones]
    for g, otype in origins:
        if "F3" in ids:
            run.seen("F3", g)
        if otype == "local":
            r = paths.space_path(g, groups=targets)
            far_e, far_o = _straight(idx, g, targets, lvl_groups)
            if r is None:
                unlinked.append((g, far_e or 0.0))
                continue
            if r["non_relie"] > 0.05 and "F3" in ids:
                run.add("F3", g, f"{r['non_relie']:.0%} de la surface du local n'est reliée à aucune sortie par les "
                        "locaux et les portes de l'étage (porte manquante, local non jointif ?)",
                        status="incoherent", severity="info", actual=round(r["non_relie"], 2))
            q = r["cellule_porte"]
        else:
            r = paths.door_path(g, groups=targets)
            far_e = far_o = None
            if r is None:
                if "F3" in ids:
                    run.add("F3", g, "Porte palière reliée à aucune sortie cible par les circulations de l'étage",
                            status="incoherent", severity="info")
                continue
            q = r["cellule"]
        L, mode = paths.dead_end(*q, groups=targets)
        in_cds = mode == "issue unique" or L > DEAD_END_TOL_M
        if in_cds:
            summary["culs_de_sac"] += 1
        dist = r["distance"] if method == "cheminement" else (far_e if far_e is not None else r["distance"])
        # seuil applicable
        verdict, limit, why = "ok", seuil, cfg["source"]
        if seuil is None:
            verdict = "non_controle"
        elif referentiel == "ERP" and kind == "etage":
            any_ok = dist <= cfg["non_protege"] + 1e-6
            lim_p = cfg["cul_de_sac"] if in_cds else cfg["seuil"]
            if any_ok:
                limit, why = cfg["non_protege"], "CO 49 (tout escalier)"
            elif prot_known:
                rp = (paths.space_path(g, groups=prot) if otype == "local" else paths.door_path(g, groups=prot)) \
                    if prot else None
                dp = rp["distance"] if rp else None
                limit = lim_p
                why = "CO 49, escalier protégé" + (", cul-de-sac" if in_cds else "")
                verdict = "ok" if dp is not None and dp <= lim_p + 1e-6 else "ko"
                if dp is not None:
                    dist = dp
                    r = rp
            elif in_cds or dist > cfg["seuil"] + 1e-6:
                verdict, limit = "ko", lim_p
                why = "CO 49" + (", cul-de-sac" if in_cds else "")
            else:
                verdict, limit = "nc", cfg["seuil"]
                why = "CO 49 : conforme seulement si l'escalier est protégé (protected_stairs)"
        else:
            if in_cds and cfg.get("cul_de_sac") is not None:
                limit, why = cfg["cul_de_sac"], cfg["source"] + (" (pas de choix entre deux sorties)"
                                                                 if kind == "acces" else ", cul-de-sac")
            verdict = "ok" if dist <= limit + 1e-6 else "ko"
        meas = {"value": round(dist, 2), "unit": "m", "cheminement_m": round(dist, 2),
                "tendu_m": round(r["tendu"], 2), "grille_m": round(r["grille"], 2),
                "cul_de_sac_m": round(L, 2) if in_cds else 0.0, "issue": mode,
                "point_le_plus_eloigne": [round(c, 2) for c in r["point"]],
                "chemin": [[round(c, 2) for c in p] for p in r["chemin"]]}
        if far_e is not None:
            meas.update(vol_oiseau_m=round(far_e, 2), orthogonal_m=round(far_o, 2))
        start = "Point le plus éloigné" if otype == "local" else "Porte palière"
        target_txt = {"escalier": "escalier", "exterieure": "sortie sur l'extérieur ou dégagement protégé",
                      "toutes": "sortie"}[cfg["cible"]]
        if "F3" in ids and verdict == "ko":
            run.add("F3", g, f"{start} à {dist:.1f} m ({method_txt(method)}) de l'{target_txt} le plus proche"
                    f"{' — cul-de-sac de ' + format(L, '.1f') + ' m' if in_cds and mode != 'issue unique' else ''}"
                    f"{' — une seule issue sur ce niveau' if mode == 'issue unique' else ''} : plus de {limit:g} m "
                    f"({why})", status="sous_seuil", actual=round(dist, 2), expected=f"≤ {limit:g} m",
                    measure=meas, threshold={"value": limit, "unit": "m", "source": f"{ref['label']} — {why}"})
        elif "F3" in ids and verdict == "nc":
            summary["non_concluants"] += 1
            run.add("F3", g, f"Non concluant : {start.lower()} à {dist:.1f} m de l'escalier le plus proche, entre 30 et "
                    f"40 m — conforme seulement si cet escalier est protégé (protected_stairs)", status="exclu",
                    severity="info", actual=round(dist, 2), measure=meas,
                    threshold={"value": limit, "unit": "m", "source": f"{ref['label']} — {why}"})
        # F5 : cul-de-sac
        if "F5" in ids and cds_max is not None and in_cds:
            run.seen("F5", g)
            if L > cds_max + 1e-6:
                where = ("une seule issue sur ce niveau" if mode == "issue unique" else
                         "avant d'avoir le choix entre deux sorties distinctes")
                run.add("F5", g, f"{'Local desservi' if otype == 'local' else 'Porte palière desservie'} par un cul-de-sac de "
                        f"{L:.1f} m ({where}) : plus de {cds_max:g} m", status="sous_seuil", actual=round(L, 2),
                        expected=f"≤ {cds_max:g} m", measure={"value": round(L, 2), "unit": "m", "issue": mode},
                        threshold={"value": cds_max, "unit": "m", "source": ref.get("cds_source") or "dead_end_max_m"})
        elif "F5" in ids and cds_max is not None:
            run.seen("F5", g)


def method_txt(method: str) -> str:
    return "suivant l'axe des circulations" if method == "cheminement" else "à vol d'oiseau"


def _f6_level(run, idx, records, st, lv_key, sps, doors, lvl_groups, kind, seuil, eas_set, effectif, door_min,
              step_max, cell, named, ipms, names, eas_levels, door_sides):
    """F6 pour un niveau : chemin sans marche ni porte étroite vers un EAS (ou une sortie extérieure au niveau
    d'accès), distance, capacité."""
    from ..geometry.paths import storey_paths

    loc = Z.SpaceLocator(idx)
    eas_here = [s for s in sps if s in eas_set]
    targets_doors = [d for grp in lvl_groups.values() if grp["kind"] == "exterieure" for d in grp["doors"]] \
        if kind == "acces" else []
    label = f"« {names.get(st, '?')} »" + (f" (plancher à {lv_key[1]:.2f} m)" if lv_key[1] is not None else "")
    if not eas_here and not targets_doors:
        run.notes.append(f"F6 : aucun espace d'attente sécurisé désigné sur {label} — niveau non contrôlé.")
        return
    passable, closed, narrow, steps, unknown = [], [], 0, 0, 0
    for d in doors:
        w = _door_width(records, d)
        if w is None:
            unknown += 1
        elif w < door_min - 1e-6:
            narrow += 1
            closed.append(d)
            continue
        sides = [sp for sp, _ in door_sides(idx, records, d, loc).values() if sp]
        if len(sides) == 2 and abs(idx.elements[sides[0]]["aabb"][2] - idx.elements[sides[1]]["aabb"][2]) > step_max:
            steps += 1
            closed.append(d)
            continue
        passable.append(d)
    stairs = [g for g, e in idx.elements.items() if e["ifc_class"] in ("IfcStair", "IfcStairFlight")
              and e["has_geometry"] and e.get("storey") == st]
    targets_doors = [d for d in targets_doors if d in passable]
    paths = storey_paths(idx, sps, passable, targets_doors, cell=cell, exit_spaces=eas_here, zone_names=named,
                         ipms=ipms, extra_blocked=stairs, step_max_m=step_max, closed_doors=closed)
    if paths is None:
        return
    run.notes.append(f"F6 {label} : {len(eas_here)} EAS, {narrow} porte(s) de moins de {door_min:.2f} m et {steps} "
                     f"seuil(s) à marche de plus de {step_max * 100:.0f} cm retirés du cheminement"
                     + (f", {unknown} porte(s) sans largeur (supposées franchissables)" if unknown else "") + ".")
    for g in sps:
        if g in eas_set or g in paths.zones:
            continue
        run.seen("F6", g)
        r = paths.space_path(g)
        if r is None:
            run.add("F6", g, "Aucun espace d'attente sécurisé (ni sortie extérieure) accessible sans marche ni porte "
                    f"étroite (< {door_min:.2f} m) depuis ce local : seulement par un escalier, un ressaut ou une porte "
                    "trop étroite", status="non_conforme", expected="EAS accessible en fauteuil roulant")
            continue
        if seuil is not None and r["distance"] > seuil + 1e-6:
            run.add("F6", g, f"Espace d'attente sécurisé le plus proche à {r['distance']:.1f} m suivant l'axe des "
                    f"circulations, sans marche ni porte étroite : plus de {seuil:g} m", status="sous_seuil",
                    actual=round(r["distance"], 2), expected=f"≤ {seuil:g} m",
                    measure={"value": round(r["distance"], 2), "unit": "m",
                             "chemin": [[round(c, 2) for c in p] for p in r["chemin"]]},
                    threshold={"value": seuil, "unit": "m", "source": "CO 43 / CO 49 (distances de F3)"})
    # capacité
    eff = effectif.get(names.get(st)) if isinstance(effectif, dict) else effectif
    if eas_here:
        area = sum(int((paths.label == paths.spaces.index(s)).sum()) for s in eas_here if s in paths.spaces) \
            * paths.cell ** 2
        eas_levels[lv_key] = area
        if eff is not None:
            places = eas_places(int(eff))
            need = places * EAS_PLACE_M2
            run.seen("F6", st)
            if area + 1e-6 < need:
                run.add("F6", st, f"Niveau {label} : {area:.1f} m² d'EAS pour un effectif de {int(eff)} personnes — il "
                        f"faut {places} places de fauteuil roulant (0,80 × 1,30 m), soit {need:.1f} m²",
                        status="sous_seuil", ifc_class="IfcBuildingStorey", name=names.get(st, ""),
                        actual=round(area, 2), expected=f"≥ {need:.2f} m²",
                        measure={"value": round(area, 2), "unit": "m²", "places": places},
                        threshold={"value": round(need, 2), "unit": "m²", "source": "2 places jusqu'à 50 personnes, "
                                   "+ 1 par tranche de 50"})
        else:
            run.notes.append(f"F6 {label} : capacité des EAS non contrôlée (effectif à fournir).")
