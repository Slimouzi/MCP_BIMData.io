"""Lecture critique d'un contrôle : fiabilité de chaque règle, faux positifs probables, données d'entrée manquantes.

Un rapport qui empile des centaines d'anomalies sans dire lesquelles sont sûres ne sert pas la décision. Ce module
produit des **indicateurs automatiques** de fiabilité, repris en tête du rapport PDF :

- taux d'anomalies anormal sur une règle (paramétrage ou données d'entrée à vérifier) ;
- écarts faibles majoritaires (contacts, tolérance trop stricte) ;
- obstacle ou élément en cause dominant (une famille d'objets explique l'essentiel des anomalies) ;
- objets majoritaires probablement hors du champ de la règle (éviers, gaines…) ;
- données d'entrée manquantes signalées par le contrôle (sens d'ouverture, référentiel, propriétés absentes).

L'analyse d'expert (rédigée par l'assistant, validée par l'utilisateur) complète ou remplace ces indicateurs.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

#: Seuils des indicateurs.
HIGH_RATE = 0.5  # plus d'une vérification sur deux en anomalie
HIGH_RATE_MIN = 20  # … sur au moins 20 anomalies
SMALL_GAP_M = 0.05  # écart à la règle de 5 cm au plus
DOMINANT = 0.5  # une famille explique plus de la moitié des anomalies
MIN_ISSUES = 10

#: Familles d'objets rarement concernées par une règle d'accessibilité, de dégagement ou d'évacuation.
OUT_OF_SCOPE = re.compile(r"(?i)(evier|évier|lavabo|wc\b|cuvette|gaine|shaft|placard|trappe|meuble|kitchen|cuisine|"
                          r"mobilier|furniture|equipement|équipement|sanitaire)")
#: Notes du contrôle qui signalent une donnée d'entrée manquante.
DATA_GAP = re.compile(r"(?i)(non renseign|inconnu|absent|non contrôlée|à fournir|indisponible|côté le plus exigeant|"
                      r"des deux côtés|sens d'ouverture)")


def family(name: str) -> str:
    """Famille d'un objet : nom sans identifiant Revit ni dimensions (« Porte 90:NAD_P90:123456 » → « NAD_P90 »)."""
    n = re.sub(r":\d{4,}$", "", str(name or "")).strip()
    parts = [p for p in n.split(":") if p]
    base = parts[1] if len(parts) >= 2 else (parts[0] if parts else "")
    m = re.match(r"([A-Za-z]{2,}_[A-Za-z]+\d*)", base)
    return m.group(1) if m else re.sub(r"[\s_-]*\d.*$", "", base).strip() or base


def _value(d) -> float | None:
    try:
        return float((d or {}).get("value"))
    except (TypeError, ValueError):
        return None


def analyze(check: dict, names: dict[str, tuple[str, str]] | None = None) -> dict:
    """Indicateurs de fiabilité par règle. ``names`` : uuid → (classe IFC, nom) pour les éléments liés."""
    names = names or {}
    issues = [i for i in check.get("issues") or [] if i.get("status") != "exclu"]
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for i in issues:
        by_rule[i.get("rule_id") or i.get("req_id") or "?"].append(i)
    rules = {r.get("req_id"): r for r in check.get("per_requirement") or []}
    out = []
    for rule, iss in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        if len(iss) < MIN_ISSUES:
            continue
        reasons: list[str] = []
        pr = rules.get(rule) or {}
        appl = pr.get("applicable") or 0
        if appl and len(iss) >= HIGH_RATE_MIN and len(iss) / appl >= HIGH_RATE:
            reasons.append(f"{100 * len(iss) / appl:.0f} % des vérifications en anomalie : taux anormal, à rapprocher "
                           "du paramétrage et des données d'entrée")
        gaps = []
        for i in iss:
            v, t = _value(i.get("measure")), _value(i.get("threshold"))
            unit = ((i.get("measure") or {}).get("unit") or "m").lower()
            if v is not None and t is not None and unit in ("m", "mm"):
                gaps.append(abs(v - t) / (1000 if unit == "mm" else 1))
        small = sum(1 for g in gaps if g <= SMALL_GAP_M)
        if gaps and small / len(gaps) >= DOMINANT:
            reasons.append(f"{small} écart(s) sur {len(gaps)} de {int(SMALL_GAP_M * 100)} cm au plus : contacts ou "
                           "tolérance trop stricte plutôt que vrais défauts")
        others = Counter()
        for i in iss:
            for e in i.get("elements") or []:
                if e.get("role") not in ("A", "principal", "porte", "cible") and e.get("uuid") != i.get("uuid"):
                    cls, nm = names.get(e["uuid"], ("", ""))
                    others[f"{cls} {family(nm)}".strip()] += 1
        if others:
            fam, n = others.most_common(1)[0]
            if fam and n / len(iss) >= DOMINANT:
                reasons.append(f"obstacle ou élément lié dominant : {fam} ({100 * n / len(iss):.0f} % des anomalies) — "
                               "vérifier qu'il s'agit d'un vrai obstacle (cloison au contact, galandage…)")
        mains = Counter(family(i.get("name")) for i in iss)
        fam, n = mains.most_common(1)[0]
        if fam and OUT_OF_SCOPE.search(fam) and n / len(iss) >= 0.2:
            reasons.append(f"« {fam} » représente {100 * n / len(iss):.0f} % des éléments signalés : objets "
                           "probablement hors du champ de la règle, à exclure du périmètre")
        verdict = "peu fiable" if len(reasons) >= 2 else "à confirmer" if reasons else "fiable"
        out.append({"regle": rule, "libelle": pr.get("label") or (iss[0].get("req_label") or ""), "anomalies": len(iss),
                    "verdict": verdict, "raisons": reasons})
    data_gaps = list(dict.fromkeys(n for n in check.get("notes") or [] if DATA_GAP.search(n)))[:6]
    doubtful = [r for r in out if r["verdict"] != "fiable"]
    n_doubt = sum(r["anomalies"] for r in doubtful)
    reco = None
    if doubtful:
        reco = (f"Ne pas envoyer en l'état en topics BCF les {n_doubt} anomalies des règles "
                f"{', '.join(r['regle'] for r in doubtful)} : lever d'abord les doutes ci-dessus (périmètre, données "
                "d'entrée, tolérances), puis relancer le contrôle.")
    return {"regles": out, "donnees_manquantes": data_gaps, "recommandation_bcf": reco,
            "regles_douteuses": [r["regle"] for r in doubtful], "anomalies_douteuses": n_doubt}
