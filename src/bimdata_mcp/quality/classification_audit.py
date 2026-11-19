"""Audit des classifications : manquantes, incohérentes, et propositions de correction.

Contrôles (sur les éléments physiques du périmètre, pour le système audité) :

K01  Classification présente — l'élément porte un code du système audité.
K02  Un seul code par système — pas deux codes du même système sur un élément.
K03  Format du code — pour UniFormat : lettre A–G + chiffres (ex. B2010, D5010200).
K04  Cohérence classe IFC ↔ code — pour UniFormat : la famille du code (niveau 2, ex. D50
     « Électricité ») est compatible avec la classe IFC (un IfcWall en D50 est incohérent).
K05  Cohérence au sein d'un type — les éléments d'un même type (classe IFC + type d'objet)
     portent le même code ; un élément qui s'écarte du code majoritaire est signalé.

Propositions (jamais inventées : toujours reprises de la maquette) :
- code majoritaire des autres éléments du même type (confiance selon l'unanimité) ;
- à défaut, code majoritaire de la même classe IFC, s'il est compatible (confiance faible) ;
- à défaut, seule la famille UniFormat attendue est indiquée dans « attendu ».

Le résultat est un :class:`~bimdata_mcp.quality.engine.CheckResult` standard : rapport Excel,
``get_check_issues``, ``prepare_corrections_from_check`` et réimport du rapport fonctionnent tels quels.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..model_view import Record, normalize_text
from .engine import CheckResult, Issue
from .requirements import SEVERITY_ORDER, Requirement

# ── Référentiel générique UniFormat II : famille (niveau 2) → classes IFC compatibles ──────────
# Classes « réseaux » génériques IFC2x3 acceptées dans toutes les familles D.
_FLOW = {"IfcFlowSegment", "IfcFlowFitting", "IfcFlowTerminal", "IfcFlowController", "IfcFlowMovingDevice",
         "IfcFlowStorageDevice", "IfcFlowTreatmentDevice", "IfcEnergyConversionDevice", "IfcDistributionElement",
         "IfcDistributionFlowElement", "IfcDistributionControlElement", "IfcDistributionChamberElement"}
_PIPES = {"IfcPipeSegment", "IfcPipeFitting", "IfcValve", "IfcPump", "IfcTank"}

UNIFORMAT_FAMILIES: dict[str, tuple[str, set[str]]] = {
    "A10": ("Fondations", {"IfcFooting", "IfcPile", "IfcSlab", "IfcBeam", "IfcWall", "IfcWallStandardCase"}),
    "A20": ("Infrastructure / sous-sol", {"IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcFooting", "IfcColumn",
                                          "IfcBeam"}),
    "B10": ("Superstructure", {"IfcBeam", "IfcColumn", "IfcSlab", "IfcMember", "IfcRoof", "IfcPlate", "IfcRamp",
                               "IfcRampFlight", "IfcWall", "IfcWallStandardCase", "IfcStair", "IfcStairFlight"}),
    "B20": ("Enveloppe extérieure", {"IfcWall", "IfcWallStandardCase", "IfcWindow", "IfcDoor", "IfcCurtainWall",
                                     "IfcPlate", "IfcMember", "IfcCovering", "IfcRailing", "IfcShadingDevice"}),
    "B30": ("Couverture", {"IfcRoof", "IfcSlab", "IfcCovering", "IfcWindow", "IfcPlate", "IfcMember", "IfcRailing"}),
    "C10": ("Aménagements intérieurs", {"IfcWall", "IfcWallStandardCase", "IfcDoor", "IfcWindow", "IfcRailing",
                                        "IfcCovering", "IfcFurnishingElement", "IfcPlate", "IfcMember",
                                        "IfcCurtainWall"}),
    "C20": ("Escaliers", {"IfcStair", "IfcStairFlight", "IfcRailing", "IfcSlab", "IfcRamp", "IfcRampFlight",
                          "IfcMember"}),
    "C30": ("Finitions intérieures", {"IfcCovering", "IfcWall", "IfcWallStandardCase", "IfcSlab"}),
    "D10": ("Transport (ascenseurs…)", {"IfcTransportElement"} | _FLOW),
    "D20": ("Plomberie", {"IfcSanitaryTerminal", "IfcWasteTerminal", "IfcBoiler", "IfcInterceptor"}
            | _PIPES | _FLOW),
    "D30": ("CVC", {"IfcDuctSegment", "IfcDuctFitting", "IfcDuctSilencer", "IfcAirTerminal", "IfcAirTerminalBox",
                    "IfcDamper", "IfcFan", "IfcUnitaryEquipment", "IfcBoiler", "IfcChiller", "IfcCoil",
                    "IfcSpaceHeater", "IfcAirToAirHeatRecovery", "IfcCoolingTower", "IfcHeatExchanger",
                    "IfcFilter", "IfcHumidifier", "IfcCompressor", "IfcCondenser", "IfcEvaporator",
                    "IfcBurner", "IfcSensor", "IfcController", "IfcActuator", "IfcUnitaryControlElement",
                    "IfcFlowMeter"} | _PIPES | _FLOW),
    "D40": ("Protection incendie", {"IfcFireSuppressionTerminal", "IfcAlarm", "IfcSensor"} | _PIPES | _FLOW),
    "D50": ("Électricité", {"IfcElectricDistributionBoard", "IfcLightFixture", "IfcLamp", "IfcCableSegment",
                            "IfcCableFitting", "IfcCableCarrierSegment", "IfcCableCarrierFitting", "IfcOutlet",
                            "IfcSwitchingDevice", "IfcJunctionBox", "IfcProtectiveDevice",
                            "IfcProtectiveDeviceTrippingUnit", "IfcTransformer", "IfcElectricGenerator",
                            "IfcElectricAppliance", "IfcAlarm", "IfcSensor", "IfcCommunicationsAppliance",
                            "IfcAudioVisualAppliance", "IfcElectricFlowStorageDevice", "IfcElectricMotor",
                            "IfcElectricTimeControl", "IfcController", "IfcActuator", "IfcUnitaryControlElement",
                            "IfcMotorConnection", "IfcSolarDevice"} | _FLOW),
    "E10": ("Équipements", {"IfcFurnishingElement", "IfcFurniture", "IfcSystemFurnitureElement",
                            "IfcElectricAppliance", "IfcMedicalDevice", "IfcSanitaryTerminal",
                            "IfcTransportElement", "IfcUnitaryEquipment"} | _FLOW),
    "E20": ("Mobilier", {"IfcFurnishingElement", "IfcFurniture", "IfcSystemFurnitureElement"}),
}
#: Classes trop génériques pour juger la cohérence (proxy, éléments d'assemblage…).
_NO_COHERENCE = {"IfcBuildingElementProxy", "IfcElementAssembly", "IfcBuildingElementPart", "IfcDiscreteAccessory",
                 "IfcFastener", "IfcMechanicalFastener", "IfcReinforcingBar", "IfcReinforcingMesh", "IfcTendon",
                 "IfcProxy"}
_UNIFORMAT_CODE = re.compile(r"^[A-G]\d{2}[\d.]*$", re.IGNORECASE)
_REVIT_ID = re.compile(r":\d{4,}$")

REQUIREMENTS = [
    Requirement(id="K01", label="Classification présente", field="Classification", category="complétude",
                severity="majeur"),
    Requirement(id="K02", label="Un seul code par système", field="Classification", category="conformité",
                severity="majeur"),
    Requirement(id="K03", label="Format du code", field="Classification", category="conformité", severity="mineur"),
    Requirement(id="K04", label="Cohérence classe IFC ↔ code", field="Classification", category="conformité",
                severity="majeur"),
    Requirement(id="K05", label="Cohérence au sein d'un type", field="Classification", category="conformité",
                severity="mineur"),
]
_REQ = {r.id: r for r in REQUIREMENTS}


def is_uniformat(system: str) -> bool:
    return "uniformat" in normalize_text(system).replace(" ", "")


def family_of(code: str) -> str | None:
    c = code.strip().upper()
    return c[:3] if _UNIFORMAT_CODE.match(c) else None


def expected_families(ifc_class: str) -> list[str]:
    return [f for f, (_, classes) in UNIFORMAT_FAMILIES.items() if ifc_class in classes]


def _family_label(fams: list[str]) -> str:
    return ", ".join(f"{f} {UNIFORMAT_FAMILIES[f][0]}" for f in fams)


def type_key(rec: Record) -> tuple[str, str]:
    """Regroupe les occurrences d'un même type : classe IFC + type d'objet (ou nom sans l'identifiant Revit)."""
    t = str(rec.attrs.get("ObjectType") or "").strip() or _REVIT_ID.sub("", rec.name).strip()
    return rec.ifc_class, normalize_text(t)


def codes_in(rec: Record, system: str) -> list[str]:
    """Codes de ``rec`` dans ``system`` (comparaison de système insensible à la casse)."""
    s = normalize_text(system)
    out = []
    for c in rec.classifications:
        sys_, sep, code = c.partition(":")
        if sep and normalize_text(sys_) == s and code.strip():
            out.append(code.strip())
    return out


def detect_system(records: list[Record]) -> str | None:
    """Système le plus utilisé dans le périmètre (orthographe exacte de la maquette)."""
    cnt = Counter(c.partition(":")[0].strip() for r in records for c in r.classifications if ":" in c)
    return cnt.most_common(1)[0][0] if cnt else None


def _coherent(ifc_class: str, code: str, uniformat: bool) -> bool | None:
    """True/False si jugeable, None sinon (système non UniFormat, classe générique, famille hors référentiel)."""
    if not uniformat or ifc_class in _NO_COHERENCE:
        return None
    fam = family_of(code)
    if fam is None or fam not in UNIFORMAT_FAMILIES:
        return None
    if not expected_families(ifc_class):
        return None
    return ifc_class in UNIFORMAT_FAMILIES[fam][1]


def run_classification_audit(records: list[Record], system: str) -> tuple[CheckResult, list[Requirement]]:
    uniformat = is_uniformat(system)
    recs = [r for r in records if r.is_physical]
    res = CheckResult(n_elements=len(recs), n_requirements=len(REQUIREMENTS), n_checks=0)
    stats: dict[str, Counter] = {k: Counter() for k in _REQ}
    by_uuid_codes = {r.uuid: codes_in(r, system) for r in recs}

    # Codes de référence : par type puis par classe IFC (seuls les codes uniques, valides et cohérents comptent).
    def _good(r: Record) -> str | None:
        codes = by_uuid_codes[r.uuid]
        if len(codes) != 1:
            return None
        c = codes[0]
        if uniformat and not _UNIFORMAT_CODE.match(c):
            return None
        return c if _coherent(r.ifc_class, c, uniformat) is not False else None

    per_type: dict[tuple, Counter] = defaultdict(Counter)
    per_class: dict[str, Counter] = defaultdict(Counter)
    for r in recs:
        g = _good(r)
        if g:
            per_type[type_key(r)][g] += 1
            per_class[r.ifc_class][g] += 1

    def propose(r: Record, exclude: str | None = None) -> tuple[str | None, float, str]:
        peers = per_type.get(type_key(r), Counter()).copy()
        if exclude and exclude in peers:
            peers[exclude] -= 1  # l'élément lui-même ne vote pas
            peers += Counter()
        if peers:
            code, n = peers.most_common(1)[0]
            total = sum(peers.values())
            share = n / total
            if share == 1:
                conf = 0.9 if total >= 2 else 0.7     # type unanime (0.7 si un seul élément de référence)
            elif share >= 0.75 and total >= 3:
                conf = 0.8
            elif share >= 0.6:
                conf = 0.6
            else:
                conf = 0.4
            return code, conf, f"code de {n}/{total} élément(s) du même type"
        cls = per_class.get(r.ifc_class, Counter())
        if cls:
            code, n = cls.most_common(1)[0]
            total = sum(cls.values())
            return code, 0.4 if n / total >= 0.75 else 0.3, f"code de {n}/{total} {r.ifc_class} de la maquette"
        return None, 0.0, ""

    counter = 0

    def add(r: Record, req_id: str, status: str, actual, expected: str, message: str,
            proposal: str | None = None, conf: float = 0.0, why: str = "") -> None:
        nonlocal counter
        counter += 1
        req = _REQ[req_id]
        res.issues.append(Issue(
            issue_id=f"I{counter:06d}", req_id=req_id, req_label=req.label, category=req.category,
            severity=req.severity, uuid=r.uuid, ifc_class=r.ifc_class, name=r.name, storey=r.storey,
            field="Classification", location="special:Classification", status=status, actual=actual,
            expected=expected, message=message, writable=True,
            proposal=f"{system}:{proposal}" if proposal else None, confidence=conf if proposal else 0.0,
            proposal_reason=why if proposal else "",
        ))
        stats[req_id][status] += 1

    for r in recs:
        codes = by_uuid_codes[r.uuid]
        actual = " | ".join(r.classifications)
        fams = expected_families(r.ifc_class) if uniformat else []
        hint = f"code {system} (famille {_family_label(fams)})" if fams else f"code {system}"
        res.cells.setdefault(r.uuid, {})["Classification"] = (actual, "conforme")
        res.n_checks += 1
        # K01 — présence
        if not codes:
            p, c, why = propose(r)
            status = "absent" if not r.classifications else "autre_systeme"
            msg = ("Aucune classification" if not r.classifications
                   else f"Aucun code {system} (autres systèmes : {actual})")
            add(r, "K01", status, actual, hint, msg, p, c, why)
            res.cells[r.uuid]["Classification"] = (actual, status)
            continue
        stats["K01"]["conforme"] += 1
        # K02 — un seul code
        res.n_checks += 1
        if len(codes) > 1:
            p, c, why = propose(r)
            p = p if p in codes else None  # ne garder qu'un code déjà porté
            add(r, "K02", "codes_multiples", actual, f"un seul code {system}",
                f"{len(codes)} codes {system} : {', '.join(codes)}", p, c * 0.9 if p else 0.0,
                f"{why} ; les autres codes {system} seront retirés" if p else "")
            res.cells[r.uuid]["Classification"] = (actual, "codes_multiples")
            continue
        stats["K02"]["conforme"] += 1
        code = codes[0]
        # K03 — format
        if uniformat:
            res.n_checks += 1
            if not _UNIFORMAT_CODE.match(code):
                p, c, why = propose(r, exclude=code)
                add(r, "K03", "format_invalide", actual, "lettre A–G + chiffres (ex. D5010200)",
                    f"Code « {code} » hors format UniFormat", p, c, why)
                res.cells[r.uuid]["Classification"] = (actual, "format_invalide")
                continue
            stats["K03"]["conforme"] += 1
        # K04 — cohérence classe IFC ↔ famille
        ok = _coherent(r.ifc_class, code, uniformat)
        if ok is not None:
            res.n_checks += 1
            if not ok:
                fam = family_of(code)
                p, c, why = propose(r, exclude=code)
                add(r, "K04", "incoherent_classe", actual, hint,
                    f"{r.ifc_class} classé en {fam} ({UNIFORMAT_FAMILIES[fam][0]}) : famille attendue "
                    f"{_family_label(fams)}", p, c, why)
                res.cells[r.uuid]["Classification"] = (actual, "incoherent_classe")
                continue
            stats["K04"]["conforme"] += 1
        # K05 — cohérence dans le type
        peers = per_type.get(type_key(r), Counter()).copy()
        if code in peers:
            peers[code] -= 1
            peers += Counter()
        if sum(peers.values()) >= 2:
            res.n_checks += 1
            top, n = peers.most_common(1)[0]
            share = n / sum(peers.values())
            if top != code and share >= 0.6:
                conf = 0.8 if share >= 0.75 else 0.6
                add(r, "K05", "incoherent_type", actual, f"{system}:{top} (code du type)",
                    f"Code {code} différent de celui de {n}/{sum(peers.values())} élément(s) du même type ({top})",
                    top, conf, f"code de {n}/{sum(peers.values())} élément(s) du même type")
                res.cells[r.uuid]["Classification"] = (actual, "incoherent_type")
                continue
            stats["K05"]["conforme"] += 1

    for req in REQUIREMENTS:
        st = stats[req.id]
        nc = sum(v for k, v in st.items() if k != "conforme")
        applicable = st["conforme"] + nc
        res.per_requirement.append({
            "req_id": req.id, "label": req.label, "field": req.field, "ifc_classes": "(tous éléments physiques)",
            "severity": req.severity, "category": req.category, "applicable": applicable,
            "conformes": st["conforme"], "non_conformes": nc,
            "taux": round(st["conforme"] / applicable, 4) if applicable else None, "optionnels_absents": 0,
        })
    res.checked_uuids = sorted(r.uuid for r in recs)
    res.issues.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.req_id, i.ifc_class, i.name))
    for n, i in enumerate(res.issues, start=1):
        i.issue_id = f"I{n:06d}"
    return res, REQUIREMENTS
