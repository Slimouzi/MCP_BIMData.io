"""Modèle d'exigence + règles génériques de nommage / complétude."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["bloquant", "majeur", "mineur", "info"]
ValueType = Literal["string", "number", "integer", "boolean", "date"]

SEVERITY_ORDER = {"bloquant": 0, "majeur": 1, "mineur": 2, "info": 3}
PHASES = ("ESQ", "DIAG", "APS", "APD", "AVP", "PRO", "DCE", "EXE", "VISA", "DOE", "GESTION", "EXP")

#: Champs qu'une correction ne peut pas écrire via l'API (ils relèvent du
#: logiciel auteur : rattachement spatial, matériau, classe IFC…). Les Psets,
#: les attributs nommés et les classifications sont modifiables.
READ_ONLY_FIELDS = {"globalid", "ifcclass", "building", "storey", "space", "zone",
                    "material", "layer", "typename"}


class Requirement(BaseModel):
    """Une exigence de donnée, appliquée à une ou plusieurs classes IFC."""

    model_config = ConfigDict(extra="ignore")
    id: str = ""
    label: str = Field("", description="Libellé lisible (ex. « Résistance au feu »).")
    ifc_classes: list[str] = Field(default_factory=list, description="Vide = tous les éléments physiques.")
    field: str = Field(..., description="Chemin de champ : 'Name', 'Pset_WallCommon.FireRating', 'Material'…")
    required: bool = True
    value_type: ValueType | None = None
    allowed_values: list[str] | None = None
    pattern: str | None = Field(None, description="Regex que la valeur doit respecter entièrement.")
    forbidden_patterns: list[str] | None = Field(None, description="Regex interdites (search).")
    min_length: int | None = None
    max_length: int | None = None
    min_value: float | None = None
    max_value: float | None = None
    unique_scope: Literal["model", "building", "storey"] | None = None
    phases: list[str] = Field(default_factory=list, description="Vide = toutes phases.")
    severity: Severity = "majeur"
    category: Literal["nommage", "complétude", "conformité"] = "conformité"
    reference: str | None = None
    example: str | None = None
    source: str | None = Field(None, description="Origine (feuille/ligne du cahier des charges).")

    @model_validator(mode="after")
    def _check(self) -> Requirement:
        for p in [self.pattern, *(self.forbidden_patterns or [])]:
            if p:
                try:
                    re.compile(p)
                except re.error as exc:
                    raise ValueError(f"Regex invalide {p!r} : {exc}") from exc
        self.phases = [p.strip().upper() for p in self.phases if p and p.strip()]
        return self

    @property
    def writable(self) -> bool:
        return self.field.strip().lower() not in READ_ONLY_FIELDS

    def applies_to_phase(self, phase: str | None) -> bool:
        return not phase or not self.phases or phase.upper() in self.phases


_TRIM = r"^(?=\S)(?!.*\s{2})(?!.*\s$)[\s\S]*$"
_DEFAULT_NAME = [
    r"^(default|défaut|defaut|generic|générique|generique|sans nom|unnamed|nouveau|new|copy of|copie de)\b",
    r":\d{4,}$",  # suffixe d'identifiant Revit « Famille:Type:123456 »
]
_FORBIDDEN_CHARS = r"[<>|\"\\*?\t\r\n]"

STRUCTURAL = ["IfcWall", "IfcSlab", "IfcColumn", "IfcBeam", "IfcRoof", "IfcCovering", "IfcFooting",
              "IfcPile", "IfcStair", "IfcRamp", "IfcCurtainWall", "IfcPlate", "IfcMember"]


def default_rules(*, include_classification: bool = False, include_default_names: bool = True) -> list[Requirement]:
    """Règles génériques de nommage et de complétude (sans référentiel client)."""
    R = Requirement
    rules = [
        R(id="N01", label="Nom renseigné", field="Name", category="nommage", severity="majeur",
          ifc_classes=[]),
        R(id="N02", label="Nom sans espaces superflus", field="Name", category="nommage",
          severity="mineur", required=False, pattern=_TRIM),
        R(id="N03", label="Nom sans caractère interdit", field="Name", category="nommage",
          severity="mineur", required=False, forbidden_patterns=[_FORBIDDEN_CHARS], max_length=128),
        R(id="N10", label="Nom d'étage renseigné", field="Name", ifc_classes=["IfcBuildingStorey"],
          category="nommage", severity="bloquant"),
        R(id="N11", label="Nom d'étage unique", field="Name", ifc_classes=["IfcBuildingStorey"],
          category="nommage", severity="majeur", unique_scope="building"),
        R(id="N12", label="Nom de bâtiment renseigné", field="Name", ifc_classes=["IfcBuilding"],
          category="nommage", severity="bloquant"),
        R(id="N13", label="Nom de site renseigné", field="Name", ifc_classes=["IfcSite"],
          category="nommage", severity="majeur"),
        R(id="N20", label="Numéro de pièce (Name) renseigné", field="Name", ifc_classes=["IfcSpace"],
          category="nommage", severity="majeur"),
        R(id="N21", label="Nom de pièce (LongName) renseigné", field="LongName", ifc_classes=["IfcSpace"],
          category="nommage", severity="majeur"),
        R(id="N22", label="Numéro de pièce unique par étage", field="Name", ifc_classes=["IfcSpace"],
          category="nommage", severity="majeur", unique_scope="storey"),
        R(id="N30", label="Nom de zone renseigné", field="Name", ifc_classes=["IfcZone"],
          category="nommage", severity="majeur"),
        R(id="C01", label="Type (ObjectType) renseigné", field="ObjectType", category="complétude",
          severity="mineur"),
        R(id="C02", label="Rattachement à un étage", field="Storey", category="complétude",
          severity="majeur"),
        R(id="C03", label="Matériau renseigné", field="Material", ifc_classes=STRUCTURAL,
          category="complétude", severity="mineur"),
        R(id="C10", label="Paroi extérieure / intérieure (IsExternal)", field="*.IsExternal",
          ifc_classes=["IfcWall", "IfcDoor", "IfcWindow", "IfcSlab", "IfcRoof"], category="complétude",
          severity="mineur", value_type="boolean"),
        R(id="C11", label="Porteur (LoadBearing)", field="*.LoadBearing",
          ifc_classes=["IfcWall", "IfcSlab", "IfcColumn", "IfcBeam"], category="complétude",
          severity="mineur", value_type="boolean"),
    ]
    if include_default_names:
        rules.insert(3, R(id="N04", label="Nom non générique (nom par défaut du logiciel)", field="Name",
                          category="nommage", severity="mineur", required=False,
                          forbidden_patterns=_DEFAULT_NAME))
    if include_classification:
        rules.append(R(id="C20", label="Classification renseignée", field="Classification",
                       category="complétude", severity="mineur"))
    return rules
