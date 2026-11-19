"""Contrôles par règles sur l'index géométrique et les données IFC (type Solibri).

- Structuration et intégrité IFC : S1…S11 (:func:`run_structure_rules`) ;
- Géométrie et modélisation, lot 1 : G1 (locaux), G3 (maillage ouvert), G8
  (garde-corps) (:func:`run_geometry_rules`).

Chaque règle a un identifiant stable, une méthode (Données, Bbox, Maillage) et une
sévérité par défaut. Les anomalies sont des :class:`Issue` du moteur commun : elles
se lisent avec ``get_check_issues`` et alimentent Smart Views / BCF sans changement.
Les seuils sont indicatifs et paramétrables : aide à la décision, pas attestation.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import numpy as np

from ..geometry.index import GeometryIndex
from ..model_view import Record, normalize_text, resolve_field
from ..selection import _num
from .engine import CheckResult, Issue
from .requirements import SEVERITY_ORDER


@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    method: str  # Données | Bbox | Maillage
    severity: str
    category: str
    description: str = ""
    available: bool = True


STRUCTURE_RULES = [
    Rule("S1", "Un seul site et un seul bâtiment", "Données", "bloquant", "structure",
         "Nombre d'IfcSite et d'IfcBuilding (sauf plusieurs bâtiments attendus)."),
    Rule("S2", "Élément contenu dans un étage", "Données", "majeur", "structure",
         "IfcRelContainedInSpatialStructure vers un IfcBuildingStorey (directement ou via son agrégat)."),
    Rule("S3", "Étages cohérents", "Données", "majeur", "structure",
         "Noms en double, étages vides, nom IFC différent du libellé affiché, altitudes non croissantes."),
    Rule("S4", "Élément posé sur son étage", "Bbox", "mineur", "structure",
         "Élément posé : étage d'assise (le plus haut sous la base + 0,35 m) = étage de rattachement ; élément porté "
         "(poutre, toiture, plafond, réseau) : dessus pas plus de 0,35 m sous son niveau (réseaux : synthèse mineure par "
         "étage) ; dalle : dessus près du niveau ou "
         "base posée sur son étage ; étages à moins de 0,70 m : non concluant (information)."),
    Rule("S5", "Porte / fenêtre dans une réservation", "Données", "majeur", "structure",
         "IfcRelFillsElement vers une réservation, elle-même liée à un mur hôte (IfcRelVoidsElement)."),
    Rule("S6", "Porte / fenêtre au même étage que son hôte", "Données", "majeur", "structure",
         "Étage de la porte ou fenêtre = étage du mur hôte (jointure réservation → hôte)."),
    Rule("S7", "Sens d'ouverture des portes défini", "Données", "mineur", "structure",
         "OperationType (occurrence ou type) ou PanelOperation différent de NOTDEFINED."),
    Rule("S8", "Identifiants uniques", "Données", "bloquant", "structure",
         "GlobalId unique (sinon occurrences indexées par #id et signalées) ; identifiant projet unique si une "
         "propriété est indiquée."),
    Rule("S9", "Géoréférencement", "Données", "majeur", "structure",
         "IfcMapConversion, ou latitude / longitude / altitude du site ; conforme aux valeurs attendues."),
    Rule("S10", "Élément physique avec géométrie", "Bbox", "mineur", "structure",
         "Éléments physiques sans représentation triangulable (agrégats dont les parties ont une géométrie exclus)."),
    Rule("S11", "Local dans l'emprise du bâtiment", "Bbox", "majeur", "structure",
         "Local sans aucun mur, dalle, toiture, poteau, porte ni fenêtre à moins de 0,30 m : posé hors du bâtiment "
         "(copie égarée, local à supprimer ou à déplacer)."),
]

GEOMETRY_RULES = [
    Rule("G1", "Locaux : surface et hauteur minimales", "Données", "mineur", "géométrie",
         "Surface nette et hauteur sous les seuils ; gaines (LongName GT, LT…) signalées pour information."),
    Rule("G2", "Locaux qui ne se chevauchent pas", "Maillage", "majeur", "géométrie",
         "Intersection de deux locaux au-delà du volume seuil (0,01 m³)."),
    Rule("G3", "Locaux à maillage fermé", "Maillage", "mineur", "géométrie",
         "Volume du local fermé (chaque arête partagée par deux triangles) : sinon surface et volume non fiables."),
    Rule("G4", "Objets en double", "Maillage", "bloquant", "géométrie",
         "Deux éléments de la même classe à la même place (type doublon du clash)."),
    Rule("G5", "Murs raccordés", "Bbox", "mineur", "géométrie",
         "Extrémités d'axe du mur (boîte orientée) à moins de 0,02 m d'un autre mur ou d'un poteau."),
    Rule("G6", "Mur posé sur son support", "Maillage", "mineur", "géométrie",
         "Écart entre le bas du mur et le dessus de la dalle (ou fondation, poutre, mur) située dessous."),
    Rule("G7", "Escaliers (marche, giron, échappée)", "Données + Maillage", "majeur", "géométrie",
         "Hauteur de marche, giron, 2h + g entre 0,60 et 0,64 m, échappée au-dessus des marches."),
    Rule("G8", "Garde-corps : hauteur et nom de type", "Données", "majeur", "géométrie",
         "Hauteur minimale ; dimension en mm du nom de type cohérente avec la hauteur."),
]
RULES = {r.id: r for r in STRUCTURE_RULES + GEOMETRY_RULES}

#: Classes exclues de S4 : elles traversent les étages par nature.
S4_EXCLUDED = ("IfcColumn", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight", "IfcCurtainWall",
               "IfcFooting", "IfcPile")
#: Éléments portés (pas posés sur un plancher) : seule l'erreur grossière est contrôlée, jugée sur leur **dessus** —
#: élément tout entier à plus de 0,35 m sous le niveau de son étage. Une poutre rattachée au plancher qu'elle porte pend
#: dessous (seul son dessus dit si elle est au bon étage) ; une gaine à 2,60 m dans un étage de 2,80 m n'est pas
#: « posée sur l'étage du dessus ».
S4_CARRIED = ("IfcBeam", "IfcMember", "IfcPlate", "IfcRoof", "IfcDistributionElement")


def _s4_kind(e: dict) -> str:
    """'exclu' | 'porte' (porté) | 'dalle' | 'pose' (posé sur un plancher)."""
    cls, pre = e["ifc_class"], str(e.get("predefined") or "").upper()
    if _is_a(cls, S4_EXCLUDED):
        return "exclu"
    if _is_a(cls, S4_CARRIED) or (cls.startswith("IfcSlab") and pre == "ROOF") or (
            cls.startswith("IfcCovering") and pre in ("CEILING", "ROOFING")):
        return "porte"
    if cls.startswith("IfcSlab") or (cls.startswith("IfcCovering") and pre == "FLOORING"):
        return "dalle"
    return "pose"


UNDEFINED_OPERATION = {None, "", "NOTDEFINED"}
SHAFT_PATTERN = r"^\s*(GT|LT|GAINES?|SHAFT|TRÉMIE|TREMIE)\b"


@cache
def _ancestors(cls: str) -> frozenset[str]:
    """Classes parentes d'après le schéma IFC (IFC4, sinon IFC2X3, sinon IFC4X3) ; vide si la classe est inconnue."""
    try:
        import ifcopenshell.ifcopenshell_wrapper as w
    except ImportError:  # pragma: no cover
        return frozenset()
    for schema in ("IFC4", "IFC2X3", "IFC4X3"):
        try:
            d = w.schema_by_name(schema).declaration_by_name(cls)
        except Exception:  # noqa: BLE001 — classe absente de ce schéma
            continue
        out = set()
        while d is not None:
            out.add(d.name())
            d = d.supertype()
        return frozenset(out)
    return frozenset()


def _is_a(cls: str, bases: tuple[str, ...]) -> bool:
    """Héritage d'après le schéma IFC (IfcPipeSegment ⊂ IfcDistributionElement), sinon par préfixe de nom
    (IfcWallStandardCase ⊂ IfcWall) + familles réseaux."""
    anc = _ancestors(cls)
    flow = ("IfcFlow", "IfcDistribution", "IfcEnergyConversionDevice")
    for b in bases:
        if cls == b or cls.startswith(b) or b in anc:
            return True
        if b == "IfcDistributionElement" and cls.startswith(flow):
            return True
    return False


@dataclass
class _Run:
    """Collecte des anomalies et statistiques par règle pour un contrôle."""

    rules: list[Rule]
    records: dict[str, Record]
    idx: GeometryIndex | None
    storey_label: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    applicable: Counter = field(default_factory=Counter)
    checked: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def element_info(self, uuid: str) -> tuple[str, str, str]:
        rec = self.records.get(uuid)
        e = (self.idx.elements.get(uuid) if self.idx else None) or {}
        cls = (rec.ifc_class if rec else None) or e.get("ifc_class") or ""
        name = (rec.name if rec else "") or e.get("name") or ""
        storey = (rec.storey if rec else "") or self.storey_label.get(e.get("storey") or "", "")
        return cls, name, storey

    def seen(self, rule_id: str, uuid: str | None = None, n: int = 1) -> None:
        self.applicable[rule_id] += n
        if uuid:
            self.checked.add(uuid)

    def add(self, rule_id: str, uuid: str, message: str, *, status: str = "non_conforme", severity: str | None = None,
            actual: Any = None, expected: str = "", measure: dict | None = None, threshold: dict | None = None,
            elements: list[dict] | None = None, bbox: list[float] | None = None, ifc_class: str | None = None,
            name: str | None = None) -> None:
        rule = RULES[rule_id]
        cls, nm, storey = self.element_info(uuid) if uuid else ("", "", "")
        e = (self.idx.elements.get(uuid) if (self.idx and uuid) else None) or {}
        center = None
        if e.get("aabb"):
            b = e["aabb"]
            center = [round((b[i] + b[i + 3]) / 2, 3) for i in range(3)]
        rec = self.records.get(uuid)
        self.issues.append(Issue(
            issue_id="", req_id=rule_id, req_label=rule.label, category=rule.category,
            severity=severity or rule.severity, uuid=uuid or "", ifc_class=ifc_class or cls, name=name or nm,
            storey=storey, field="", location="", status=status, actual=actual, expected=expected, message=message,
            writable=False, rule_id=rule_id, elements=elements or ([{"uuid": uuid, "role": "élément"}] if uuid else []),
            measure=measure, threshold=threshold, where={"storey": storey, "space": rec.space if rec else "",
                                                         "center": center},
            bbox=bbox or e.get("aabb"), method=rule.method,
        ))

    def result(self) -> CheckResult:
        res = CheckResult(n_elements=len(self.checked), n_requirements=len(self.rules), n_checks=0)
        self.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), _rule_order(i.req_id), i.ifc_class, i.name))
        for k, iss in enumerate(self.issues, start=1):
            iss.issue_id = f"I{k:06d}"
        res.issues = self.issues
        nc: Counter = Counter()
        for i in self.issues:  # une ligne de synthèse compte pour les éléments qu'elle regroupe
            if i.status != "exclu":
                nc[i.req_id] += int((i.measure or {}).get("n_elements", 1)) if isinstance(i.measure, dict) else 1
        for r in self.rules:
            n = self.applicable.get(r.id, 0)
            bad = min(nc.get(r.id, 0), n) if n else nc.get(r.id, 0)
            res.per_requirement.append({
                "req_id": r.id, "label": r.label, "field": r.method, "ifc_classes": r.description,
                "severity": r.severity, "category": r.category, "applicable": n, "conformes": max(n - bad, 0),
                "non_conformes": nc.get(r.id, 0), "taux": round((n - bad) / n, 4) if n else None,
                "optionnels_absents": 0,
            })
        res.n_checks = sum(self.applicable.values())
        res.checked_uuids = sorted(self.checked)
        return res


def _rule_order(rule_id: str) -> tuple:
    m = re.match(r"([A-Z]+)(\d+)", rule_id or "")
    return (m.group(1), int(m.group(2))) if m else (rule_id, 0)


def _round(x, n=3):
    return None if x is None else round(float(x), n)


# ── Structuration et intégrité IFC ──────────────────────────────────────


def _dms_to_deg(v) -> float | None:
    if v in (None, ()):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    parts = list(v) + [0] * (4 - len(v))
    sign = -1 if any(p < 0 for p in parts[:4]) else 1
    d, m, s, us = (abs(p) for p in parts[:4])
    return sign * (d + m / 60 + s / 3600 + us / 3.6e9)


def _structure_s1(run: _Run, idx: GeometryIndex, expected_buildings: int) -> None:
    ifc = idx.ifc
    sites = ifc.get("sites") or []
    run.seen("S1", n=2)
    if len(sites) != 1:
        run.add("S1", sites[1]["guid"] if len(sites) > 1 else "", f"{len(sites)} IfcSite dans la maquette (1 attendu)",
                actual=len(sites), expected="1 site", ifc_class="IfcSite", name=", ".join(s["name"] or "" for s in sites),
                measure={"value": len(sites), "unit": "site"}, threshold={"value": 1, "unit": "site", "source": "règle"})
    blds = ifc.get("buildings") or []
    if len(blds) != expected_buildings:
        run.add("S1", blds[0]["guid"] if blds else "",
                f"{len(blds)} IfcBuilding dans la maquette ({expected_buildings} attendu)", actual=len(blds),
                expected=f"{expected_buildings} bâtiment(s)", ifc_class="IfcBuilding",
                name=", ".join(b["name"] or "(sans nom)" for b in blds),
                measure={"value": len(blds), "unit": "bâtiment"},
                threshold={"value": expected_buildings, "unit": "bâtiment", "source": "paramètre expected_buildings"})


def _structure_s3(run: _Run, idx: GeometryIndex, displayed: dict[str, str]) -> None:
    storeys = idx.ifc.get("storeys") or []
    resolved = Counter(e["storey"] for e in idx.elements.values() if e.get("storey"))
    by_name: dict[tuple, list[dict]] = defaultdict(list)
    for s in storeys:
        run.seen("S3", s["guid"])
        by_name[(s.get("building"), normalize_text(s["name"]))].append(s)
    for _key, group in by_name.items():
        if len(group) > 1:
            for s in group:
                run.add("S3", s["guid"], f"Nom d'étage en double : « {s['name']} » ({len(group)} étages)",
                        actual=s["name"], expected="nom unique par bâtiment", ifc_class="IfcBuildingStorey",
                        name=s["name"])
    for s in storeys:
        n = max(s.get("n_contained") or 0, resolved.get(s["guid"], 0))
        if n == 0:
            run.add("S3", s["guid"], f"Étage « {s['name']} » vide : aucun élément contenu", actual=0,
                    expected="au moins un élément", ifc_class="IfcBuildingStorey", name=s["name"],
                    measure={"value": 0, "unit": "élément"})
        label = displayed.get(s["guid"])
        if label is not None and normalize_text(label) != normalize_text(s["name"]):
            run.add("S3", s["guid"],
                    f"Nom IFC « {s['name']} » différent du libellé affiché « {label} » "
                    f"({n} élément(s) rattaché(s) sous « {s['name']} ») : un filtre sur « {label} » ne les trouve pas",
                    status="incoherent", actual=s["name"], expected=f"« {label} »", ifc_class="IfcBuildingStorey",
                    name=s["name"])
        if s.get("elevation") is not None and s.get("z") is not None:
            site_z = _site_z(idx)
            if site_z is not None and abs((s["z"] - site_z) - s["elevation"]) > 0.05:
                run.add("S3", s["guid"],
                        f"Altitude déclarée {s['elevation']:.3f} m ≠ altitude du placement {s['z'] - site_z:.3f} m",
                        status="incoherent", severity="mineur", actual=_round(s["elevation"]),
                        expected=f"{s['z'] - site_z:.3f} m", ifc_class="IfcBuildingStorey", name=s["name"],
                        measure={"value": _round(s["elevation"] - (s["z"] - site_z)), "unit": "m"},
                        threshold={"value": 0.05, "unit": "m", "source": "règle"})
    # Altitudes non croissantes : l'ordre des étages par Elevation déclarée doit être celui de leur placement réel
    # (l'ordre d'agrégation IfcRelAggregates est un SET au sens IFC : il ne fait pas foi).
    by_b: dict[str | None, list[dict]] = defaultdict(list)
    for s in storeys:
        if s.get("z") is not None and s.get("elevation") is not None:
            by_b[s.get("building")].append(s)
    for group in by_b.values():
        group.sort(key=lambda s: (s["elevation"], s["z"]))
        for prev, cur in zip(group, group[1:], strict=False):
            if cur["z"] < prev["z"] - 0.05:
                run.add("S3", cur["guid"], f"Altitudes non croissantes : « {cur['name']} » déclaré au-dessus de "
                        f"« {prev['name']} » (Elevation {cur['elevation']:.2f} > {prev['elevation']:.2f} m) mais placé "
                        f"plus bas ({cur['z']:.2f} < {prev['z']:.2f} m)", status="incoherent",
                        ifc_class="IfcBuildingStorey", name=cur["name"], actual=_round(cur["z"], 2),
                        expected=f"> {prev['z']:.2f} m",
                        elements=[{"uuid": cur["guid"], "role": "étage"}, {"uuid": prev["guid"], "role": "étage"}])
        by_z = sorted(group, key=lambda s: s["z"])
        for prev, cur in zip(by_z, by_z[1:], strict=False):
            if abs(cur["z"] - prev["z"]) < 1e-3:
                run.add("S3", cur["guid"], f"Même altitude que « {prev['name']} » ({cur['z']:.2f} m)",
                        status="incoherent", ifc_class="IfcBuildingStorey", name=cur["name"],
                        elements=[{"uuid": cur["guid"], "role": "étage"}, {"uuid": prev["guid"], "role": "étage"}])


def _site_z(idx: GeometryIndex) -> float | None:
    """Altitude 0 des étages : l'étage d'élévation 0 s'il existe, sinon la médiane z − élévation."""
    vals = [s["z"] - s["elevation"] for s in idx.ifc.get("storeys") or []
            if s.get("z") is not None and s.get("elevation") is not None]
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


S4_DEFAULTS = {"base_tolerance_m": 0.35, "ambiguous_gap_m": 0.70, "slab_top_below_m": 0.35, "slab_top_above_m": 0.10,
               "carried_tolerance_m": 0.35}
#: Réseaux (IfcDistributionElement) sous le niveau de leur étage : Revit les rattache souvent à l'étage du dessus
#: (plafond du sous-sol rattaché au RDC). Signalés en mineur, une ligne de synthèse par étage.
S4_NETWORKS = ("IfcDistributionElement",)


def _union_aabb(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [min(b[i] for b in boxes) for i in range(3)] + [max(b[i] for b in boxes) for i in range(3, 6)]


#: Suite en pente régulière (S4) : au moins 5 éléments du même type, dessus en progression régulière.
RAMP_MIN_ELEMENTS = 5
RAMP_STEP_CV = 0.15


def _s4_ramp_hint(run: _Run, idx: GeometryIndex) -> None:
    """Oriente le modeleur : éléments S4 d'un même type et d'un même étage dont les dessus montent d'un pas régulier
    le long d'une direction (dalles préfabriquées d'une rampe, gradins) — « rampe probable », à modéliser en IfcRamp."""
    groups: dict[tuple, list[Issue]] = defaultdict(list)
    for i in run.issues:
        e = idx.elements.get(i.uuid) if i.req_id == "S4" and i.status == "non_conforme" else None
        if e and e.get("aabb") and e["ifc_class"] != "IfcBuildingStorey":
            groups[(e.get("storey"), (e.get("name") or "").rsplit(":", 1)[0], e["ifc_class"])].append(i)
    for (_, tname, _), iss in groups.items():
        if len(iss) < RAMP_MIN_ELEMENTS:
            continue
        b = np.array([idx.elements[i.uuid]["aabb"] for i in iss], float)
        xy = (b[:, :2] + b[:, 3:5]) / 2
        top = b[:, 5]
        d = xy - xy.mean(axis=0)
        axis = np.linalg.svd(d, full_matrices=False)[2][0]  # direction principale de la suite
        order = np.argsort(d @ axis)
        dz = np.diff(top[order])
        dh = np.diff((d @ axis)[order])
        if not len(dz) or np.any(dh < 1e-3):
            continue
        step = float(np.mean(np.abs(dz)))
        if step < 0.02 or not (np.all(dz > 0) or np.all(dz < 0)) or np.std(np.abs(dz)) > RAMP_STEP_CV * step:
            continue
        slope = 100 * step / float(np.mean(dh))
        hint = (f" — suite de {len(iss)} « {tname} » en pente régulière (pas de {step:.2f} m, pente ≈ {slope:.1f} %, "
                f"dessus de {top.min():.2f} à {top.max():.2f} m) : rampe probable, à modéliser en IfcRamp "
                "(classe exclue de S4) ou à rattacher à un seul étage")
        for i in iss:
            i.message += hint


def _storey_stacks(idx: GeometryIndex) -> dict[str | None, list[dict]]:
    """Étages de chaque bâtiment, triés par altitude réelle (placement)."""
    by_b: dict[str | None, list[dict]] = defaultdict(list)
    for s in idx.ifc.get("storeys") or []:
        if s.get("z") is not None:
            by_b[s.get("building")].append(s)
    for g in by_b.values():
        g.sort(key=lambda s: s["z"])
    return by_b


def _seated(stack: list[dict], z: float, tol: float) -> dict | None:
    """Étage le plus haut dont le niveau est sous ``z`` + ``tol`` (None : sous l'étage le plus bas)."""
    cand = [s for s in stack if s["z"] <= z + tol + 1e-9]
    return cand[-1] if cand else None


def _structure_s4(run: _Run, idx: GeometryIndex, uuids: set[str], th: dict | None) -> None:
    """Élément posé sur un autre étage que le sien, raisonné sur la base de l'élément (pas sur une tranche d'étage :
    correct aussi pour les bâtiments à étages décalés ou à volumes de hauteurs différentes).

    - étage d'assise = étage le plus haut dont le niveau est sous la base + 0,35 m ;
    - assise = son étage : conforme ; un autre étage : anomalie (le message nomme l'étage réel) ;
    - sous l'étage le plus bas (fondations) : conforme si l'élément est rattaché à cet étage le plus bas ;
    - éléments portés (poutres, toitures, plafonds, réseaux) : jugés sur leur dessus — anomalie s'ils sont tout entiers
      à plus de ``carried_tolerance_m`` (0,35 m) sous le niveau de leur étage ; les réseaux sont regroupés en une ligne
      mineure par étage ;
    - dalles : dessus entre le niveau − 0,35 m et le niveau + 0,10 m ;
    - étages à moins de 0,70 m l'un de l'autre : élément ambigu, « non concluant » (information).
    """
    t = {**S4_DEFAULTS, **(th or {})}
    stacks = _storey_stacks(idx)
    by_guid = {s["guid"]: (b, s) for b, st in stacks.items() for s in st}
    tol, amb = t["base_tolerance_m"], t["ambiguous_gap_m"]
    thr = {"value": tol, "unit": "m", "source": "s4_thresholds.base_tolerance_m"}
    ctol = t["carried_tolerance_m"]
    cthr = {"value": ctol, "unit": "m", "source": "s4_thresholds.carried_tolerance_m"}
    low_networks: dict[str, list[tuple[str, float]]] = defaultdict(list)  # réseaux sous le niveau de leur étage
    shifted: dict[str, list[float]] = defaultdict(list)  # écarts des éléments posés hors de leur étage, par étage
    posed: Counter = Counter()
    for g in uuids:
        e = idx.elements.get(g)
        if not e or e["ifc_class"] == "IfcSpace" or not e.get("aabb") or e.get("storey") not in by_guid:
            continue
        kind = _s4_kind(e)
        if kind == "exclu" or e.get("parent"):
            continue
        b, own = by_guid[e["storey"]]
        stack = stacks[b]
        run.seen("S4", g)
        base = e["aabb"][2]
        if kind == "porte":  # porté : jugé sur son dessus, seule la descente entière sous son niveau est une erreur
            top = e["aabb"][5]
            if top >= own["z"] - ctol - 1e-9 or own["guid"] == stack[0]["guid"]:
                continue
            if _is_a(e["ifc_class"], S4_NETWORKS):
                low_networks[own["guid"]].append((g, top - own["z"]))
                continue
            real = _seated(stack, top, ctol)
            where = f" : il se trouve au niveau de « {real['name']} »" if real is not None else ""
            run.add("S4", g, f"Élément porté rattaché à « {own['name']} » mais tout entier sous son niveau (dessus à "
                    f"{top - own['z']:+.2f} m){where}", actual=_round(top - own["z"], 3),
                    expected=f"dessus à {ctol} m au plus sous le niveau de son étage",
                    measure={"value": _round(top - own["z"], 3), "unit": "m"}, threshold=cthr)
            continue
        if kind == "dalle":
            top = e["aabb"][5]
            lo, hi = own["z"] - t["slab_top_below_m"], own["z"] + t["slab_top_above_m"]
            if lo - 1e-9 <= top <= hi + 1e-9:
                continue  # dalle de structure au niveau
            seated_base = _seated(stack, base, tol)
            if seated_base is not None and seated_base["guid"] == own["guid"]:
                continue  # posée sur son propre étage (estrade, jardinière, claustra…)
            if seated_base is None and own["guid"] == stack[0]["guid"] and top <= hi + 1e-9:
                continue  # fondations sous l'étage le plus bas, rattachées à cet étage
            real = _seated(stack, top, t["slab_top_below_m"])
            if real is not None and real["guid"] != own["guid"] and abs(real["z"] - own["z"]) < amb:
                run.add("S4", g, f"Non concluant : dessus de dalle à {top - own['z']:+.2f} m du niveau de « {own['name']} », "
                        f"étages « {own['name']} » et « {real['name']} » à moins de {amb} m", status="exclu",
                        severity="info", actual=_round(top - own["z"], 3))
                continue
            if real is None:
                where = "elle se trouve sous l'étage le plus bas"
            elif real["guid"] == own["guid"]:
                where = (f"hors de la tolérance de son propre niveau (+{t['slab_top_above_m']:.2f} m) : dalle surélevée "
                         "ou mal rattachée")
            else:
                where = f"elle se trouve au niveau de « {real['name']} »"
            run.add("S4", g, f"Dalle rattachée à « {own['name']} » mais dessus à {top - own['z']:+.2f} m de son niveau : "
                    f"{where}", actual=_round(top - own["z"], 3),
                    expected=f"dessus entre {-t['slab_top_below_m']:.2f} et +{t['slab_top_above_m']:.2f} m du niveau",
                    measure={"value": _round(top - own["z"], 3), "unit": "m"},
                    threshold={"value": f"−{t['slab_top_below_m']} / +{t['slab_top_above_m']}", "unit": "m",
                               "source": "s4_thresholds.slab_top_below_m / slab_top_above_m"})
            continue
        posed[own["guid"]] += 1
        real = _seated(stack, base, tol)
        if real is None:  # sous l'étage le plus bas : fondations
            if own["guid"] == stack[0]["guid"]:
                continue
            run.add("S4", g, f"Rattaché à « {own['name']} » mais posé sous l'étage le plus bas « {stack[0]['name']} » "
                    f"(base à {base - own['z']:+.2f} m de son niveau)", actual=_round(base - own["z"], 3),
                    expected="posé sur son étage", measure={"value": _round(base - own["z"], 3), "unit": "m"},
                    threshold=thr)
            continue
        if real["guid"] == own["guid"]:
            continue
        if abs(real["z"] - own["z"]) < amb:
            run.add("S4", g, f"Non concluant : base à {base - own['z']:+.2f} m du niveau de « {own['name']} », étages "
                    f"« {own['name']} » et « {real['name']} » à moins de {amb} m l'un de l'autre", status="exclu",
                    severity="info", actual=_round(base - own["z"], 3),
                    threshold={"value": amb, "unit": "m", "source": "s4_thresholds.ambiguous_gap_m"})
            continue
        run.add("S4", g, f"Rattaché à « {own['name']} » mais posé sur « {real['name']} » (base à "
                f"{base - own['z']:+.2f} m du niveau de son étage)", actual=_round(base - own["z"], 3),
                expected=f"base au-dessus du niveau de son étage − {tol} m et sous le niveau de l'étage suivant",
                measure={"value": _round(base - own["z"], 3), "unit": "m"}, threshold=thr)
        shifted[own["guid"]].append(base - own["z"])
    # Synthèse par étage : beaucoup d'éléments posés au même écart = niveau d'étage décalé (ou étage qui regroupe des
    # planchers de hauteurs différentes) — un message pour le modeleur, en plus des anomalies élément par élément.
    for sg, offs in shifted.items():
        n_pose = posed.get(sg, 0)
        if len(offs) < 10 or len(offs) < 0.3 * n_pose:
            continue
        s_ = by_guid[sg][1]
        med = float(np.median(offs))
        q1, q3 = (float(x) for x in np.percentile(offs, [25, 75]))
        run.add("S4", sg, f"Étage « {s_['name']} » : {len(offs)} élément(s) posé(s) sur {n_pose} ({len(offs) / n_pose:.0%}) "
                f"sont à {q1:+.2f} à {q3:+.2f} m (médiane {med:+.2f} m) de son niveau — niveau d'étage probablement "
                "décalé, ou étage qui regroupe des planchers de hauteurs différentes", status="incoherent",
                severity="majeur", ifc_class="IfcBuildingStorey", name=s_["name"], actual=_round(med, 2),
                expected="éléments posés sur le niveau de leur étage",
                measure={"value": _round(med, 3), "unit": "m", "elements": len(offs), "sur": n_pose})
    _s4_ramp_hint(run, idx)
    # Réseaux tout entiers sous le niveau de leur étage : une ligne mineure par étage (rattachement Revit fréquent au
    # niveau du dessus), avec la liste des éléments pour les isoler dans la visionneuse.
    for sg, items in low_networks.items():
        s_ = by_guid[sg][1]
        stack = stacks[by_guid[sg][0]]
        offs = [o for _, o in items]
        med = float(np.median(offs))
        real = _seated(stack, s_["z"] + med, ctol)
        where = f", au niveau de « {real['name']} »" if real is not None and real["guid"] != sg else ""
        run.add("S4", sg, f"Étage « {s_['name']} » : {len(items)} élément(s) de réseau rattaché(s) à cet étage mais tout "
                f"entiers sous son niveau (dessus de {min(offs):+.2f} à {max(offs):+.2f} m, médiane {med:+.2f} m){where} — "
                "rattachement fréquent dans Revit (réseaux du plafond rattachés à l'étage du dessus), à vérifier",
                severity="mineur", ifc_class="IfcBuildingStorey", name=s_["name"], actual=_round(med, 2),
                expected=f"dessus à {ctol} m au plus sous le niveau de son étage",
                measure={"value": _round(med, 3), "unit": "m", "elements": len(items)}, threshold=cthr,
                elements=[{"uuid": g, "role": "réseau"} for g, _ in items],
                bbox=_union_aabb([idx.elements[g]["aabb"] for g, _ in items]))


def _structure_doors(run: _Run, idx: GeometryIndex, uuids: set[str], direction_property: str | None) -> None:
    fills, voids, doors = idx.ifc.get("fills") or {}, idx.ifc.get("voids") or {}, idx.ifc.get("doors") or {}
    names = idx.storey_names()
    s7_groups: dict[tuple, list[str]] = defaultdict(list)
    for g in uuids:
        e = idx.elements.get(g)
        if not e or not (e["ifc_class"].startswith("IfcDoor") or e["ifc_class"].startswith("IfcWindow")):
            continue
        run.seen("S5", g)
        opening = fills.get(g)
        host = voids.get(opening) if opening else None
        if not opening:
            run.add("S5", g, "Orpheline : aucune réservation (IfcRelFillsElement absent)",
                    expected="porte ou fenêtre posée dans une réservation")
        elif not host:
            run.add("S5", g, "Réservation sans mur hôte (IfcRelVoidsElement absent)",
                    expected="réservation liée à un mur ou une dalle",
                    elements=[{"uuid": g, "role": "porte/fenêtre"}, {"uuid": opening, "role": "réservation"}])
        if host:
            run.seen("S6", g)
            he = idx.elements.get(host) or {}
            if he.get("storey") and e.get("storey") and he["storey"] != e["storey"]:
                run.add("S6", g, f"Rattachée à « {names.get(e['storey'], '?')} », son hôte "
                        f"« {he.get('name') or host} » à « {names.get(he['storey'], '?')} »",
                        actual=names.get(e["storey"]), expected=names.get(he["storey"]),
                        elements=[{"uuid": g, "role": "porte/fenêtre"}, {"uuid": host, "role": "hôte"}])
        if g in doors:
            run.seen("S7", g)
            op = doors[g]
            defined = (op.get("occurrence") not in UNDEFINED_OPERATION or op.get("type") not in UNDEFINED_OPERATION
                       or any(p not in UNDEFINED_OPERATION for p in op.get("panels") or []))
            if not defined and direction_property:
                rec = run.records.get(g)
                defined = bool(rec and resolve_field(rec, direction_property).found)
            if not defined:
                # Distinguer « attribut absent » (rien à lire) et « attribut à NOTDEFINED » (présent, non renseigné).
                found = [(where, v) for where, v in (("OperationType de l'occurrence", op.get("occurrence")),
                                                      ("OperationType du type", op.get("type")))
                         if v not in (None, "")]
                found += [("PanelOperation", p) for p in op.get("panels") or [] if p not in (None, "")]
                detail = ", ".join(f"{w} = {v}" for w, v in found)
                tname = (idx.elements[g].get("name") or "").rsplit(":", 1)[0] if idx.elements.get(g) else ""
                s7_groups[(tname, bool(found), detail)].append(g)
    # Une ligne par type de porte (même cause pour toutes ses occurrences), avec la liste des portes.
    for (tname, notdef, detail), gs in sorted(s7_groups.items(), key=lambda kv: -len(kv[1])):
        what = (f"attribut à NOTDEFINED ({detail})" if notdef else
                "ni OperationType (occurrence ou type) ni PanelOperation")
        run.add("S7", gs[0], f"Type « {tname or '(sans nom)'} » : sens d'ouverture "
                f"{'non renseigné' if notdef else 'absent'} sur {len(gs)} porte(s) — {what}",
                status="vide" if notdef else "absent", actual="NOTDEFINED" if notdef else "absent",
                expected="OperationType (ex. SINGLE_SWING_LEFT) ou PanelOperation renseigné, sur le type de préférence",
                name=tname, measure={"value": len(gs), "unit": "portes", "n_elements": len(gs)},
                elements=[{"uuid": x, "role": "porte"} for x in gs])


def _structure_s8(run: _Run, idx: GeometryIndex, uuids: set[str], unique_property: str | None) -> None:
    dup = idx.ifc.get("duplicate_guids") or {}
    occ = idx.ifc.get("duplicate_occurrences") or {}
    run.seen("S8", n=len(uuids))
    for g, n in dup.items():
        items = occ.get(g) or []
        desc = ", ".join(f"#{o['step_id']} {o['ifc_class']} « {o['name']} »" for o in items[:6])
        first = items[0] if items else {}
        run.add("S8", g, f"GlobalId présent {n} fois dans le fichier IFC ({desc}) : sélections, corrections et "
                f"sujets BCF ne savent pas quel élément viser — chaque occurrence est indexée à part (#id)",
                actual=n, expected="unique", measure={"value": n, "unit": "occurrence"},
                ifc_class=first.get("ifc_class"), name=first.get("name"),
                elements=[{"uuid": o["key"], "role": f"occurrence #{o['step_id']}", "step_id": o["step_id"]}
                          for o in items])
    if unique_property:
        vals: dict[str, list[str]] = defaultdict(list)
        missing = 0
        for g in uuids:
            rec = run.records.get(g)
            if not rec or not rec.is_physical:
                continue
            fv = resolve_field(rec, unique_property)
            if fv.found:
                vals[normalize_text(fv.value)].append(g)
            else:
                missing += 1
        for v, group in vals.items():
            if len(group) > 1:
                for g in group:
                    run.add("S8", g, f"{unique_property} « {v} » porté par {len(group)} éléments", actual=v,
                            expected="identifiant unique",
                            elements=[{"uuid": x, "role": "même identifiant"} for x in group[:20]])
        if missing:
            run.notes.append(f"S8 : {missing} élément(s) sans {unique_property} (non contrôlés pour l'unicité).")


def _structure_s9(run: _Run, idx: GeometryIndex, expected: dict | None) -> None:
    ifc = idx.ifc
    mc = ifc.get("map_conversion")
    site = (ifc.get("sites") or [{}])[0]
    run.seen("S9", n=1)
    lat, lon = _dms_to_deg(site.get("lat")), _dms_to_deg(site.get("lon"))
    has_latlon = lat is not None and lon is not None and not (lat == 0 and lon == 0)
    sguid = site.get("guid", "")
    if not mc and not has_latlon:
        run.add("S9", sguid, "Aucun géoréférencement : ni IfcMapConversion, ni latitude / longitude du site",
                ifc_class="IfcSite", expected="IfcMapConversion (IFC4) ou RefLatitude / RefLongitude")
        return
    if not mc:
        run.notes.append("S9 : pas d'IfcMapConversion (" + (ifc.get("schema") or "") + ") — géoréférencement par "
                         "latitude / longitude du site uniquement.")
    if not expected:
        return
    tol = float(expected.get("tolerance_m") or 1.0)
    epsg = expected.get("epsg")
    if epsg and mc:
        crs = str(mc.get("crs") or "")
        if str(epsg).lower().replace("epsg:", "") not in crs.lower().replace("epsg:", ""):
            run.add("S9", sguid, f"Système de coordonnées « {crs or 'absent'} » ≠ attendu EPSG:{epsg}",
                    ifc_class="IfcSite", actual=crs, expected=f"EPSG:{epsg}")
    elif epsg and not mc:
        run.add("S9", sguid, f"EPSG:{epsg} attendu mais aucun IfcMapConversion", ifc_class="IfcSite",
                expected=f"IfcMapConversion vers EPSG:{epsg}")
    for key, label in (("eastings", "Abscisse (Eastings)"), ("northings", "Ordonnée (Northings)"),
                       ("orthogonal_height", "Altitude")):
        if expected.get(key) is not None and mc:
            got = mc.get(key)
            if got is None or abs(got - float(expected[key])) > tol:
                run.add("S9", sguid, f"{label} {got} ≠ attendu {expected[key]} (± {tol} m)", ifc_class="IfcSite",
                        actual=got, expected=str(expected[key]),
                        measure={"value": None if got is None else _round(got - float(expected[key])), "unit": "m"},
                        threshold={"value": tol, "unit": "m", "source": "georef_expected.tolerance_m"})
    for key, val in (("latitude", lat), ("longitude", lon)):
        if expected.get(key) is not None:
            exp = float(expected[key])
            err_m = None if val is None else abs(val - exp) * 111_320 * (math.cos(math.radians(exp)) if key ==
                                                                          "longitude" else 1)
            if err_m is None or err_m > tol:
                run.add("S9", sguid, f"{key.capitalize()} du site {val if val is None else round(val, 6)} ≠ attendue "
                        f"{exp} (écart ≈ {err_m if err_m is None else round(err_m, 1)} m)", ifc_class="IfcSite",
                        actual=val, expected=str(exp), measure={"value": _round(err_m, 1), "unit": "m"},
                        threshold={"value": tol, "unit": "m", "source": "georef_expected.tolerance_m"})


#: Éléments qui matérialisent le bâtiment autour d'un local (S11).
BUILDING_FABRIC = ("IfcWall", "IfcSlab", "IfcRoof", "IfcCurtainWall", "IfcColumn", "IfcCovering", "IfcDoor",
                   "IfcWindow", "IfcRailing", "IfcStair", "IfcBeam", "IfcPlate", "IfcMember")
S11_GAP_M = 0.30


def _structure_s11(run: _Run, idx: GeometryIndex, uuids: set[str]) -> None:
    """Local hors de l'emprise du bâtiment : aucun élément du bâti (mur, dalle, toiture, porte…) à moins de 0,30 m."""
    from ..geometry import zones as Z

    fabric = [g for g, e in idx.elements.items() if e["has_geometry"] and e["ifc_class"].startswith(BUILDING_FABRIC)]
    for g in uuids:
        e = idx.elements.get(g)
        if not e or e["ifc_class"] != "IfcSpace" or not e["has_geometry"]:
            continue
        run.seen("S11", g)
        fp = None
        near = False
        for h in idx.query_box(e["aabb"], tol=S11_GAP_M):
            f = idx.elements.get(h)
            if h == g or not f or not f["has_geometry"] or not f["ifc_class"].startswith(BUILDING_FABRIC):
                continue
            fp = Z.footprint(idx, g) if fp is None else fp
            fh = Z.footprint(idx, h)
            if fp is None or fh is None or Z.horizontal_distance(fp, fh) <= S11_GAP_M:
                near = True
                break
        if near:
            continue
        c = np.array([(e["aabb"][i] + e["aabb"][i + 3]) / 2 for i in range(3)])
        dmin = min((float(np.linalg.norm(np.maximum(0, np.maximum(np.array(idx.elements[h]["aabb"][:3]) - c,
                                                                     c - np.array(idx.elements[h]["aabb"][3:])))))
                    for h in fabric), default=None)
        where = f" ; élément du bâti le plus proche à {dmin:.1f} m" if dmin is not None else ""
        run.add("S11", g, f"Local hors de l'emprise du bâtiment : aucun mur, dalle, toiture, porte ni fenêtre à moins "
                f"de {S11_GAP_M} m{where} (copie égarée, local à supprimer ou à déplacer)",
                actual=None if dmin is None else round(dmin, 2), expected="local entouré du bâti",
                measure={"value": None if dmin is None else round(dmin, 2), "unit": "m"},
                threshold={"value": S11_GAP_M, "unit": "m", "source": "S11 (fixe)"})


def _structure_s10(run: _Run, idx: GeometryIndex, uuids: set[str]) -> None:
    with_geom_parents = {e["parent"] for e in idx.elements.values() if e.get("parent") and e["has_geometry"]}
    for g in uuids:
        e = idx.elements.get(g)
        if not e or e["ifc_class"] == "IfcSpace":
            continue
        if e["has_geometry"] or g in with_geom_parents:
            run.seen("S10", g)
            continue
        run.seen("S10", g)
        run.add("S10", g, f"Sans géométrie : {e.get('reason') or 'non triangulée'}", status="sans_geometrie",
                expected="représentation 3D (Body)")


def run_structure_rules(idx: GeometryIndex, records: dict[str, Record], *, uuids: set[str] | None = None,
                        checks: list[str] | None = None, displayed_storeys: dict[str, str] | None = None,
                        expected_buildings: int = 1, s4_thresholds: dict | None = None,
                        unique_property: str | None = None, door_direction_property: str | None = None,
                        georef_expected: dict | None = None) -> tuple[CheckResult, list[str]]:
    wanted = [r for r in STRUCTURE_RULES if not checks or r.id in {c.upper() for c in checks}]
    run = _Run(rules=wanted, records=records, idx=idx, storey_label=dict(displayed_storeys or {}))
    for s in idx.ifc.get("storeys") or []:
        run.storey_label.setdefault(s["guid"], s["name"])
    scope = {g for g, e in idx.elements.items() if e["ifc_class"] != "IfcSpace"}
    if uuids is not None:
        scope &= uuids
    ids = {r.id for r in wanted}
    if "S1" in ids:
        _structure_s1(run, idx, expected_buildings)
    if "S2" in ids:
        for g in scope:
            e = idx.elements[g]
            run.seen("S2", g)
            if not e.get("storey"):
                where = e.get("container_class")
                run.add("S2", g, "Aucun étage : " + (f"contenu dans {where}, pas dans un étage" if where
                                                    else "élément non contenu dans la structure spatiale"),
                        actual=where or "aucun conteneur", expected="IfcBuildingStorey")
    if "S3" in ids:
        _structure_s3(run, idx, displayed_storeys or {})
    if "S4" in ids:
        _structure_s4(run, idx, scope, s4_thresholds)
    if ids & {"S5", "S6", "S7"}:
        _structure_doors(run, idx, scope, door_direction_property)
        run.issues = [i for i in run.issues if i.req_id in ids]  # S5, S6, S7 calculées ensemble
    if "S8" in ids:
        _structure_s8(run, idx, scope, unique_property)
    if "S9" in ids:
        _structure_s9(run, idx, georef_expected)
    if "S10" in ids:
        _structure_s10(run, idx, scope)
    if "S11" in ids:
        spaces = {g for g, e in idx.elements.items() if e["ifc_class"] == "IfcSpace"}
        _structure_s11(run, idx, spaces if uuids is None else spaces & uuids)
    uc = idx.meta.get("unit_check") or {}
    if uc.get("status") == "ecart":
        run.notes.append(f"Unités : la hauteur déclarée des portes / fenêtres vaut {uc.get('median_ratio')} × la hauteur "
                         "mesurée — vérifier l'unité de longueur du fichier avant d'exploiter les mesures.")
    return run.result(), run.notes


# ── Géométrie et modélisation (lot 1 : G1, G3, G8) ──────────────────────

GEOMETRY_THRESHOLDS = {
    "space_min_area_m2": 1.0, "space_min_height_m": 2.50, "shaft_pattern": SHAFT_PATTERN,
    "railing_min_height_m": 1.00, "railing_name_tolerance_m": 0.05,
    "space_overlap_min_m3": 0.01, "space_overlap_exclude_pattern": None, "wall_join_gap_m": 0.02, "wall_support_gap_m": 0.02, "wall_screed_max_m": 0.35, "wall_level_tolerance_m": 0.005,
    "wall_support_search_m": 1.0, "stair_riser_max_m": 0.17, "stair_tread_min_m": 0.28,
    "stair_blondel_min_m": 0.60, "stair_blondel_max_m": 0.64, "stair_headroom_min_m": 2.00,
}

AREA_FIELDS = ("Qto_SpaceBaseQuantities.NetFloorArea", "BaseQuantities.NetFloorArea", "*.NetFloorArea",
               "Pset_SpaceCommon.NetPlannedArea", "*.Surface", "*.Area")
HEIGHT_FIELDS = ("Qto_SpaceBaseQuantities.Height", "BaseQuantities.Height", "*.FinishCeilingHeight",
                 "*.Hauteur non liée", "*.Unbounded Height")
RAILING_HEIGHT_FIELDS = ("Pset_RailingCommon.Height", "*.Hauteur du garde-corps", "*.Railing Height", "*.Height",
                         "*.Hauteur")


def _first_number(rec: Record | None, fields: tuple[str, ...]) -> tuple[float | None, str]:
    if rec is None:
        return None, ""
    for f in fields:
        fv = resolve_field(rec, f)
        if fv.found:
            n = _num(fv.value)
            if n is not None:
                return float(n), fv.location.split(":", 1)[-1]
    return None, ""


def run_geometry_rules(idx: GeometryIndex | None, records: dict[str, Record], *, uuids: set[str] | None = None,
                       checks: list[str] | None = None, thresholds: dict | None = None,
                       storey_labels: dict[str, str] | None = None) -> tuple[CheckResult, list[str]]:
    th = {**GEOMETRY_THRESHOLDS, **(thresholds or {})}
    wanted = [r for r in GEOMETRY_RULES if r.available and (not checks or r.id in {c.upper() for c in checks})]
    unavailable = [c.upper() for c in checks or [] if c.upper() in RULES and not RULES[c.upper()].available]
    run = _Run(rules=wanted, records=records, idx=idx, storey_label=dict(storey_labels or {}))
    if unavailable:
        run.notes.append(f"Règles {', '.join(unavailable)} : non disponibles, non contrôlées.")
    ids = {r.id for r in wanted}
    scope = [r for r in records.values() if uuids is None or r.uuid in uuids]
    shaft = re.compile(th["shaft_pattern"], re.IGNORECASE)

    if "G1" in ids:
        no_area = 0
        for rec in (r for r in scope if r.ifc_class == "IfcSpace"):
            run.seen("G1", rec.uuid)
            e = (idx.elements.get(rec.uuid) if idx else None) or {}
            label = str(rec.attrs.get("LongName") or rec.name or "")
            is_shaft = bool(shaft.search(label) or shaft.search(rec.name or ""))
            area, src = _first_number(rec, AREA_FIELDS)
            method = "Données"
            if area is None and e.get("floor_area") and e.get("is_closed"):
                area, src, method = e["floor_area"], "maillage (faces supérieures)", "Maillage"
            height, hsrc = _first_number(rec, HEIGHT_FIELDS)
            if height is None and e.get("aabb"):
                height, hsrc = e["aabb"][5] - e["aabb"][2], "AABB"
            if area is None:
                no_area += 1
            elif area < th["space_min_area_m2"]:
                run.add("G1", rec.uuid, f"Surface {area:.2f} m² < {th['space_min_area_m2']} m²"
                        + (f" — gaine « {label} », exclue du seuil" if is_shaft else ""),
                        status="exclu" if is_shaft else "sous_seuil", severity="info" if is_shaft else None,
                        actual=round(area, 2), expected=f"≥ {th['space_min_area_m2']} m² ({src})",
                        measure={"value": round(area, 3), "unit": "m²", "source": src},
                        threshold={"value": th["space_min_area_m2"], "unit": "m²", "source": "space_min_area_m2"})
                run.issues[-1].method = method
            if height is not None and height < th["space_min_height_m"] and not is_shaft:
                run.add("G1", rec.uuid, f"Hauteur {height:.2f} m < {th['space_min_height_m']} m",
                        status="sous_seuil", actual=round(height, 2),
                        expected=f"≥ {th['space_min_height_m']} m ({hsrc})",
                        measure={"value": round(height, 3), "unit": "m", "source": hsrc},
                        threshold={"value": th["space_min_height_m"], "unit": "m", "source": "space_min_height_m"})
        if no_area:
            run.notes.append(f"G1 : {no_area} local(aux) sans surface lisible (ni quantité, ni maillage fermé).")

    if "G3" in ids:
        if idx is None:
            run.notes.append("G3 : index géométrique indisponible, maillages des locaux non contrôlés.")
        else:
            for rec in (r for r in scope if r.ifc_class == "IfcSpace"):
                e = idx.elements.get(rec.uuid)
                run.seen("G3", rec.uuid)
                if not e or not e["has_geometry"]:
                    run.add("G3", rec.uuid, "Local sans géométrie : surface et volume non mesurables",
                            status="sans_geometrie", expected="volume fermé")
                elif not e.get("is_closed"):
                    run.add("G3", rec.uuid, "Maillage ouvert : volume et surface mesurés non fiables",
                            expected="volume fermé", measure={"value": _round(e.get("volume"), 3), "unit": "m³"})

    if "G8" in ids:
        mm = re.compile(r"(\d{3,4})\s*mm\b", re.IGNORECASE)
        for rec in (r for r in scope if r.ifc_class.startswith("IfcRailing")):
            run.seen("G8", rec.uuid)
            e = (idx.elements.get(rec.uuid) if idx else None) or {}
            h, src = _first_number(rec, RAILING_HEIGHT_FIELDS)
            method = "Données"
            if h is None and e.get("obb"):
                h, src, method = 2 * e["obb"]["half"][2], "boîte orientée", "Bbox"
            if h is None:
                run.notes.append(f"G8 : hauteur illisible pour « {rec.name} ».")
                continue
            if h < th["railing_min_height_m"] - 1e-6:
                run.add("G8", rec.uuid, f"Hauteur {h:.2f} m < {th['railing_min_height_m']:.2f} m", status="sous_seuil",
                        actual=round(h, 3), expected=f"≥ {th['railing_min_height_m']:.2f} m ({src})",
                        measure={"value": round(h, 3), "unit": "m", "source": src},
                        threshold={"value": th["railing_min_height_m"], "unit": "m", "source": "railing_min_height_m"})
                run.issues[-1].method = method
            type_name = str(rec.attrs.get("ObjectType") or e.get("type_name") or rec.name or "")
            m = mm.search(type_name)
            if m:
                named = int(m.group(1)) / 1000
                if abs(named - h) > th["railing_name_tolerance_m"]:
                    run.add("G8", rec.uuid, f"Type « {type_name} » ({named:.2f} m) mais hauteur {h:.2f} m",
                            status="incoherent", severity="mineur", actual=round(h, 3),
                            expected=f"{named:.2f} m d'après le nom du type",
                            measure={"value": round(h - named, 3), "unit": "m"},
                            threshold={"value": th["railing_name_tolerance_m"], "unit": "m",
                                       "source": "railing_name_tolerance_m"})
    if ids & {"G2", "G4", "G5", "G6", "G7"}:
        from .geometry_rules import run_lot2_rules

        run_lot2_rules(run, idx, scope, ids, th)
    return run.result(), run.notes
