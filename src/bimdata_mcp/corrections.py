"""Fichier corrigé → liste de changements validés → plan d'écriture.

Formats reconnus (détection automatique) :

``check-report``
    Rapport de contrôle de ce serveur, feuille « Erreurs à corriger » : lignes
    dont « Appliquer » vaut Oui et « Valeur corrigée » est renseignée.
``selection-export``
    Export de sélection de ce serveur, feuille « Éléments » modifiée : chaque
    cellule **différente de la maquette** devient un changement. Une colonne
    ajoutée « Pset.Propriété » crée la propriété.
``generic-long``
    Tout tableau avec colonnes GlobalId + Champ (ou Pset + Propriété) + Nouvelle
    valeur / Valeur corrigée.
``generic-wide``
    Tout tableau avec une colonne GlobalId et des en-têtes « Pset.Propriété » ou
    noms d'attributs IFC.

Chaque changement est revalidé contre l'état **actuel** de la maquette
(élément existant, champ inscriptible, valeur convertible, différente de
l'actuelle) ; les lignes écartées sont listées avec leur raison.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bim_core.write_plan import WritePlan, WritePlanKind

from .model_view import ATTRIBUTE_FIELDS, Record, normalize_text, resolve_field
from .plans import coerce_value
from .quality.requirements import READ_ONLY_FIELDS

WRITABLE_ATTRIBUTES = {a.lower(): a for a in ATTRIBUTE_FIELDS if a != "PredefinedType"}
_YES = {"oui", "yes", "o", "y", "x", "1", "true", "vrai", "ok"}
_CLEAR = {"<vide>", "<empty>", "<null>", "<effacer>"}
_GUID_HEADERS = {"globalid", "guid", "uuid", "ifcguid", "ifc guid", "global id"}  # noqa: E501
_LABEL_TO_PATH = {"nom": "Name", "nom long": "LongName", "type (objecttype)": "ObjectType", "type d'objet": "ObjectType",
                  "description": "Description", "tag": "Tag"}


def _read_sheet(wb, name: str) -> list[list[Any]]:
    ws = wb[name]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _meta(wb) -> dict:
    if "_bimdata" not in wb.sheetnames:
        return {}
    out = {}
    for row in wb["_bimdata"].iter_rows(min_row=2, values_only=True):
        if row and row[0]:
            v = row[1]
            try:
                v = json.loads(v) if isinstance(v, str) and v[:1] in "[{\"" else v
            except json.JSONDecodeError:
                pass
            out[str(row[0])] = v
    return out


def _hidx(headers: list[Any], *names: str) -> int | None:
    norm = [normalize_text(h) for h in headers]
    for n in names:
        nn = normalize_text(n)
        if nn in norm:
            return norm.index(nn)
    for n in names:
        nn = normalize_text(n)
        for i, h in enumerate(norm):
            if h.startswith(nn):
                return i
    return None


def _path_for_header(header: Any) -> str | None:
    h = str(header or "").strip()
    if not h:
        return None
    if normalize_text(h) in _LABEL_TO_PATH:
        return _LABEL_TO_PATH[normalize_text(h)]
    if h.lower() in WRITABLE_ATTRIBUTES:
        return WRITABLE_ATTRIBUTES[h.lower()]
    if "." in h and not h.startswith("*."):
        pset, prop = h.split(".", 1)
        if pset.strip() and prop.strip():
            return f"{pset.strip()}.{prop.strip()}"
    return None


def parse_corrections_file(path: Path) -> tuple[str, dict, list[dict]]:
    """Retourne ``(format, meta, changements bruts)``.

    Changement brut : ``{uuid, field, value, source}`` (+ ``compare`` si la
    valeur doit d'abord être comparée à la maquette).
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        meta = _meta(wb)
        fmt = str(meta.get("format") or "")
        changes: list[dict] = []
        if fmt.startswith("bimdata-mcp/check-report") or "Erreurs à corriger" in wb.sheetnames:
            rows = _read_sheet(wb, meta.get("issue_sheet") or "Erreurs à corriger")
            hrow = next((i for i, r in enumerate(rows[:4]) if _hidx(r, "UUID", "GlobalId") is not None), 0)
            h = rows[hrow]
            rows = rows[hrow:]
            iu, ifld = _hidx(h, "UUID", "GlobalId"), _hidx(h, "Champ")
            iv, ia = _hidx(h, "Valeur corrigée"), _hidx(h, "Appliquer (Oui/Non)", "Appliquer")
            iloc = _hidx(h, "Emplacement trouvé")
            if None in (iu, ifld, iv, ia):
                raise ValueError("Feuille « Erreurs à corriger » : colonnes UUID / Champ / Valeur corrigée / Appliquer introuvables.")
            for n, r in enumerate(rows[1:], start=hrow + 2):
                if not r or r[iu] in (None, ""):
                    continue
                if normalize_text(r[ia]) not in _YES:
                    continue
                fld = str(r[ifld] or "").strip()
                loc = str(r[iloc] or "").strip() if iloc is not None else ""
                if fld.startswith("*.") and "." in loc:
                    fld = loc  # exigence « tous Psets » : on écrit là où la propriété existe déjà
                changes.append({"uuid": str(r[iu]).strip(), "field": fld,
                                "value": r[iv], "source": f"Erreurs à corriger!L{n}"})
            return "check-report", meta, changes

        if isinstance(meta.get("sheets"), dict):
            return "selection-export", meta, _read_export_v2(wb, meta)
        sheet = meta.get("sheet") or ("Éléments" if "Éléments" in wb.sheetnames else wb.sheetnames[0])
        rows = _read_sheet(wb, sheet)
        hrow = next((i for i, r in enumerate(rows[:20]) if _hidx(r, *_GUID_HEADERS) is not None), None)
        if hrow is None:
            raise ValueError("Colonne GlobalId introuvable : impossible d'identifier les éléments.")
        h = rows[hrow]
        iu = _hidx(h, *_GUID_HEADERS)
        ifld = _hidx(h, "Champ", "Field")
        ipset, iprop = _hidx(h, "Pset"), _hidx(h, "Propriété", "Propriete", "Property")
        ival = _hidx(h, "Valeur corrigée", "Nouvelle valeur", "New value", "Valeur cible")
        if ival is not None and (ifld is not None or iprop is not None):
            iapply = _hidx(h, "Appliquer (Oui/Non)", "Appliquer", "Apply")
            for n, r in enumerate(rows[hrow + 1:], start=hrow + 2):
                if not r or r[iu] in (None, "") or r[ival] in (None, ""):
                    continue
                if iapply is not None and normalize_text(r[iapply]) not in _YES:
                    continue
                fld = str(r[ifld]).strip() if ifld is not None and r[ifld] else (
                    f"{r[ipset]}.{r[iprop]}" if ipset is not None and r[ipset] else str(r[iprop] or ""))
                changes.append({"uuid": str(r[iu]).strip(), "field": fld, "value": r[ival], "source": f"{sheet}!L{n}"})
            return "generic-long", meta, changes

        cols: dict[int, str] = {}
        for c, head in enumerate(h):
            if c == iu:
                continue
            p = _path_for_header(head)
            if p and p.lower() not in READ_ONLY_FIELDS and not p.startswith("*."):
                cols[c] = p
        if not cols:
            raise ValueError("Aucune colonne modifiable reconnue (attendu : Pset.Propriété ou Name/LongName/…).")
        for n, r in enumerate(rows[hrow + 1:], start=hrow + 2):
            if not r or iu >= len(r) or r[iu] in (None, ""):
                continue
            for c, p in cols.items():
                v = r[c] if c < len(r) else None
                changes.append({"uuid": str(r[iu]).strip(), "field": p, "value": v, "source": f"{sheet}!L{n}",
                                "compare": True})
        return ("selection-export" if fmt.startswith("bimdata-mcp/selection-export") else "generic-wide"), meta, changes
    finally:
        wb.close()


def _writable_path(p: str | None) -> bool:
    if not p or p.lower() in READ_ONLY_FIELDS or p.startswith("*."):
        return False
    return "." in p or p.lower() in WRITABLE_ATTRIBUTES


def _read_export_v2(wb, meta: dict) -> list[dict]:
    """Export v2 : une feuille par classe, en-tête sur 2 lignes (groupe / propriété)."""
    changes: list[dict] = []
    for sheet, paths in meta["sheets"].items():
        if sheet not in wb.sheetnames:
            continue
        rows = _read_sheet(wb, sheet)
        if len(rows) < 2:
            continue
        groups, labels = list(rows[0]), list(rows[1])
        last = None
        for i, g in enumerate(groups):  # cellules fusionnées : seul le coin haut-gauche porte la valeur
            if g not in (None, ""):
                last = g
            elif i >= len(paths):
                groups[i] = None
            else:
                groups[i] = last
        cols: dict[int, str] = {}
        for c in range(max(len(labels), len(paths))):
            if c < len(paths):
                p = paths[c]
            else:  # colonne ajoutée par l'utilisateur : Pset (ligne 1) + propriété (ligne 2)
                g = str(groups[c] or "").strip() if c < len(groups) else ""
                lab = str(labels[c] or "").strip() if c < len(labels) else ""
                p = f"{g}.{lab}" if g and lab else _path_for_header(lab)
            if _writable_path(p):
                cols[c] = p
        iu = paths.index("GlobalId") if "GlobalId" in paths else 0
        for n, r in enumerate(rows[2:], start=3):
            if not r or iu >= len(r) or r[iu] in (None, ""):
                continue
            for c, p in cols.items():
                changes.append({"uuid": str(r[iu]).strip(), "field": p, "value": r[c] if c < len(r) else None,
                                "source": f"{sheet}!L{n}", "compare": True})
    return changes


def _same(a: Any, b: Any) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        from .selection import as_bool

        if as_bool(a) is None or as_bool(b) is None:
            return str(a).strip().upper() == str(b).strip().upper()
        return as_bool(a) == as_bool(b)
    try:
        return abs(float(str(a).replace(",", ".")) - float(str(b).replace(",", "."))) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def validate_changes(raw: list[dict], records: dict[str, Record], *, value_types: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    items, rejected = [], []
    seen: dict[tuple[str, str], int] = {}
    for ch in raw:
        uuid, fld = ch["uuid"], (ch["field"] or "").strip()
        rec = records.get(uuid)
        why = None
        if rec is None:
            why = "élément introuvable dans la maquette active"
        elif not fld:
            why = "champ vide"
        elif fld.lower() in READ_ONLY_FIELDS or fld.startswith("*."):
            why = (f"champ « {fld} » non modifiable par l'API (corriger dans le logiciel auteur)"
                   if not fld.startswith("*.") else "Pset non précisé (« *.Propriété ») : préciser le Pset cible")
        if why:
            if not ch.get("compare"):
                rejected.append({**ch, "reason": why})
            continue
        if "." in fld:
            kind, pset, prop = "property", *fld.split(".", 1)
        elif fld.lower() in WRITABLE_ATTRIBUTES:
            kind, pset, prop = "attribute", "", WRITABLE_ATTRIBUTES[fld.lower()]
        else:
            rejected.append({**ch, "reason": f"champ « {fld} » non reconnu (attendu Pset.Propriété ou attribut IFC)"})
            continue
        cur = resolve_field(rec, fld if kind == "property" else prop)
        raw_val = ch["value"]
        if isinstance(raw_val, str) and raw_val.strip().lower() in _CLEAR:
            raw_val = None
        elif raw_val in (None, "") and not ch.get("compare"):
            rejected.append({**ch, "reason": "valeur corrigée vide (saisir <vide> pour effacer)"})
            continue
        if ch.get("compare") and raw_val in (None, "") and not cur.found:
            continue
        if ch.get("compare") and _same(cur.value, raw_val):
            continue  # cellule non modifiée
        vt = None
        if kind == "property":
            key = cur.location.split(":", 1)[1] if cur.location.startswith("pset:") else None
            vt = rec.prop_types.get(key) if key else None
        vt = vt or (value_types or {}).get(fld)
        try:
            new = coerce_value(raw_val, vt)
        except (TypeError, ValueError) as exc:
            rejected.append({**ch, "reason": f"valeur invalide pour le type {vt}: {exc}"})
            continue
        if _same(cur.value, new):
            if not ch.get("compare"):
                rejected.append({**ch, "reason": "valeur identique à la maquette (déjà corrigée ?)"})
            continue
        k = (uuid, fld.lower())
        item = {"uuid": uuid, "ifc_class": rec.ifc_class, "name": rec.name, "kind": kind, "pset": pset, "prop": prop,
                "field": fld, "old_value": cur.value, "new_value": new, "value_type": vt, "source": ch.get("source"),
                "creates": kind == "property" and not cur.location}
        if k in seen:
            items[seen[k]] = item
            continue
        seen[k] = len(items)
        items.append(item)
    return items, rejected


def build_plan(items: list[dict], *, target: dict, origin: str, rejected: list[dict]) -> WritePlan:
    from collections import Counter

    risks = []
    n_attr = sum(1 for i in items if i["kind"] == "attribute")
    n_clear = sum(1 for i in items if i["new_value"] is None)
    n_create = sum(1 for i in items if i["creates"])
    if n_attr:
        risks.append(f"{n_attr} attribut(s) IFC modifié(s) (Name/LongName…) : renommage visible dans tous les outils.")
    if n_clear:
        risks.append(f"{n_clear} valeur(s) seront effacées.")
    if n_create:
        risks.append(f"{n_create} propriété(s) créée(s) (absentes de la maquette).")
    if rejected:
        risks.append(f"{len(rejected)} ligne(s) écartée(s) : voir rejected.")
    risks.append("Les modifications portent sur la maquette BIMData : penser à les reporter dans le logiciel auteur "
                 "(sinon elles seront perdues au prochain export IFC).")
    return WritePlan(
        kind=WritePlanKind.PROPERTY_INJECTION,
        target=target,
        summary={
            "origin": origin,
            "changes": len(items),
            "elements": len({i["uuid"] for i in items}),
            "by_field": dict(Counter(i["field"] for i in items).most_common(20)),
            "rejected": len(rejected),
        },
        items=items,
        risks=risks,
    )
