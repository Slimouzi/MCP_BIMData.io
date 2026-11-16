"""Import d'un cahier des charges de données (Excel / CSV) → liste d'exigences.

Deux usages :

1. **Gabarit** fourni par le serveur (``write_template``) : colonnes fixes,
   lecture sans ambiguïté.
2. **Fichier du projet** (charte de données, annexe contractuelle, table des attributs…) : la ligne
   d'en-tête est **détectée** (synonymes FR/EN, en-têtes sur deux lignes,
   cellules fusionnées propagées), ainsi que les colonnes de **phases**
   (APS, APD, PRO, DCE, EXE, DOE…) cochées par « x ». Un ``column_map`` permet
   de forcer l'association colonne → champ quand la détection échoue.

Chaque ligne retenue devient une :class:`Requirement` ; chaque ligne écartée
l'est avec une raison (``warnings``) : un cahier des charges mal lu en silence
produirait un contrôle faux mais d'apparence complète.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from ..model_view import ATTRIBUTE_FIELDS, normalize_text
from .requirements import PHASES, Requirement

FIELDS: dict[str, list[str]] = {
    "ifc_class": ["classe ifc", "classe d'objet ifc", "classe d objet ifc", "ifc class", "entite ifc",
                  "entité ifc", "ifc entity", "objet ifc", "ifcobject", "classe"],
    "property": ["propriete technique", "nom technique", "attribut/proprietes", "attribut / proprietes",
                 "attribut/propriete", "property name", "propriete ifc", "proprietes de l'objet",
                 "propriete", "proprietes", "attribut", "property", "parametre", "champ"],
    "pset": ["pset", "property set", "propertyset", "ensemble de proprietes", "jeu de proprietes",
             "groupe de proprietes"],
    "label": ["nom de la propriete", "libelle", "designation", "besoin", "intitule", "description de la donnee"],
    "object": ["nom de l'objet", "objet", "element", "famille d'objet", "object"],
    "value_type": ["type de valeur", "type de donnee", "format de la valeur", "data type", "value type",
                   "type de la valeur", "unite/type", "type"],
    "allowed_values": ["valeurs autorisees", "valeurs possibles", "liste de valeurs", "valeurs admises",
                       "allowed values", "valeurs attendues", "enumeration", "liste"],
    "pattern": ["regex", "expression reguliere", "format (regex)", "regle de nommage", "pattern", "syntaxe",
                "format"],
    "required": ["obligatoire", "requis", "required", "exigence", "niveau d'exigence", "obligation"],
    "example": ["exemple de valeur", "exemple", "example"],
    "reference": ["reference", "ref", "chapitre", "article", "id exigence"],
    "severity": ["criticite", "gravite", "severite", "priorite", "severity"],
    "unique": ["unicite", "unique"],
    "max_length": ["longueur max", "longueur maximale", "nb caracteres max", "max length"],
    "phases": ["phases", "phase"],
    "comment": ["commentaire", "commentaires", "remarque", "observations", "note"],
}

_PHASE_TOKENS = {p.lower(): p for p in PHASES} | {"esquisse": "ESQ", "diagnostic": "DIAG",
                                                    "gestion": "GESTION", "exploitation": "EXP"}
_YES = {"x", "oui", "yes", "o", "y", "1", "true", "vrai", "obligatoire", "requis", "required", "●", "•", "✓", "✔", "ob"}
_NO = {"non", "no", "n", "0", "false", "faux", "facultatif", "optionnel", "optional", "f", "-", "conditionnel"}
_MATERIAL = {"material", "materials", "materiau", "materiaux", "materiallayer", "material layer", "matiere"}
_CLASSIF = {"classification", "uniformat", "uniformat ii", "omniclass", "code uniformat", "classification uniformat"}
_ATTR_PSET = {"attribut", "attributs", "attribut ifc", "ifcattribute", "attributes", "ifc", "-", "attribut natif", "natif"}
_EXTRA_ATTRS = {"reflatitude", "reflongitude", "refelevation", "elevation", "landtitlenumber", "compositiontype",
                "interiororexteriorspace", "elevationwithflooring", "operationtype", "overallheight", "overallwidth"}
_SECTION_MARKERS = {"documents": "documents", "document": "documents", "documents attendus": "documents",
                    "livrables documentaires": "documents", "proprietes": "properties",
                    "propriete": "properties", "properties": "properties", "attributs": "properties"}
_TYPES = {
    "string": ["texte", "text", "string", "chaine", "label", "ifclabel", "ifctext", "identifier", "ifcidentifier",
               "alphanumerique", "caractere"],
    "number": ["reel", "real", "nombre", "numerique", "float", "decimal", "double", "measure", "mesure",
               "surface", "longueur", "m2", "m²", "m3", "kw", "ifcreal", "ifclengthmeasure", "ifcareameasure"],
    "integer": ["entier", "integer", "int", "ifcinteger"],
    "boolean": ["booleen", "boolean", "bool", "oui/non", "vrai/faux", "ifcboolean", "logique"],
    "date": ["date", "ifcdate"],
}


def _norm(v: Any) -> str:
    return normalize_text(v).replace("’", "'")


def _score(header: str, synonyms: list[str]) -> int:
    h = _norm(header)
    if not h:
        return 0
    best = 0
    for i, syn in enumerate(synonyms):
        s = _norm(syn)
        bonus = max(0, 3 - i // 4)  # synonymes de tête = plus spécifiques
        if h == s:
            best = max(best, 30 + bonus)
        elif h.startswith(s):
            best = max(best, 20 + bonus)
        elif re.search(rf"(^|[\s/(]){re.escape(s)}($|[\s/)])", h):
            best = max(best, 10 + bonus)
    return best


def _phase_of(text: Any) -> list[str]:
    t = _norm(text)
    if not t or len(t) > 24:
        return []
    t = t.replace("bim ", "")
    tokens = re.split(r"[\s/,;+\-]+", t)
    tokens = [x for x in tokens if x]
    if not tokens or len(tokens) > 2:  # en-tête de colonne : « APS », « ESQ/DIAG », « BIM AVP »
        return []
    out = [_PHASE_TOKENS[tok] for tok in tokens if tok in _PHASE_TOKENS]
    return out if len(out) == len(tokens) else []


def _read_rows(path: Path, sheet: str | None) -> dict[str, list[list[Any]]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        raw = path.read_bytes()
        text = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        text = text or ""
        first = text.splitlines()[0] if text else ""
        delim = max((";", ",", "\t"), key=first.count)  # séparateur majoritaire de l'en-tête
        return {"csv": [list(r) for r in csv.reader(text.splitlines(), delimiter=delim)]}
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=False, data_only=True)
    out: dict[str, list[list[Any]]] = {}
    try:
        names = [sheet] if sheet else wb.sheetnames
        for name in names:
            if name not in wb.sheetnames:
                raise ValueError(f"Feuille {name!r} absente (feuilles : {wb.sheetnames}).")
            if not sheet and name.lower() in ("synthèse", "synthese", "lisez-moi", "_bimdata"):
                continue  # feuilles de trame des livrables BIMData MCP
            ws = wb[name]
            if ws.sheet_state != "visible" and not sheet:
                continue
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            # Cellules fusionnées : propagation **verticale** seulement (objet /
            # classe IFC fusionnés sur plusieurs lignes). Une fusion horizontale
            # est un titre de groupe (« Phases », « Classe IFC ») : la propager
            # donnerait le même en-tête à toutes les colonnes du groupe.
            for rng in ws.merged_cells.ranges:
                if rng.max_row == rng.min_row:
                    continue
                v = ws.cell(rng.min_row, rng.min_col).value
                c = rng.min_col
                for r in range(rng.min_row + 1, rng.max_row + 1):
                    if r - 1 < len(rows) and c - 1 < len(rows[r - 1]) and rows[r - 1][c - 1] is None:
                        rows[r - 1][c - 1] = v
            out[name] = rows
    finally:
        wb.close()
    return out


def _col_index(ref: str | int, headers: list[str]) -> int | None:
    if isinstance(ref, int):
        return ref
    r = str(ref).strip()
    if re.fullmatch(r"[A-Za-z]{1,3}", r):
        idx = 0
        for ch in r.upper():
            idx = idx * 26 + (ord(ch) - 64)
        return idx - 1
    for i, h in enumerate(headers):
        if _norm(h) == _norm(r):
            return i
    return None


def _detect(rows: list[list[Any]], column_map: dict | None, header_row: int | None) -> dict | None:
    width = max((len(r) for r in rows[:60]), default=0)
    candidates = [header_row - 1] if header_row else range(min(40, len(rows)))
    best = None
    for r in candidates:
        if r < 0 or r >= len(rows):
            continue
        row = rows[r] + [None] * (width - len(rows[r]))
        sub = (rows[r + 1] + [None] * width)[:width] if r + 1 < len(rows) else [None] * width
        # Colonnes de phase d'abord : elles ne doivent pas être prises pour un champ.
        phase_cols: dict[int, list[str]] = {}
        for c in range(width):
            ph = _phase_of(row[c]) or _phase_of(sub[c])
            if ph:
                phase_cols[c] = ph
        scores: list[tuple[int, str, int, bool]] = []
        for c in range(width):
            if c in phase_cols:
                continue
            for fld, syns in FIELDS.items():
                own, below = _score(row[c], syns), _score(sub[c], syns) - 1
                sc = max(own, below)
                if sc:
                    scores.append((sc, fld, c, own >= below))
        scores.sort(key=lambda t: (-t[0], t[2]))
        mapping: dict[str, int] = {}
        own_hits = 0
        used: set[int] = set()
        for sc, fld, c, is_own in scores:
            if fld in mapping or c in used or sc < 10:
                continue
            mapping[fld] = c
            used.add(c)
            own_hits += 1 if is_own else 0
        quality = (len(mapping) + own_hits + (2 if "property" in mapping else 0)
                   + (2 if "ifc_class" in mapping else 0) + (1 if phase_cols else 0))
        if ("property" in mapping or "ifc_class" in mapping) and quality >= 4:
            uses_sub = any(_phase_of(sub[c]) for c in phase_cols) or any(
                _score(sub[c], FIELDS[f]) >= 10 and _score(row[c], FIELDS[f]) < 10 for f, c in mapping.items())
            cand = {"header_row": r, "data_start": r + (2 if uses_sub else 1), "mapping": mapping,
                    "phase_cols": phase_cols, "headers": [str(x or "") for x in row], "quality": quality}
            if best is None or quality > best["quality"]:
                best = cand
    if best is None and not column_map:
        return None
    if column_map:
        base = best or {"header_row": (header_row or 1) - 1, "data_start": header_row or 1,
                        "mapping": {}, "phase_cols": {}, "quality": 0,
                        "headers": [str(x or "") for x in rows[(header_row or 1) - 1]]}
        for fld, ref in column_map.items():
            if fld == "phases_columns":
                for ref_ph, ph in (ref or {}).items():
                    idx = _col_index(ref_ph, base["headers"])
                    if idx is not None:
                        base["phase_cols"][idx] = [ph.upper()]
                continue
            if fld not in FIELDS:
                raise ValueError(f"column_map : champ inconnu {fld!r} (attendus : {sorted(FIELDS)}).")
            idx = _col_index(ref, base["headers"])
            if idx is None:
                raise ValueError(f"column_map : colonne {ref!r} introuvable.")
            base["mapping"][fld] = idx
        best = base
    return best


def _classes(text: Any) -> list[str]:
    found = re.findall(r"Ifc[A-Za-z0-9]+", str(text or ""))
    out = []
    for f in found:
        if f.lower() in {"ifcobject", "ifctypeobject", "ifcname", "ifclabel", "ifctext", "ifcproduct"}:
            continue
        if f.endswith("Type") and len(f) > 7:
            f = f[:-4]
        if f not in out:
            out.append(f)
    # IfcWall couvre déjà IfcWallStandardCase : inutile (et source de doublons) de garder les deux.
    lowered = {c.lower() for c in out}
    return [c for c in out if not any(c.lower() == b + suf for b in lowered
                                      for suf in ("standardcase", "elementedcase"))]


def attr_like(prop: str) -> bool:
    return any(a.lower() == _norm(prop).replace(" ", "") for a in ATTRIBUTE_FIELDS)


def _field(prop_raw: Any, pset_raw: Any) -> tuple[str | None, list[str], str]:
    """(chemin de champ, classes déduites, raison si rejet)."""
    prop = str(prop_raw or "").strip()
    pset = str(pset_raw or "").strip()
    if not prop:
        return None, [], "propriété vide"
    hint: list[str] = []
    m = re.match(r"^(Ifc[A-Za-z0-9]+)\s*[/.:]\s*(\w+)$", prop)
    if m:
        hint = _classes(m.group(1))
        prop = m.group(2)
    m = re.match(r"^((?:Pset|Qto|CPset|EPset)_?[\w-]*|[\w-]+)\s*[.:]\s*([\w-]+)$", prop)
    if m and not pset and m.group(1).lower().startswith(("pset", "qto", "cpset", "epset")):
        pset, prop = m.group(1), m.group(2)
    np_ = _norm(prop)
    if np_ in _MATERIAL or np_.startswith("materia"):
        return "Material", hint, ""
    if np_ in _CLASSIF:
        return "Classification", hint, ""
    if np_ == "ifcname":
        return "Name", hint, ""
    attr = next((a for a in ATTRIBUTE_FIELDS if a.lower() == np_.replace(" ", "")), None)
    if _norm(pset) in {"pset", "psets", "propriete", "proprietes", "property set", "ensemble de proprietes"}:
        pset = ""
        if not attr_like(prop):
            return f"*.{prop.strip()}", hint, ""
    if pset and _norm(pset) not in _ATTR_PSET and not _norm(pset).startswith("ifc"):
        pset_clean = pset.splitlines()[0].strip()
        return f"{pset_clean}.{prop.strip()}", hint, ""
    if attr:
        return attr, hint, ""
    if np_.replace(" ", "") in _EXTRA_ATTRS:
        return prop.replace(" ", ""), hint, ""
    if len(prop) > 60:
        return None, hint, "libellé trop long pour un nom de propriété (texte descriptif ?)"
    return f"*.{prop.strip()}", hint, ""


def _value_type(raw: Any) -> str | None:
    t = _norm(raw)
    if not t:
        return None
    for vt, keys in _TYPES.items():
        if any(k in t for k in keys):
            return vt
    return None


def _split_values(raw: Any) -> list[str] | None:
    t = str(raw or "").strip()
    if not t:
        return None
    parts = re.split(r"\s*[;\n|]\s*", t)
    if len(parts) == 1 and "," in t:
        parts = [p.strip() for p in t.split(",")]
    vals = [p.strip() for p in parts if p.strip()]
    return vals or None


def _yes(raw: Any) -> bool | None:
    t = _norm(raw)
    if not t:
        return None
    if t in _YES:
        return True
    if t in _NO:
        return False
    return True if t.startswith(("oblig", "requis", "x")) else None


def _regex(raw: Any) -> str | None:
    t = str(raw or "").strip()
    if not t:
        return None
    if re.search(r"[\^$\\\[\]{}()]", t):
        try:
            re.compile(t)
            return t
        except re.error:
            return None
    return None


def _severity(raw: Any) -> str:
    t = _norm(raw)
    if t.startswith(("bloq", "critiq", "crit", "elev", "haute", "high")):
        return "bloquant"
    if t.startswith(("mineur", "faible", "basse", "low")):
        return "mineur"
    if t.startswith("info"):
        return "info"
    return "majeur"


def parse_requirements(
    path: Path, *, sheet: str | None = None, column_map: dict | None = None,
    header_row: int | None = None, default_classes: list[str] | None = None,
) -> dict:
    sheets = _read_rows(path, sheet)
    reqs: list[Requirement] = []
    warnings: list[str] = []
    parsed_sheets: list[dict] = []
    phases_seen: set[str] = set()
    for name, rows in sheets.items():
        det = _detect(rows, column_map, header_row)
        if det is None:
            if sheet:
                raise ValueError(f"Feuille {name!r} : ligne d'en-tête non reconnue. Fournir column_map.")
            continue
        m, phase_cols = det["mapping"], det["phase_cols"]
        if "property" not in m:
            warnings.append(f"{name} : aucune colonne « propriété » détectée — feuille ignorée.")
            continue
        get = lambda row, f, _m=m: (row[_m[f]] if f in _m and _m[f] < len(row) else None)  # noqa: E731
        n_before = len(reqs)
        last_classes: list[str] = []
        last_object = None
        mode = "properties"
        skipped_docs = 0
        for i in range(det["data_start"], len(rows)):
            row = rows[i]
            if not any(c not in (None, "") for c in row):
                continue
            distinct = {_norm(c) for c in row if c not in (None, "")}
            marker = next((d for d in distinct if d in _SECTION_MARKERS), None)
            prop_marker = _norm(get(row, "property"))
            if prop_marker in _SECTION_MARKERS:
                marker = prop_marker
            if marker and (len(distinct) <= 2 or marker == prop_marker):
                mode = _SECTION_MARKERS[marker]
                continue
            if mode == "documents":
                skipped_docs += 1
                continue
            cls_cell = get(row, "ifc_class")
            classes = _classes(cls_cell)
            obj = get(row, "object")
            if classes:
                last_classes, last_object = classes, obj
            elif obj not in (None, "") and obj != last_object and "ifc_class" in m:
                last_classes, last_object = [], obj
            field, hint, why = _field(get(row, "property"), get(row, "pset"))
            if not field:
                if get(row, "property") not in (None, ""):
                    warnings.append(f"{name}!L{i + 1} : ignorée — {why}.")
                continue
            target_classes = classes or hint or last_classes or (default_classes or [])
            if not target_classes:
                warnings.append(f"{name}!L{i + 1} : « {get(row, 'property')} » sans classe IFC — ignorée.")
                continue
            phases = []
            for c, ph in phase_cols.items():
                if c < len(row) and _yes(row[c]):
                    phases += ph
            if "phases" in m:
                phases += [p for p in (_PHASE_TOKENS.get(t) for t in
                                       re.split(r"[\s,;/+]+", _norm(get(row, "phases")).replace("bim ", ""))) if p]
            phases = sorted(set(phases), key=lambda p: PHASES.index(p) if p in PHASES else 99)
            phases_seen.update(phases)
            required = _yes(get(row, "required"))
            if required is None:
                required = bool(phases) if phase_cols else True
            uniq_raw = _norm(get(row, "unique"))
            unique = None
            if uniq_raw:
                unique = ("storey" if any(k in uniq_raw for k in ("etage", "niveau", "storey")) else
                          "building" if "bat" in uniq_raw else
                          "model" if _yes(uniq_raw) else None)
            max_len = get(row, "max_length")
            try:
                max_len = int(float(str(max_len))) if max_len not in (None, "") else None
            except ValueError:
                max_len = None
            label = get(row, "label") or (get(row, "property") if "label" not in m else None)
            try:
                req = Requirement(
                    id=f"R{len(reqs) + 1:04d}",
                    label=str(label or "").strip()[:200],
                    ifc_classes=target_classes,
                    field=field,
                    required=required,
                    value_type=_value_type(get(row, "value_type")) if field not in ("Material", "Classification") else None,
                    allowed_values=_split_values(get(row, "allowed_values")),
                    pattern=_regex(get(row, "pattern")),
                    max_length=max_len,
                    unique_scope=unique,
                    phases=phases,
                    severity=_severity(get(row, "severity")) if "severity" in m else ("majeur" if required else "mineur"),
                    category="nommage" if field in ("Name", "LongName") else "conformité",
                    reference=str(get(row, "reference") or "").strip() or None,
                    example=str(get(row, "example") or "").strip()[:200] or None,
                    source=f"{name}!L{i + 1}",
                )
            except ValueError as exc:
                warnings.append(f"{name}!L{i + 1} : {exc}")
                continue
            reqs.append(req)
        if skipped_docs:
            warnings.append(f"{name} : {skipped_docs} ligne(s) de la section « Documents » ignorée(s) "
                            "(documents attendus, pas des propriétés de maquette).")
        parsed_sheets.append({
            "sheet": name, "header_row": det["header_row"] + 1, "data_start_row": det["data_start"] + 1,
            "columns": {f: det["headers"][c] if c < len(det["headers"]) and det["headers"][c] else f"col {c + 1}"
                        for f, c in m.items()},
            "phase_columns": {str(c + 1): ph for c, ph in phase_cols.items()},
            "requirements": len(reqs) - n_before,
        })
    if not reqs:
        raise ValueError(
            "Aucune exigence lue. Vérifier le fichier, ou préciser sheet / header_row / column_map "
            "(ex. {'ifc_class': 'C', 'property': 'D', 'pset': 'E'})."
        )
    # Fusion des doublons (même champ, mêmes classes) : union des phases.
    merged: dict[tuple, Requirement] = {}
    for r in reqs:
        key = (tuple(sorted(r.ifc_classes)), r.field.lower())
        if key in merged:
            m_ = merged[key]
            m_.phases = sorted(set(m_.phases) | set(r.phases))
            m_.required = m_.required or r.required
        else:
            merged[key] = r
    final = list(merged.values())
    for i, r in enumerate(final, start=1):
        r.id = f"R{i:04d}"
    return {
        "requirements": [r.model_dump() for r in final],
        "sheets": parsed_sheets,
        "warnings": warnings[:200],
        "n_warnings": len(warnings),
        "phases_available": sorted(phases_seen, key=lambda p: PHASES.index(p) if p in PHASES else 99),
    }


TEMPLATE_HEADERS = [
    "Référence", "Objet", "Classe IFC", "Pset", "Propriété", "Libellé", "Obligatoire", "Type de valeur",
    "Valeurs autorisées", "Format (regex)", "Longueur max", "Unicité", "Phases", "Criticité", "Exemple",
    "Commentaire",
]
TEMPLATE_EXAMPLES = [
    ["NOM-01", "Pièce", "IfcSpace", "", "Name", "Numéro de pièce", "Oui", "Texte", "", r"^[A-Z0-9]{2,4}-\d{3}$",
     "12", "Étage", "APS;APD;PRO;DCE;EXE;DOE", "Majeur", "R01-012", "Unique par étage"],
    ["NOM-02", "Pièce", "IfcSpace", "", "LongName", "Nom de la pièce", "Oui", "Texte",
     "BUREAU;SALLE DE REUNION;CIRCULATION;SANITAIRE;LOCAL TECHNIQUE;STOCKAGE", "", "", "", "APD;PRO;DCE;EXE;DOE",
     "Majeur", "BUREAU", ""],
    ["PRO-01", "Mur", "IfcWall", "Pset_WallCommon", "FireRating", "Degré coupe-feu", "Oui", "Texte",
     "EI30;EI60;EI90;EI120;Sans", "", "", "", "PRO;DCE;EXE;DOE", "Majeur", "EI60", ""],
    ["PRO-02", "Mur", "IfcWall", "Pset_WallCommon", "IsExternal", "Mur extérieur", "Oui", "Booléen", "", "", "",
     "", "APS;APD;PRO;DCE;EXE;DOE", "Mineur", "TRUE", ""],
    ["PRO-03", "Porte", "IfcDoor", "Pset_DoorCommon", "AcousticRating", "Affaiblissement acoustique (dB)", "Non",
     "Nombre", "", "", "", "", "PRO;DCE;EXE", "Mineur", "35", ""],
    ["GMAO-01", "Équipement CVC", "IfcFlowTerminal", "Pset_ManufacturerTypeInformation", "Manufacturer",
     "Fabricant", "Oui", "Texte", "", "", "", "", "EXE;DOE", "Majeur", "Atlantic", ""],
    ["MAT-01", "Dalle", "IfcSlab", "", "Matériau", "Matériau", "Oui", "Texte", "", "", "", "", "APD;PRO;DCE;EXE;DOE",
     "Mineur", "Béton", "Lecture seule : se corrige dans le logiciel auteur"],
]


TEMPLATE_HELP = [
    ("Référence", "Identifiant libre de l'exigence (repris dans les rapports)."),
    ("Objet", "Libellé métier de l'objet (Mur, Porte, Pièce…)."),
    ("Classe IFC", "Une ou plusieurs classes (ex. « IfcWall IfcSlab ») ; sous-types inclus."),
    ("Pset", "Pset de la propriété (ex. Pset_WallCommon). Vide = attribut IFC ou recherche dans tous les Psets."),
    ("Propriété", "Nom de la propriété ou de l'attribut (Name, LongName…) ; « Matériau » / « Classification » acceptés."),
    ("Libellé", "Libellé lisible repris dans les rapports."),
    ("Obligatoire", "Oui / Non."),
    ("Type de valeur", "Texte, Nombre, Entier, Booléen, Date."),
    ("Valeurs autorisées", "Liste séparée par « ; »."),
    ("Format (regex)", "Expression régulière que la valeur entière doit respecter."),
    ("Longueur max", "Nombre maximal de caractères."),
    ("Unicité", "Modèle, Bâtiment ou Étage : la valeur doit y être unique."),
    ("Phases", "Liste séparée par « ; » (ESQ, APS, APD, AVP, PRO, DCE, EXE, DOE, GESTION). Vide = toutes."),
    ("Criticité", "Bloquant, Majeur, Mineur, Info."),
    ("Exemple", "Valeur type."),
    ("Commentaire", "Libre."),
]


def write_template(path: Path, ctx=None) -> None:
    from ..charte import Col, Contexte, Livrable

    ctx = ctx or Contexte(type_livrable="Gabarit", titre="Gabarit de cahier des charges de données")
    lv = Livrable(path, ctx)
    lv.synthese(intro="Gabarit à compléter : une ligne par exigence (propriété attendue sur une ou plusieurs classes "
                      "IFC). Le fichier complété s'importe avec import_data_requirements.")
    lv.feuille("Exigences", [Col(h, kind="edit") for h in TEMPLATE_HEADERS], TEMPLATE_EXAMPLES, role="saisie",
               group_row=False, freeze_cols=3,
               validations={6: ["Oui", "Non"], 7: ["Texte", "Nombre", "Entier", "Booléen", "Date"],
                            11: ["Modèle", "Bâtiment", "Étage"], 13: ["Bloquant", "Majeur", "Mineur", "Info"]},
               description="Exigences à saisir (les lignes d'exemple peuvent être modifiées ou supprimées).")
    lv.lisez_moi("Gabarit de cahier des charges de données, lu sans ambiguïté par le serveur BIMData MCP.",
                 [("Colonnes", [f"{k} — {v}" for k, v in TEMPLATE_HELP])])
    lv.meta({"format": "bimdata-mcp/requirements-template/v1"})
    lv.close()
