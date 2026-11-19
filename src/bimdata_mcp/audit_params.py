"""Paramètres des audits : valeurs par défaut, plages de réglage, justification, référentiels et méthode.

Source unique pour : la liste des paramètres présentée avant un audit (``list_audits``), la page de réglage à
curseurs, l'injection des valeurs dans les options de ``run_audit`` et le chapitre « Hypothèses » des rapports PDF.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Param:
    key: str  # identifiant stable (audit.clé)
    label: str
    default: Any
    unit: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple = ()
    help: str = ""
    source: str = ""
    path: tuple = ()  # chemin dans les options de l'audit (ex. ("s4_thresholds", "base_tolerance_m"))
    optional: bool = False  # sans valeur par défaut : contrôle non lancé tant qu'elle n'est pas fournie

    def to_dict(self) -> dict:
        d = asdict(self)
        d["choices"] = list(self.choices)
        d["path"] = ".".join(self.path)
        return d


def _p(audit: str, key: str, label: str, default, unit="", lo=None, hi=None, step=None, help="", source="",
       path=None, choices=(), optional=False) -> Param:
    return Param(key=f"{audit}.{key}", label=label, default=default, unit=unit, min=lo, max=hi, step=step,
                 choices=tuple(choices), help=help, source=source, path=tuple(path or (key,)), optional=optional)


PARAMS: dict[str, list[Param]] = {
    "c1_structure": [
        _p("c1_structure", "base_tolerance_m", "S4 — tolérance d'assise d'un élément posé", 0.35, "m", 0.0, 1.0, 0.05,
           "Écart admis entre la base d'un élément et le niveau de son étage (structure sous revêtement, chape).",
           "Paramètre projet", ("s4_thresholds", "base_tolerance_m")),
        _p("c1_structure", "carried_tolerance_m", "S4 — descente admise d'un élément porté", 0.35, "m", 0.0, 1.5, 0.05,
           "Dessus d'une poutre, d'un plafond ou d'un réseau sous le niveau de son étage ; à augmenter sous une dalle "
           "épaisse (dalle alvéolaire).", "Paramètre projet", ("s4_thresholds", "carried_tolerance_m")),
        _p("c1_structure", "slab_top_below_m", "S4 — dessus de dalle sous le niveau", 0.35, "m", 0.0, 1.0, 0.05,
           "Dalle de structure sous le revêtement de sol.", "Paramètre projet", ("s4_thresholds", "slab_top_below_m")),
        _p("c1_structure", "ambiguous_gap_m", "S4 — écart d'étages non concluant", 0.70, "m", 0.0, 2.0, 0.05,
           "Étages plus proches que cette valeur : rattachement indécidable, signalé en information.",
           "Paramètre projet", ("s4_thresholds", "ambiguous_gap_m")),
    ],
    "c2_clash": [
        _p("c2_clash", "tol_h_m", "Tolérance horizontale des conflits", 0.005, "m", 0.0, 0.05, 0.001,
           "Pénétration horizontale sous laquelle une intersection n'est pas signalée.",
           "Usage Solibri (General Intersection), tutoriel ARC / Fluides / Structure : 5 mm", ("tol_h_m",)),
        _p("c2_clash", "tol_v_m", "Tolérance verticale des conflits", 0.005, "m", 0.0, 0.05, 0.001,
           "Pénétration verticale sous laquelle une intersection n'est pas signalée.", "Idem", ("tol_v_m",)),
        _p("c2_clash", "contact_m", "Contacts à plat ignorés", 0.0, "m", 0.0, 0.005, 0.001,
           "Surfaces à moins de cette distance : contact, pas conflit (0 : méthode d'origine ; 0,001 conseillé, "
           "à valider).", "Paramètre MCP", ("contact_m",)),
    ],
    "c4_geometrie": [
        _p("c4_geometrie", "space_min_area_m2", "G1 — surface minimale d'un local", 1.0, "m²", 0.0, 10.0, 0.1,
           "Locaux plus petits signalés (gaines exclues par leur nom).", "Paramètre projet",
           ("thresholds", "space_min_area_m2")),
        _p("c4_geometrie", "space_min_height_m", "G1 — hauteur minimale d'un local", 2.50, "m", 1.8, 3.5, 0.05,
           "Hauteur sous plafond minimale.", "Usage courant (logement : 2,50 m)", ("thresholds", "space_min_height_m")),
        _p("c4_geometrie", "wall_join_gap_m", "G5 — jeu maximal d'un raccord de murs", 0.02, "m", 0.0, 0.10, 0.005,
           "Extrémité de mur plus éloignée de tout mur ou poteau : mur non raccordé.", "Paramètre projet",
           ("thresholds", "wall_join_gap_m")),
        _p("c4_geometrie", "wall_support_gap_m", "G6 — jeu maximal sous un mur", 0.02, "m", 0.0, 0.10, 0.005,
           "Écart entre le bas du mur et son support au-delà duquel le mur est « flottant ».", "Paramètre projet",
           ("thresholds", "wall_support_gap_m")),
        _p("c4_geometrie", "wall_screed_max_m", "G6 — épaisseur de chape non modélisée admise", 0.35, "m", 0.0, 0.6,
           0.05, "Mur posé au niveau fini au-dessus d'une chape non modélisée : information.", "Paramètre projet",
           ("thresholds", "wall_screed_max_m")),
        _p("c4_geometrie", "stair_riser_max_m", "G7 — hauteur de marche maximale", 0.17, "m", 0.14, 0.22, 0.005,
           "Usage courant ; en PMR, voir l'audit d'accessibilité.", "Arrêté du 24/12/2015 (BHC) : 0,17 m",
           ("thresholds", "stair_riser_max_m")),
        _p("c4_geometrie", "stair_tread_min_m", "G7 — giron minimal", 0.28, "m", 0.20, 0.35, 0.005,
           "Profondeur de marche minimale.", "Arrêtés du 24/12/2015 et du 20/04/2017 : 0,28 m",
           ("thresholds", "stair_tread_min_m")),
        _p("c4_geometrie", "stair_headroom_min_m", "G7 — échappée minimale", 2.00, "m", 1.8, 2.4, 0.05,
           "Hauteur libre au-dessus des marches.", "Usage courant", ("thresholds", "stair_headroom_min_m")),
        _p("c4_geometrie", "railing_min_height_m", "G8 — hauteur minimale des garde-corps", 1.00, "m", 0.8, 1.2, 0.05,
           "Hauteur de protection.", "NF P01-012", ("thresholds", "railing_min_height_m")),
    ],
    "c5_pmr": [
        _p("c5_pmr", "referentiel", "Référentiel PMR", None, choices=("ERP 2017", "BHC 2015", "personnalisé"),
           help="Fixe les largeurs de portes, hauteurs de marche et girons.",
           source="Arrêté du 20/04/2017 (ERP) ou du 24/12/2015 (BHC)", path=("referentiel",), optional=True),
        _p("c5_pmr", "turning_height_m", "A3 — hauteur sous laquelle un élément gêne le demi-tour", 0.70, "m", 0.3,
           1.2, 0.05, "Laisse libre l'espace sous un lavabo.", "Arrêtés PMR (vide sous équipement)",
           ("thresholds", "turning_height_m")),
        _p("c5_pmr", "threshold_step_max_m", "A7 — ressaut maximal au seuil", 0.02, "m", 0.0, 0.05, 0.005,
           "Ressaut admis au passage d'une porte.", "Arrêtés PMR : 2 cm", ("thresholds", "threshold_step_max_m")),
    ],
    "c6_degagements": [
        _p("c6_degagements", "front_m", "Profondeur de la zone libre devant une porte", 1.00, "m", 0.5, 2.0, 0.05,
           "Côté ouverture de la porte.", "Paramètre projet", ("front_m",)),
        _p("c6_degagements", "tolerance_m", "Empiètement toléré dans la zone libre", 0.05, "m", 0.0, 0.20, 0.01,
           "Une boîte aux lettres ou une conduite plaquée au mur ne gênent pas.", "Paramètre projet",
           ("tolerance_m",)),
        _p("c6_degagements", "height_m", "Hauteur de la zone libre", 2.00, "m", 1.0, 2.5, 0.05,
           "Hauteur contrôlée au-dessus du sol.", "Paramètre projet", ("height_m",)),
    ],
    "c7_incendie": [
        _p("c7_incendie", "f3_referentiel", "Référentiel d'évacuation (F3, F5)", None,
           choices=("ERP", "ERP_J", "habitation_3A", "habitation_3B_4", "code_travail", "personnalise"),
           help="Fixe la cible (escalier, sortie) et les distances maximales.",
           source="Arrêté du 25/06/1980 (ERP), arrêté du 31/01/1986 (habitation), Code du travail",
           path=("f3_referentiel",), optional=True),
        _p("c7_incendie", "max_distance_m", "F3 — distance maximale (référentiel personnalisé)", 40.0, "m", 5.0, 60.0,
           1.0, "Utilisée seulement avec le référentiel « personnalisé ».", "Paramètre projet", ("max_distance_m",)),
        _p("c7_incendie", "dead_end_max_m", "F5 — longueur maximale de cul-de-sac", 10.0, "m", 0.0, 30.0, 0.5,
           "Longueur parcourue avant d'avoir le choix entre deux sorties.", "ERP 5e catégorie : 10 m",
           ("dead_end_max_m",)),
        _p("c7_incendie", "pmr_door_min_m", "F6 — largeur minimale d'une porte franchissable en fauteuil", 0.90, "m",
           0.7, 1.4, 0.01, "Porte plus étroite : infranchissable vers un espace d'attente sécurisé.",
           "Arrêté du 20/04/2017 : 0,90 m", ("pmr_door_min_m",)),
        _p("c7_incendie", "path_cell_m", "Maille de la grille de cheminement", 0.10, "m", 0.05, 0.50, 0.05,
           "Plus fine : plus précise et plus lente.", "Paramètre MCP", ("path_cell_m",)),
    ],
}

#: Textes de référence cités dans les rapports (valeurs toujours indicatives, à confirmer par le projet).
REFERENCES: dict[str, list[str]] = {
    "ccbim": ["Cahier des charges BIM (CCBIM) du projet transmis par l'utilisateur."],
    "general": ["ISO 16739-1:2018 — Industry Foundation Classes (IFC 4) ; IFC 2x3 TC1 (buildingSMART).",
                "ISO 19650 — organisation de l'information des ouvrages (bonnes pratiques de maquette exploitable).",
                "Convention BIM / charte de modélisation du projet (si fournie)."],
    "nommage": ["Charte de nommage du projet (si fournie), conventions de la convention BIM."],
    "classifications": ["Système de classification retenu par le projet (Uniformat, Omniclass, Uniclass…)."],
    "c1_structure": ["ISO 16739-1:2018 — Industry Foundation Classes (IFC 4) ; IFC 2x3 TC1 (buildingSMART).",
                     "Convention BIM / cahier des charges BIM du projet (structuration spatiale, identifiants)."],
    "c2_clash": ["Méthode de détection inspirée de la règle « General Intersection » de Solibri (doublons, "
                 "inclusions, chevauchements, tolérances, exceptions).",
                 "Protocole de synthèse du projet (matrice de disciplines, tolérances)."],
    "c3_informations": ["Cahier des charges des informations (LOI) importé pour le projet."],
    "c4_geometrie": ["NF P01-012 — Garde-corps : dimensions de sécurité.",
                     "Règle de confort des escaliers (formule de Blondel 2h + g entre 0,60 et 0,64 m).",
                     "Arrêté du 24 décembre 2015 (BHC) et arrêté du 20 avril 2017 (ERP) — marches et girons."],
    "c5_pmr": ["Arrêté du 20 avril 2017 relatif à l'accessibilité aux personnes handicapées des établissements recevant "
               "du public lors de leur construction et des installations ouvertes au public.",
               "Arrêté du 24 décembre 2015 relatif à l'accessibilité aux personnes handicapées des bâtiments "
               "d'habitation collectifs et des maisons individuelles lors de leur construction.",
               "Code de la construction et de l'habitation."],
    "c6_degagements": ["Exigences du programme et du projet (zones libres devant les portes et les équipements)."],
    "c7_incendie": ["Arrêté du 25 juin 1980 modifié portant approbation des dispositions générales du règlement de "
                    "sécurité contre les risques d'incendie et de panique dans les ERP (notamment CO 34, CO 43, CO 49).",
                    "Arrêté du 31 janvier 1986 relatif à la protection contre l'incendie des bâtiments d'habitation.",
                    "Code du travail, articles R. 4227-1 et suivants (dégagements des lieux de travail)."],
}

#: Méthode de contrôle, par audit (chapitre « Méthode » des rapports).
METHODS: dict[str, str] = {
    "ccbim": "Attendus extraits du CCBIM avec assistance d'IA (chacun avec sa source), validés par l'utilisateur, puis "
             "contrôlés sur les données de la maquette dans BIMData (présence, valeurs admises, format, unicité) et "
             "au niveau de la maquette (version IFC, nom du fichier, bâtiments, niveaux, locaux, objets génériques).",
    "general": "Lecture des données de la maquette dans BIMData (sans géométrie) : inventaire, structuration "
               "spatiale (bâtiment, niveaux, locaux), typage des objets, nommage et complétude des propriétés de "
               "base (matériau, IsExternal, LoadBearing, classification, résistance au feu).",
    "nommage": "Lecture des attributs et propriétés de chaque objet dans BIMData ; contrôle de présence, de valeur "
               "non générique et de format.",
    "classifications": "Lecture des classifications associées aux objets ; contrôle de présence et de cohérence "
                       "avec la classe IFC.",
    "c1_structure": "Lecture des relations IFC (contenance spatiale, réservations, remplissages) et des boîtes "
                    "englobantes calculées sur la géométrie du fichier IFC.",
    "c2_clash": "Paires candidates par boîtes englobantes, puis intersection confirmée et mesurée sur les maillages "
                "triangulés (pénétrations horizontale et verticale, volume) ; classement doublon / inclus / "
                "chevauchement ; exceptions et sévérités paramétrables.",
    "c3_informations": "Comparaison des propriétés renseignées avec les exigences du cahier des charges importé.",
    "c4_geometrie": "Mesures sur les boîtes orientées et les maillages (locaux, murs, escaliers, garde-corps) et "
                    "lecture des cotes renseignées.",
    "c5_pmr": "Lecture des largeurs et cotes renseignées ; zones virtuelles (cercle de demi-tour, espaces de "
              "manœuvre) testées contre les maillages.",
    "c6_degagements": "Zone virtuelle construite devant chaque porte, côté ouverture ; obstacles confirmés sur les "
                      "maillages.",
    "c7_incendie": "Grille de cheminement par étage et par niveau de plancher (murs, poteaux, fenêtres en obstacles, "
                   "portes et passages franchissables) ; distance mesurée suivant l'axe des circulations depuis le "
                   "point le plus éloigné de chaque local (ou la porte palière) ; culs-de-sac ; espaces d'attente "
                   "sécurisés.",
}

#: Avertissement porté par tous les rapports.
DISCLAIMER = (
    "Le présent document a été établi au moyen d'outils automatisés d'analyse de maquette numérique faisant appel à "
    "l'intelligence artificielle. Les constats, mesures et recommandations qu'il contient constituent une aide à la "
    "décision et un support de revue de maquette. Ils ne se substituent ni aux missions d'un bureau d'études, d'un "
    "bureau de contrôle technique ou d'un coordonnateur SSI, ni à l'avis de la commission de sécurité ou des services "
    "instructeurs, et n'ont en aucun cas valeur d'attestation de conformité réglementaire. Les seuils appliqués sont "
    "indicatifs ; ils résultent des hypothèses exposées dans le présent rapport et doivent être validés par un "
    "professionnel qualifié au regard du programme, du classement et de la catégorie de l'ouvrage. Les résultats "
    "dépendent de la qualité, de l'exhaustivité et de la date de la maquette analysée : tout élément non modélisé ou "
    "mal renseigné échappe au contrôle. Il appartient à l'utilisateur de vérifier les constats avant toute décision."
)
DISCLAIMER_SHORT = ("Aide à la décision produite avec assistance d'IA — ne remplace pas un bureau d'études ni une "
                    "attestation de conformité.")


def defaults(audit: str) -> dict[str, Any]:
    return {p.key: p.default for p in PARAMS.get(audit, [])}


def validate(values: dict[str, Any]) -> dict[str, Any]:
    """Valeurs reçues (clés « audit.clé ») contrôlées : connues, dans la plage, choix admis."""
    known = {p.key: p for ps in PARAMS.values() for p in ps}
    out = {}
    for k, v in (values or {}).items():
        p = known.get(k)
        if p is None:
            raise ValueError(f"Paramètre inconnu : {k}. Paramètres : {', '.join(sorted(known))}.")
        if v in (None, ""):
            out[k] = None
            continue
        if p.choices:
            if v not in p.choices:
                raise ValueError(f"{p.label} : « {v} » n'est pas un choix admis ({', '.join(p.choices)}).")
            out[k] = v
            continue
        x = float(v)
        if (p.min is not None and x < p.min - 1e-9) or (p.max is not None and x > p.max + 1e-9):
            raise ValueError(f"{p.label} : {x} {p.unit} hors de la plage [{p.min} ; {p.max}].")
        out[k] = x
    return out


def apply_to_options(audit: str, settings: dict[str, Any], options: dict | None) -> tuple[dict, list[dict]]:
    """Options de l'audit = réglages enregistrés, puis options explicites (prioritaires). Renvoie aussi le tableau
    des hypothèses : paramètre, valeur retenue, défaut, plage, source, modifié ou non."""
    opts = dict(options or {})
    rows = []
    for p in PARAMS.get(audit, []):
        explicit = _get(opts, p.path)
        value = explicit if explicit is not None else settings.get(p.key)
        if explicit is None and value is not None:
            _set(opts, p.path, value)
        used = value if value is not None else p.default
        rows.append({"cle": p.key, "parametre": p.label, "valeur": used, "defaut": p.default, "unite": p.unit,
                     "plage": (f"{_fr(p.min)} – {_fr(p.max)}" if p.min is not None else " / ".join(p.choices)),
                     "source": p.source, "modifie": used != p.default and used is not None,
                     "a_fournir": p.optional and used is None})
    return opts, rows


def _fr(x) -> str:
    return f"{x:g}".replace(".", ",") if isinstance(x, (int, float)) else str(x)


def _get(d: dict, path: tuple):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _set(d: dict, path: tuple, value) -> None:
    for k in path[:-1]:
        d = d.setdefault(k, {})
    d[path[-1]] = value
