"""Implémentations (synchrones) des outils MCP.

Chaque fonction reçoit un :class:`Call` (qui appelle, depuis quelle session)
et retourne un dict JSON-sérialisable compact. ``server.py`` ne fait que
déclarer les outils et déporter ces appels hors de la boucle d'événements.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from bim_core.write_plan import WritePlan, WritePlanKind

from . import corrections as corr
from . import exports, files, plans, publication
from .auth import Identity
from .bimdata import BIMDataClient, load_snapshot, make_client, make_navigation_client
from .charte import Contexte, nom_fichier
from .config import get_settings
from .model_view import Record, build_records, property_catalog, records_by_uuid, resolve_field
from .quality.engine import STATUS_LABELS, run_check
from .quality.report import write_check_report
from .quality.requirements import STRUCTURAL, Requirement, default_rules
from .quality.spec_parser import parse_requirements, write_template
from .selection import SelectionSpec, select, summarize
from .state import UserState, get_session_target, new_id, now_iso, set_session_target, slug
from .target import BIMDataTarget, resolve_target


@dataclass(frozen=True)
class Call:
    identity: Identity
    session_id: str

    @property
    def state(self) -> UserState:
        return UserState(self.identity)


# ── Contexte maquette ───────────────────────────────────────────────────


def _target(call: Call) -> BIMDataTarget:
    t = get_session_target(call.identity, call.session_id) or call.state.last_target()
    if t is None:
        raise ValueError("Aucune maquette active : appeler set_active_model (URL viewer BIMData ou identifiants).")
    return t


def _ctx(call: Call, *, refresh: bool = False):
    target = _target(call)
    client = make_client(target, call.identity)
    snap, source = load_snapshot(client, target, refresh=refresh)
    return target, client, snap, source


def _model_name(snap) -> str:
    return (snap.model or {}).get("name") or f"modèle {(snap.model or {}).get('id', '')}"


def _doc_ctx(call: Call, snap, target: BIMDataTarget, type_livrable: str, titre: str, *, perimetre: str = "",
             phase: str | None = None) -> Contexte:
    """Bloc d'identification commun à tous les livrables."""
    return Contexte(type_livrable=type_livrable, titre=titre, projet=(snap.project or {}).get("name") or "",
                    maquette=_model_name(snap), cible=target.to_dict(),
                    perimetre="" if perimetre in ("", "(maquette entière)") else f"Sélection « {perimetre} »",
                    phase=phase or "", auteur=call.identity.display)


def _livrable(meta: dict, *, type_livrable: str, titre: str, feuilles=None) -> dict:
    """Description normalisée d'un fichier produit (identique pour tous les outils)."""
    out = {"type": type_livrable, "titre": titre, "fichier": meta["name"], "file_id": meta["file_id"],
           "download_url": meta["download_url"], "taille_ko": max(1, meta["size"] // 1024)}
    if meta.get("chemin_local"):  # copie lisible sur le poste (dossier des rapports)
        out["chemin_local"] = meta["chemin_local"]
    if feuilles is not None:
        out["feuilles"] = feuilles
    return out


def browse(call: Call, cloud_id: int | None, project_id: int | None) -> dict:
    client = make_navigation_client(call.identity)
    s = get_settings()
    if cloud_id is None:
        clouds = client.list_clouds()
        return {"clouds": [{"cloud_id": c.get("id"), "name": c.get("name")} for c in clouds][:200],
                "next": "bimdata_browse(cloud_id=…) pour lister les projets."}
    if project_id is None:
        projects = client.list_projects(cloud_id)
        return {"cloud_id": cloud_id,
                "projects": [{"project_id": p.get("id"), "name": p.get("name"), "status": p.get("status")}
                             for p in projects][:300]}
    models = client.list_models(cloud_id, project_id)
    out = []
    for m in models:
        if (m.get("type") or "IFC").upper() not in ("IFC", "IFC2X3", "IFC4", "BIMDATA", "MERGE"):
            continue
        t = BIMDataTarget(str(cloud_id), str(project_id), str(m.get("id")))
        out.append({"model_id": m.get("id"), "name": m.get("name"), "type": m.get("type"),
                    "status": m.get("status"), "version": m.get("version"),
                    "viewer_url": t.viewer_url(s.platform_url)})
    return {"cloud_id": cloud_id, "project_id": project_id, "models": out[:300]}


def set_active_model(call: Call, bimdata_url, cloud_id, project_id, model_id, refresh: bool) -> dict:
    target = resolve_target(bimdata_url=bimdata_url, cloud_id=cloud_id, project_id=project_id, model_id=model_id)
    client = make_client(target, call.identity)
    snap, source = load_snapshot(client, target, refresh=refresh)
    set_session_target(call.identity, call.session_id, target)
    call.state.set_last_target(target, {"model_name": _model_name(snap)})
    recs = build_records(snap)
    return {
        "target": target.to_dict(),
        "model_name": _model_name(snap),
        "project_name": (snap.project or {}).get("name"),
        "viewer_url": target.viewer_url(get_settings().platform_url),
        "loaded_from": source,
        "partial": bool(snap.extraction_errors),
        "extraction_errors": snap.extraction_errors[:10],
        **model_overview_from(recs, snap),
    }


def model_overview_from(recs: list[Record], snap) -> dict:
    storeys = Counter(r.storey for r in recs if r.storey and r.is_physical)
    return {
        "n_elements": sum(1 for r in recs if r.is_physical),
        "n_spaces": sum(1 for r in recs if r.ifc_class == "IfcSpace"),
        "n_zones": sum(1 for r in recs if r.ifc_class == "IfcZone"),
        "buildings": sorted({r.name for r in recs if r.ifc_class == "IfcBuilding"}),
        "storeys": [{"name": s.get("name"), "elevation": s.get("bimdata_elevation") or s.get("elevation"),
                     "n_elements": storeys.get(s.get("name"), 0)} for s in snap.storeys][:60],
        "ifc_classes": dict(Counter(r.ifc_class for r in recs).most_common(40)),
        "top_psets": dict(Counter(k.split(".", 1)[0] for r in recs for k in r.props).most_common(25)),
    }


def model_overview(call: Call) -> dict:
    target, _, snap, source = _ctx(call)
    return {"target": target.to_dict(), "model_name": _model_name(snap), "loaded_from": source,
            **model_overview_from(build_records(snap), snap)}


def list_properties(call: Call, ifc_classes, contains, limit: int) -> dict:
    _, _, snap, _ = _ctx(call)
    recs = build_records(snap)
    if ifc_classes:
        spec = SelectionSpec(ifc_classes=ifc_classes, include_spatial=True)
        recs = select(recs, spec)
    cat = property_catalog(recs)
    if contains:
        c = contains.lower()
        cat = [x for x in cat if c in x["field"].lower()]
    return {"n_elements": len(recs), "n_fields": len(cat), "fields": cat[:limit]}


# ── Sélections ──────────────────────────────────────────────────────────


def _load_selection(call: Call, name: str, target: BIMDataTarget) -> dict:
    sel = call.state.load("selections", name)
    if not sel:
        names = [s["name"] for s in call.state.list("selections")][:30]
        raise ValueError(f"Sélection {name!r} introuvable. Sélections existantes : {names}")
    if BIMDataTarget.from_dict(sel["target"]).key != target.key:
        raise ValueError(f"La sélection {name!r} a été faite sur une autre maquette ({sel['target']}).")
    return sel


def _selection_records(call: Call, name: str | None, target, snap) -> tuple[list[Record], str]:
    recs = build_records(snap)
    if not name:
        return recs, "(maquette entière)"
    sel = _load_selection(call, name, target)
    wanted = set(sel["uuids"])
    return [r for r in recs if r.uuid in wanted], name


def _save_selection(call: Call, name: str, target, recs: list[Record], criteria: dict, description: str) -> dict:
    data = {"name": name, "target": target.to_dict(), "criteria": criteria, "description": description,
            "uuids": [r.uuid for r in recs], "created_at": now_iso(), **summarize(recs)}
    call.state.save("selections", name, data)
    return data


def _preview(recs: list[Record], n: int, fields: list[str] | None = None) -> list[dict]:
    fields = fields or ["IfcClass", "Name", "Storey"]
    return [{"GlobalId": r.uuid, **{f: resolve_field(r, f).value for f in fields}} for r in recs[:n]]


def select_elements(call: Call, name: str, criteria: SelectionSpec | None, description: str, preview: int) -> dict:
    target, _, snap, _ = _ctx(call)
    spec = criteria or SelectionSpec()
    recs = select(build_records(snap), spec)
    data = _save_selection(call, slug(name, "selection"), target, recs, spec.model_dump(exclude_none=True), description)
    return {"selection": data["name"], "count": data["count"], "by_ifc_class": data["by_ifc_class"],
            "by_storey": data["by_storey"], "preview": _preview(recs, preview),
            "hint": None if recs else "Aucun élément : vérifier les noms d'étage / classes (model_overview, list_properties)."}


def combine_selections(call: Call, name: str, operation: str, selections: list[str]) -> dict:
    target, _, snap, _ = _ctx(call)
    if len(selections) < 2 and operation != "union":
        raise ValueError("Au moins deux sélections sont nécessaires.")
    sets = [set(_load_selection(call, s, target)["uuids"]) for s in selections]
    if operation == "union":
        res = set().union(*sets)
    elif operation == "intersection":
        res = set.intersection(*sets)
    elif operation == "difference":
        res = sets[0].difference(*sets[1:])
    else:
        raise ValueError("operation : union | intersection | difference")
    recs = [r for r in build_records(snap) if r.uuid in res]
    data = _save_selection(call, slug(name), target, recs, {"operation": operation, "of": selections}, "")
    return {"selection": data["name"], "count": data["count"], "by_ifc_class": data["by_ifc_class"]}


def list_selections(call: Call) -> dict:
    return {"selections": [{k: s.get(k) for k in ("name", "count", "description", "created_at", "target", "by_ifc_class")}
                           for s in call.state.list("selections")][:100]}


def show_selection(call: Call, name: str, fields, offset: int, limit: int) -> dict:
    target, _, snap, _ = _ctx(call)
    recs, _ = _selection_records(call, name, target, snap)
    return {"selection": name, "count": len(recs), "offset": offset,
            "rows": _preview(recs[offset:], limit, fields or ["IfcClass", "Name", "ObjectType", "Storey", "Space"]),
            "next_offset": offset + limit if offset + limit < len(recs) else None}


def delete_selection(call: Call, name: str) -> dict:
    return {"deleted": call.state.delete("selections", name)}


# ── Exports ─────────────────────────────────────────────────────────────


def _publish(client: BIMDataClient, path, folder_id) -> dict:
    doc = client.upload_document(path, name=path.name, parent_id=folder_id)
    return {"bimdata_document_id": doc.get("id"), "name": doc.get("name")}


def export_selection_excel(call: Call, selection, properties, include_quantities, include_location, layout,
                           long_format, min_fill_rate, file_name, publish_to_bimdata, bimdata_folder_id) -> dict:
    target, client, snap, _ = _ctx(call)
    recs, sel_name = _selection_records(call, selection, target, snap)
    if not recs:
        raise ValueError("Sélection vide : rien à exporter.")
    st = call.state
    titre = f"Export de données — {sel_name if selection else 'maquette entière'}"
    ctx = _doc_ctx(call, snap, target, "Export de données", titre, perimetre=sel_name)
    fname = file_name or nom_fichier("EXPORT", ctx.projet, ctx.maquette, selection or "")
    path = files.new_output_path(st, fname if fname.lower().endswith(".xlsx") else fname + ".xlsx")
    sel = st.load("selections", selection) if selection else None
    info = exports.export_selection_xlsx(
        path, recs, ctx=ctx, selection_spec=(sel or {}).get("criteria"), properties=properties,
        include_quantities=include_quantities, include_location=include_location, layout=layout,
        long_format=long_format, min_fill_rate=min_fill_rate)
    meta = files.register_file(st, path, kind="export", label=titre)
    out = {"livrable": _livrable(meta, type_livrable="Export de données", titre=titre,
                                 feuilles=[s["sheet"] for s in info["sheets"]]),
           "synthese": {"elements": info["rows"], "classes_ifc": len(info["sheets"]),
                        "colonnes_max": info["n_columns_max"]},
           "detail": info["sheets"], "avertissements": [],
           "suite": ["Modifier le fichier puis prepare_corrections_from_file pour corriger la maquette."]}
    if publish_to_bimdata:
        out["bimdata"] = _publish(client, path, bimdata_folder_id)
    return out


def generate_cobie(call: Call, selection, file_name, publish_to_bimdata, bimdata_folder_id) -> dict:
    target, client, snap, _ = _ctx(call)
    uuids = None
    if selection:
        uuids = set(_load_selection(call, selection, target)["uuids"])
    st = call.state
    titre = f"COBie — {selection or 'maquette entière'}"
    ctx = _doc_ctx(call, snap, target, "COBie", titre, perimetre=selection or "")
    fname = file_name or nom_fichier("COBIE", ctx.projet, ctx.maquette, selection or "")
    path = files.new_output_path(st, fname if fname.endswith(".xlsx") else fname + ".xlsx")
    report = exports.export_cobie_xlsx(path, snap, client, uuids, ctx=ctx)
    meta = files.register_file(st, path, kind="cobie", label=titre)
    warns = list(report.get("warnings") or [])
    if report.get("system_source") == "absent":
        warns.append("Aucun système (IfcSystem) : feuille System vide.")
    out = {"livrable": _livrable(meta, type_livrable="COBie", titre=titre, feuilles=list(report.get("counts", {}))),
           "synthese": report.get("counts"), "detail": {"type_source": report.get("type_source"),
                                                        "split_sheets": report.get("split_sheets")},
           "avertissements": warns, "suite": []}
    if publish_to_bimdata:
        out["bimdata"] = _publish(client, path, bimdata_folder_id)
    return out


# ── Smart Views / BCF ───────────────────────────────────────────────────


def _names(recs: list[Record]) -> dict[str, str]:
    return {r.uuid: (r.name or r.attrs.get("ObjectType") or "") for r in recs}


def _groups_by_field(recs: list[Record], field: str, max_groups: int = 20) -> list[dict]:
    buckets: dict[str, list[str]] = {}
    for r in recs:
        v = resolve_field(r, field).value
        key = "(vide)" if v in (None, "") else str(v)
        buckets.setdefault(key, []).append(r.uuid)
    ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    groups = [{"label": k, "uuids": v} for k, v in ordered[: max_groups - 1]]
    rest = [u for _, v in ordered[max_groups - 1:] for u in v]
    if rest:
        groups.append({"label": "(autres valeurs)", "uuids": rest})
    for i, g in enumerate(groups):
        g["color"] = publication.PALETTE[i % len(publication.PALETTE)]
    return groups


def _check_groups(check: dict) -> list[dict]:
    issues = [i for i in check["issues"] if i.get("uuid")]  # anomalies de niveau maquette : sans élément
    sev_of: dict[str, str] = {}
    order = {"bloquant": 0, "majeur": 1, "mineur": 2, "info": 3}
    for i in issues:  # élément principal et éléments liés (B d'un conflit, hôte…) : la plus grave l'emporte
        for u in _issue_uuids([i]):
            prev = sev_of.get(u)
            if prev is None or order[i["severity"]] < order[prev]:
                sev_of[u] = i["severity"]
    bad = set(sev_of)
    colors = publication.STATUS_COLORS
    groups = [{"label": f"Non conforme — {s}", "color": colors[s], "uuids": [u for u, v in sev_of.items() if v == s]}
              for s in ("bloquant", "majeur", "mineur", "info")]
    groups.append({"label": "Conforme", "color": publication.STATUS_COLORS["conforme"],
                   "uuids": [u for u in check.get("checked_uuids", []) if u not in bad]})
    return [g for g in groups if g["uuids"]]


#: Au-delà de ce nombre d'éléments en anomalie (ou dès deux règles), sans filtre, on propose d'abord des sélections.
SV_CHOICE_MIN = 30
SPLIT_KEYS = {"regle": lambda i: f"{i.get('rule_id') or i.get('req_id')} {i.get('req_label') or ''}".strip(),
              "gravite": lambda i: i.get("severity") or "—", "niveau": lambda i: i.get("storey") or "(sans niveau)"}


def _sv_filter(severities, rules, types, storeys):
    from .service_bcf import issue_filter

    keep = issue_filter(severities, rules, types)
    sts = {str(x).lower() for x in storeys or []}
    return lambda i: keep(i) and (not sts or str(i.get("storey") or "").lower() in sts)


def smart_view_scope(check: dict, title: str, insight: dict | None = None) -> dict:
    """Étape de choix d'une Smart View depuis un contrôle : sélections proposées (gravité, règle, type, niveau, une
    Smart View par règle / gravité / niveau), la recommandée d'abord."""
    from .service_bcf import SEV_ORDER

    iss = [i for i in check.get("issues") or [] if i.get("status") != "exclu" and i.get("uuid")]
    n_el = lambda xs: len(set(_issue_uuids(xs)))  # noqa: E731
    base = {"check_id": check["check_id"], "title": title}
    doubtful = set((insight or {}).get("regles_douteuses") or [])
    by_sev = {s: n_el([i for i in iss if i["severity"] == s]) for s in SEV_ORDER}
    rules = Counter(i.get("rule_id") or i.get("req_id") for i in iss)
    labels = {i.get("rule_id") or i.get("req_id"): i.get("req_label") or "" for i in iss}
    types = Counter(i.get("clash_type") or i.get("cell") for i in iss if i.get("clash_type") or i.get("cell"))
    storeys = Counter(i.get("storey") or "(sans niveau)" for i in iss)
    options = []
    top = [s for s in ("bloquant", "majeur") if by_sev.get(s)]
    if top:
        args = {**base, "severities": top}
        sure = sorted(r for r in rules if r not in doubtful)
        if doubtful and sure:
            args["rules"] = sure
        sel = [i for i in iss if i["severity"] in top and (not args.get("rules") or
                                                          (i.get("rule_id") or i.get("req_id")) in args["rules"])]
        options.append({"choix": "Bloquants et majeurs" + (" des règles fiables" if args.get("rules") else ""),
                        "elements": n_el(sel), "smart_views": 1, "recommande": True, "arguments": args})
    if len(rules) > 1:
        options.append({"choix": "Une Smart View par règle", "smart_views": len(rules), "elements": n_el(iss),
                        "arguments": {**base, "split_by": "regle"}})
    if len(storeys) > 1:
        options.append({"choix": "Une Smart View par niveau", "smart_views": len(storeys), "elements": n_el(iss),
                        "arguments": {**base, "split_by": "niveau"}})
    for s in SEV_ORDER:
        if by_sev[s]:
            options.append({"choix": f"Gravité {s} seulement", "elements": by_sev[s], "smart_views": 1,
                            "arguments": {**base, "severities": [s]}})
    for r, _ in rules.most_common(8):
        options.append({"choix": f"Règle {r} {labels.get(r, '')}".strip(), "smart_views": 1,
                        "elements": n_el([i for i in iss if (i.get("rule_id") or i.get("req_id")) == r]),
                        "arguments": {**base, "rules": [r]}})
    for ty, _ in types.most_common(5):
        options.append({"choix": f"Type de conflit « {ty} »", "smart_views": 1,
                        "elements": n_el([i for i in iss if ty in (i.get("clash_type"), i.get("cell"))]),
                        "arguments": {**base, "types": [ty]}})
    options.append({"choix": "Tout le contrôle en une Smart View (conformes en vert)", "smart_views": 1,
                    "elements": len(set(_issue_uuids(iss)) | set(check.get("checked_uuids") or [])),
                    "arguments": {**base, "perimetre_valide": True}})
    lines = [f"Le contrôle « {check.get('title')} » signale {n_el(iss)} éléments en anomalie. Quelle Smart View "
             "voulez-vous ?",
             "Par gravité : " + ", ".join(f"{n} {s}" for s, n in by_sev.items() if n) + " (éléments).",
             "Par règle : " + ", ".join(f"{r} ({n})" for r, n in rules.most_common(8)) + " (anomalies).",
             "Par niveau : " + ", ".join(f"{k} ({v})" for k, v in storeys.most_common(8)) + "."]
    if types:
        lines.append("Par type de conflit : " + ", ".join(f"{k} ({v})" for k, v in types.most_common(5)) + ".")
    if doubtful:
        lines.append(f"Règles dont les résultats sont à confirmer : {', '.join(sorted(doubtful))}.")
    lines.append("Je peux aussi faire une Smart View par règle ou par niveau.")
    if options and options[0].get("recommande"):
        lines.append(f"Je recommande : {options[0]['choix'].lower()} ({options[0]['elements']} éléments).")
    return {"etape": "choix_selection_smart_view", "elements_en_anomalie": n_el(iss), "par_gravite": by_sev,
            "par_regle": dict(rules.most_common()), "par_niveau": dict(storeys.most_common()),
            "par_type": dict(types.most_common()), "regles_douteuses": sorted(doubtful), "options": options,
            "question": "\n".join(lines),
            "consigne": ("Poser la question telle quelle (choix simple parmi options, la recommandée en premier) ; "
                         "rien n'est préparé. Puis rappeler prepare_smart_view avec les arguments de l'option "
                         "choisie.")}


def prepare_smart_view(call: Call, title, selection, color, color_by, selections, colors, check_id,
                       severities=None, rules=None, types=None, storeys=None, split_by=None,
                       perimetre_valide: bool = False) -> dict:
    target, _, snap, _ = _ctx(call)
    recs_all = build_records(snap)
    names = _names(recs_all)
    st = call.state
    items = None
    if check_id:
        check = _load_check(call, check_id, target)
        filtered = bool(severities or rules or types or storeys)
        if not (filtered or split_by or perimetre_valide):
            iss = [i for i in check["issues"] if i.get("status") != "exclu" and i.get("uuid")]
            n_rules = len({i.get("rule_id") or i.get("req_id") for i in iss})
            if len(set(_issue_uuids(iss))) > SV_CHOICE_MIN or n_rules > 1:
                from .quality import insight as INS

                nm = {x.uuid: (x.ifc_class, x.attrs.get("ObjectType") or x.name) for x in recs_all}
                return smart_view_scope(check, title, INS.analyze(check, nm))
        keep = _sv_filter(severities, rules, types, storeys)
        sub = dict(check, issues=[i for i in check["issues"] if keep(i)])
        if not sub["issues"]:
            raise ValueError("Aucune anomalie pour cette sélection (gravité, règle, type, niveau).")
        if filtered or split_by:  # sélection : seulement les éléments en anomalie (les conformes n'ont pas de sens)
            sub["checked_uuids"] = []
        if split_by:
            if split_by not in SPLIT_KEYS:
                raise ValueError(f"split_by : {', '.join(SPLIT_KEYS)}.")
            parts: dict[str, list] = {}
            for i in sub["issues"]:
                parts.setdefault(SPLIT_KEYS[split_by](i), []).append(i)
            items, legend = [], []
            for val, iss in sorted(parts.items(), key=lambda kv: -len(kv[1])):
                groups = _check_groups(dict(sub, issues=iss))
                if not groups:
                    continue
                items.append({"payload": publication.smartview_groups(groups, title=f"{title} — {val}",
                                                                       model_id=target.model_id, names=names),
                              "title": f"{title} — {val}"})
                legend.append({"label": val, "count": sum(len(g["uuids"]) for g in groups)})
        else:
            groups = _check_groups(sub)
            payload = publication.smartview_groups(groups, title=title, model_id=target.model_id, names=names)
            legend = [{"label": g["label"], "color": g["color"], "count": len(g["uuids"])} for g in groups]
    elif selections:
        groups = []
        for i, sname in enumerate(selections):
            sel = _load_selection(call, sname, target)
            c = (colors or [])[i] if colors and i < len(colors) else publication.PALETTE[i % len(publication.PALETTE)]
            groups.append({"label": sname, "color": c, "uuids": sel["uuids"]})
        payload = publication.smartview_groups(groups, title=title, model_id=target.model_id, names=names)
        legend = [{"label": g["label"], "color": g["color"], "count": len(g["uuids"])} for g in groups]
    else:
        recs, _ = _selection_records(call, selection, target, snap)
        if not recs:
            raise ValueError("Sélection vide.")
        if color_by:
            groups = _groups_by_field(recs, color_by)
            payload = publication.smartview_groups(groups, title=title, model_id=target.model_id, names=names)
            legend = [{"label": g["label"], "color": g["color"], "count": len(g["uuids"])} for g in groups]
        else:
            payload = publication.smartview_single([r.uuid for r in recs], title=title, color=color,
                                                   model_id=target.model_id, names=names)
            legend = [{"label": selection or "maquette", "color": color, "count": len(recs)}]
    items = items or [{"payload": payload}]
    n = len(items)
    plan = WritePlan(kind=WritePlanKind.SMART_VIEWS, target=target.to_dict(),
                     summary={"title": title, "smart_views": n, "groups": len(legend), "legend": legend},
                     items=items,
                     risks=[f"Crée {n} Smart View{'s' if n > 1 else ''} visible{'s' if n > 1 else ''} par les "
                            "membres du projet BIMData."])
    return plans.save_plan(st, plan)


def _issue_uuids(iss: list[dict]) -> list[str]:
    """Éléments d'anomalies : l'élément principal et tous les éléments liés (A et B d'un conflit, porte et hôte…)."""
    out: list[str] = []
    for i in iss:
        for u in [i.get("uuid")] + [e.get("uuid") for e in i.get("elements") or []]:
            if u and u not in out:
                out.append(u)
    return out


def _union_bbox(iss: list[dict]) -> list[float] | None:
    boxes = [i["bbox"] for i in iss if i.get("bbox") and len(i["bbox"]) == 6]
    if not boxes:
        return None
    return [min(b[k] for b in boxes) for k in range(3)] + [max(b[k] for b in boxes) for k in range(3, 6)]


#: Au-delà, la préparation des topics avec images part en tâche de fond (délai d'attente du client MCP).
BCF_SYNC_MAX_TOPICS = 12
#: Au-delà, l'envoi d'un plan BCF part en tâche de fond, avec plusieurs envois simultanés.
BCF_SYNC_MAX_SEND = 10
#: Plans en cours d'envoi (un seul processus serveur) → job_id.
_SENDING: dict[str, str] = {}


def prepare_bcf_topics(call: Call, title, selection, description, priority, topic_type, labels, assigned_to,
                       due_date, group_by, one_topic_per_element, check_id, check_group_by, max_topics,
                       frame_bbox: bool = True, isolate: bool = False, section_box=None, snapshot: bool = True,
                       camera: str = "auto", field_of_view_deg: float = 60.0, extra_viewpoint: str = "none",
                       max_components: int = 1000, dedupe: bool = True, severities=None, rules=None, types=None,
                       perimetre_valide: bool = False) -> dict:
    if check_id:  # point de vue complet par anomalie (caméra, couleurs, coupe, image), clé stable
        from functools import partial

        from . import jobs
        from .service_bcf import (
            BCF_CHOICE_MIN,
            _group_key,
            bcf_scope,
            issue_filter,
            prepare_check_topics,
        )

        run = partial(
            prepare_check_topics, call, check_id=check_id, title=title, check_group_by=check_group_by,
            max_topics=max_topics, frame_bbox=frame_bbox, isolate=isolate, section_box=section_box, snapshot=snapshot,
            camera=camera, field_of_view_deg=field_of_view_deg, extra_viewpoint=extra_viewpoint,
            max_components=max_components, dedupe=dedupe, labels=labels, assigned_to=assigned_to, due_date=due_date,
            topic_type=topic_type, severities=severities, rules=rules, types=types)
        target = _target(call)
        check = _load_check(call, check_id, target)
        keep = issue_filter(severities, rules, types)
        n = len({_group_key(i, check_group_by) for i in check["issues"] if keep(i)})
        if n > BCF_CHOICE_MIN and not (severities or rules or types or perimetre_valide):
            # trop de topics sans filtre : proposer de choisir par gravité, règle ou type avant de rien préparer
            from .quality import insight as INS

            _, _, snap, _ = _ctx(call)
            names = {x.uuid: (x.ifc_class, x.attrs.get("ObjectType") or x.name) for x in build_records(snap)}
            return bcf_scope(check, check_group_by, INS.analyze(check, names), title=title)
        if snapshot and n > BCF_SYNC_MAX_TOPICS:  # images de nombreux topics : tâche de fond, réponse immédiate
            job = jobs.start(call.state, kind="prepare_bcf_topics", label=f"{n} topics BCF ({check.get('title')})",
                             total=None, estimate_s=round(1.5 + 0.6 * n),
                             fn=lambda progress: run(progress=progress))
            return {**job, "topics_estimes": n,
                    "consigne": "Préparation lancée en tâche de fond (images de chaque point de vue). Interroger "
                                "job_status(job_id) toutes les 20 à 30 s et donner l'avancement ; à la fin, le "
                                "résultat contient le plan_id à montrer (show_plan). Rien n'est écrit dans BIMData."}
        return run()
    title = title or "Topic"
    topic_type = topic_type or "Issue"
    target, _, snap, _ = _ctx(call)
    st = call.state
    items: list[dict] = []
    recs, sel_name = _selection_records(call, selection, target, snap)
    if not recs:
        raise ValueError("Sélection vide.")
    if one_topic_per_element:
        for r in recs:
            items.append({"payload": publication.bcf_topic(
                [r.uuid], title=f"{title} — {r.ifc_class} {r.name}".strip(), description=description,
                priority=priority, topic_type=topic_type, labels=labels, assigned_to=assigned_to,
                due_date=due_date, model_id=target.model_id)})
    elif group_by:
        for g in _groups_by_field(recs, group_by, max_groups=max_topics):
            items.append({"payload": publication.bcf_topic(
                g["uuids"], title=f"{title} — {group_by} = {g['label']}",
                description=(description + "\n\n" if description else "") + f"{len(g['uuids'])} élément(s).",
                priority=priority, topic_type=topic_type, labels=labels, assigned_to=assigned_to,
                due_date=due_date, color=g["color"], model_id=target.model_id)})
    else:
        desc = description or f"{len(recs)} élément(s) — sélection « {sel_name} »."
        items.append({"payload": publication.bcf_topic(
            [r.uuid for r in recs], title=title, description=desc, priority=priority, topic_type=topic_type,
            labels=labels, assigned_to=assigned_to, due_date=due_date, model_id=target.model_id)})
    if len(items) > max_topics:
        raise ValueError(f"{len(items)} topics seraient créés (> max_topics={max_topics}). Regrouper (group_by / check_group_by) ou augmenter max_topics.")
    plan = WritePlan(kind=WritePlanKind.BCF_TOPICS, target=target.to_dict(),
                     summary={"topics": len(items), "title": title},
                     items=items, risks=[f"Crée {len(items)} topic(s) BCF dans le projet (visibles par l'équipe)."])
    return plans.save_plan(st, plan)


def _target_records(call: Call, selection, uuids) -> tuple:
    """Éléments visés par une écriture : sélection nommée ou liste de GlobalId."""
    if not selection and not uuids:
        raise ValueError("Préciser les éléments : selection (nom) ou uuids (GlobalId).")
    target, _, snap, _ = _ctx(call)
    if selection:
        recs, _ = _selection_records(call, selection, target, snap)
    else:
        by_uuid = records_by_uuid(snap)
        missing = [u for u in uuids if u not in by_uuid]
        if missing:
            raise ValueError(f"GlobalId introuvables dans la maquette : {missing[:10]}")
        recs = [by_uuid[u] for u in dict.fromkeys(uuids)]
    if not recs:
        raise ValueError("Sélection vide.")
    return target, recs


def prepare_classification_removal(call: Call, selection, uuids, system, notation) -> dict:
    """Plan : retirer ``system:notation`` (ou tous les codes de ``system``) des éléments, sans rien ajouter."""
    system, notation = (system or "").strip(), (notation or "").strip()
    if not system:
        raise ValueError("system (ex. 'uniformat') est obligatoire ; notation facultative (sans elle : "
                         "tous les codes de ce système sont retirés).")
    target, recs = _target_records(call, selection, uuids)
    prefix = system.lower() + ":"

    def matching(r) -> list[str]:
        return [c for c in r.classifications if c.lower().startswith(prefix)
                and (not notation or c.partition(":")[2].strip() == notation)]

    todo = [(r, matching(r)) for r in recs]
    todo = [(r, m) for r, m in todo if m]
    label = f"{system}:{notation}" if notation else f"tous les codes {system}"
    if not todo:
        raise ValueError(f"Aucun des {len(recs)} éléments ne porte {label} : rien à retirer.")
    removed = Counter(c for _, m in todo for c in m)
    items = [{"uuid": r.uuid, "name": r.name, "ifc_class": r.ifc_class, "system": system, "notation": notation,
              "remove_only": True, "current": " | ".join(r.classifications), "removed": " | ".join(m)}
             for r, m in todo]
    risks = [f"Retire {label} de {len(items)} élément(s) : "
             + ", ".join(f"{c} ({n})" for c, n in removed.most_common(5))
             + ". Aucun code n'est ajouté ; le code reste disponible dans le projet.",
             "À reporter aussi dans le logiciel auteur (sur le type, dans Revit) : sinon réapparaît au prochain export IFC."]
    plan = WritePlan(kind=WritePlanKind.CLASSIFICATION_UPDATE, target=target.to_dict(),
                     summary={"retrait": label, "elements": len(items), "non_concernes": len(recs) - len(items),
                              "codes_retires": dict(removed)},
                     items=items, risks=risks)
    return plans.save_plan(call.state, plan)


def prepare_classification_update(call: Call, selection, uuids, system, notation, title, replace_existing) -> dict:
    """Plan : affecter le code ``system:notation`` aux éléments demandés (sélection ou GlobalId)."""
    system, notation = (system or "").strip(), (notation or "").strip()
    if not system or not notation:
        raise ValueError("system (ex. 'uniformat') et notation (ex. 'D5010200') sont obligatoires.")
    target, recs = _target_records(call, selection, uuids)
    code = f"{system}:{notation}"
    todo = [r for r in recs if code.lower() not in {c.lower() for c in r.classifications}]
    if not todo:
        raise ValueError(f"Les {len(recs)} éléments portent déjà {code} : rien à écrire.")
    replaced = Counter(c for r in todo for c in r.classifications
                       if replace_existing and c.lower().startswith(system.lower() + ":"))
    items = [{"uuid": r.uuid, "name": r.name, "ifc_class": r.ifc_class, "system": system, "notation": notation,
              "title": title or "", "replace_existing": replace_existing, "current": " | ".join(r.classifications)}
             for r in todo]
    risks = [f"Associe {code} à {len(items)} élément(s) (le code est créé dans la maquette s'il n'existe pas)."]
    if replaced:
        risks.append("Retire l'ancien code du même système : "
                     + ", ".join(f"{c} ({n})" for c, n in replaced.most_common(5)) + ".")
    risks.append("À reporter aussi dans le logiciel auteur (sur le type, dans Revit) : sinon écrasé au prochain export IFC.")
    plan = WritePlan(kind=WritePlanKind.CLASSIFICATION_UPDATE, target=target.to_dict(),
                     summary={"code": code, "title": title or "", "elements": len(items),
                              "deja_classes": len(recs) - len(todo), "codes_remplaces": dict(replaced)},
                     items=items, risks=risks)
    return plans.save_plan(call.state, plan)


def apply_plan(call: Call, plan_id: str, confirm: bool) -> dict:
    if not confirm:
        return {"applied": False, **plans.plan_preview(plans.load_plan(call.state, plan_id)),
                "message": "Rien n'a été écrit. Relancer avec confirm=True après validation par l'utilisateur."}
    target = _target(call)
    client = make_client(target, call.identity)
    data = plans.load_plan(call.state, plan_id)
    if data["kind"] == "ged_documents":
        return _apply_ged(call, client, target, data)
    todo = sum(1 for i in data["items"] if not i.get("_done"))
    if plan_id in _SENDING:  # un envoi en cours sur ce plan : pas de second envoi en parallèle (doublons)
        raise ValueError(f"Envoi déjà en cours pour ce plan (job_status('{_SENDING[plan_id]}')).")
    if data["kind"] == WritePlanKind.BCF_TOPICS.value and todo > BCF_SYNC_MAX_SEND:
        from . import jobs

        def run(progress):
            try:
                r = plans.apply_plan(call.state, client, target, plan_id, progress)
                r["viewer_url"] = target.viewer_url(get_settings().platform_url)
                return _with_report_offer(r, plan_id)
            finally:
                _SENDING.pop(plan_id, None)

        _SENDING[plan_id] = "…"
        try:
            job = jobs.start(call.state, kind="apply_plan", label=f"Envoi de {todo} topic(s) BCF", total=todo,
                             estimate_s=round(2 + 0.8 * todo), fn=run)
        except Exception:
            _SENDING.pop(plan_id, None)
            raise
        if plan_id in _SENDING:
            _SENDING[plan_id] = job["job_id"]
        return {**job, "applied": "en_cours",
                "consigne": "Envoi lancé en tâche de fond (plusieurs topics à la fois). Interroger job_status(job_id) "
                            "toutes les 20 à 30 s et donner l'avancement. En cas d'interruption, relancer "
                            "apply_plan(plan_id, confirm=True) : seuls les topics non envoyés repartent, sans doublon."}
    result = plans.apply_plan(call.state, client, target, plan_id)
    result["viewer_url"] = target.viewer_url(get_settings().platform_url)
    return _with_report_offer(result, plan_id) if data["kind"] == WritePlanKind.BCF_TOPICS.value else result


def _apply_ged(call: Call, client, target, data: dict) -> dict:
    from . import ged
    from .target import BIMDataTarget

    if not get_settings().allow_writes:
        raise PermissionError("Écritures désactivées sur ce serveur (MCP_ALLOW_WRITES=false).")
    if data.get("status") == "applied":
        raise ValueError("Ce plan a déjà été appliqué. Préparer un nouveau plan si nécessaire.")
    if BIMDataTarget.from_dict(data["target"]).key != target.key:
        raise ValueError("Le plan vise une autre maquette que la maquette active : refusé.")
    st = call.state
    res = ged.apply(client, data, save=lambda: st.save("plans", data["plan_id"], data, gz=True))
    data["status"] = "applied" if not res["failed"] else "partially_applied"
    data["result"] = res
    st.save("plans", data["plan_id"], data, gz=True)
    st.journal("apply_plan", plan_id=data["plan_id"], kind=ged.KIND, target=data["target"],
               succeeded=res["succeeded"], failed=res["failed"])
    res["message"] = (f"Documents déposés dans la GED BIMData, dossier « {res['dossier']} »." if not res["failed"]
                      else "Dépôt partiel : relancer apply_plan(plan_id, confirm=True) pour reprendre le reste.")
    return res


def _with_report_offer(result: dict, plan_id: str) -> dict:
    """Après l'envoi de topics BCF : proposer le rapport Excel des topics (rien n'est produit sans accord)."""
    from .service_bcf import report_offer

    n = sum(1 for c in result.get("created") or [] if c.get("action") in ("create", "comment", "resolve"))
    if n:
        result["proposition_rapport"] = report_offer(plan_id, n)
    return result


def export_bcf_report(call: Call, plan_id: str, title) -> dict:
    from .service_bcf import export_bcf_report as run

    return run(call, plan_id, title)


def job_status(call: Call, job_id: str) -> dict:
    from . import jobs

    out = jobs.status(call.state, job_id)
    if out["status"] == "en_cours":
        out["consigne"] = "Toujours en cours : réinterroger dans 20 à 30 s et donner l'avancement (done / total)."
    elif out["status"] == "termine":
        out["consigne"] = ("Terminé : présenter le résultat (pour une préparation, le plan à montrer avec show_plan ; "
                           "pour un envoi, les topics créés et en échec).")
    return out


def list_plans(call: Call, limit: int) -> dict:
    return {"plans": [{"plan_id": p["plan_id"], "kind": p["kind"], "status": p.get("status"), "created_at": p["created_at"],
                       "summary": p["summary"], "n_items": len(p["items"])} for p in call.state.list("plans")[:limit]]}


def show_plan(call: Call, plan_id: str, offset: int, limit: int) -> dict:
    p = plans.load_plan(call.state, plan_id)
    shown = p["items"][offset: offset + limit]
    items = []
    for it in shown:
        d = plans._short(it)
        if it.get("snapshot_files"):  # miniatures : ce qui partira, avant l'accord pour apply_plan
            d["images"] = [files.download_url(call.state, f["file_id"], f["name"]) for f in it["snapshot_files"]]
        items.append(d)
    return {**plans.plan_preview(p), "items": items,
            "offset": offset, "result": p.get("result"), "rejected": p.get("rejected", [])[:30]}


# ── Contrôles ───────────────────────────────────────────────────────────


def _load_check(call: Call, check_id: str, target: BIMDataTarget) -> dict:
    c = call.state.load("checks", check_id)
    if not c:
        raise ValueError(f"Contrôle {check_id!r} introuvable (voir list_checks).")
    if BIMDataTarget.from_dict(c["target"]).key != target.key:
        raise ValueError("Ce contrôle porte sur une autre maquette que la maquette active.")
    return c


def write_check_pdf(call: Call, check_id: str, *, snap=None, target=None, expert: dict | None = None) -> dict:
    """(Ré)écrit le rapport PDF d'un contrôle depuis son enregistrement : encart « L'essentiel » en tête (analyse
    d'expert si fournie, sinon indicateurs automatiques de fiabilité), puis le rapport complet. Renvoie le livrable
    (PDF + annexe Excel)."""
    from .quality import insight as INS
    from .report_pdf import AuditSection, pdf_name, write_audit_pdf

    st = call.state
    if snap is None or target is None:
        target, _, snap, _ = _ctx(call)
    c = st.load("checks", check_id) or {}
    r = c.get("report")
    if not r:
        raise ValueError("Ce contrôle a été produit par une version précédente : relancer le contrôle.")
    names = {x.uuid: (x.ifc_class, x.attrs.get("ObjectType") or x.name) for x in build_records(snap)}
    auto = INS.analyze(c, names)
    ctx = _doc_ctx(call, snap, target, r["type_livrable"], r["title"], perimetre=r.get("perimetre") or "",
                   phase=c.get("phase"))
    m = snap.model or {}
    doc = m.get("document") or {}
    pdf = files.new_output_path(st, pdf_name(r["excel_name"]))  # dossier propre : file_id distinct de l'Excel
    write_audit_pdf(pdf, ctx, title=r["title"], skipped=[], excel_name=r["excel_name"],
                    model_info={"fichier": doc.get("file_name") or m.get("name"),
                                "date": str(doc.get("created_at") or "")[:10] or None, "schema": m.get("version")},
                    audits=[AuditSection(id=r.get("audit_id") or "", label=r["title"], check_id=check_id,
                                         rules=c.get("per_requirement") or [], issues=c.get("issues") or [],
                                         notes=list(c.get("notes") or []), hypotheses=r.get("hypotheses") or [],
                                         references=r.get("references") or [])],
                    expert=expert or c.get("expert_analysis"), insight=auto)
    pmeta = files.register_file(st, pdf, kind="check-report", label=r["title"])
    excel = files.get_file(st, r["excel_file_id"])
    emeta = json.loads((excel.parent / "meta.json").read_text())
    emeta["download_url"] = files.download_url(st, emeta["file_id"], emeta["name"])
    c["report"]["pdf_file_id"] = pmeta["file_id"]
    if expert:
        c["expert_analysis"] = expert
    st.save("checks", check_id, c, gz=True)
    lv = {**_livrable(pmeta, type_livrable=f"{r['type_livrable']} (PDF)", titre=r["title"]),
          "annexe_excel": _livrable(emeta, type_livrable=r.get("excel_label") or "Annexe Excel", titre=r["title"],
                                    feuilles=r.get("excel_sheets"))}
    return {"livrable": lv, "analyse_automatique": auto}


def annotate_audit_report(call: Call, check_id: str, analyse: dict) -> dict:
    """Intègre l'analyse d'expert en tête du rapport PDF (nouvelle version du PDF ; l'Excel reste l'annexe)."""
    if not any(analyse.get(k) for k in ("a_retenir", "fiabilite", "prerequis", "recommandation")):
        raise ValueError("Analyse vide : renseigner au moins a_retenir, fiabilite, prerequis ou recommandation.")
    out = write_check_pdf(call, check_id, expert=analyse)
    _, _, snap, _ = _ctx(call)
    return {"check_id": check_id, **out, "proposition_ged": ged_offer(check_id, snap),
            "message": "Rapport PDF mis à jour : l'analyse figure en tête, dans l'encart « L'essentiel »."}


# ── GED BIMData : publication des livrables ─────────────────────────────

GED_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".docx", ".csv", ".ifc", ".bcf", ".bcfzip", ".png", ".jpg", ".jpeg", ".zip")


def ged_offer(check_id: str, snap) -> dict:
    """Après un audit : proposer de déposer le rapport PDF et l'Excel dans la GED, dossier par défaut nommé."""
    from . import ged

    folder = ged.default_folder(_model_name(snap))
    return {"dossier_par_defaut": folder, "check_id": check_id,
            "appel": {"outil": "prepare_ged_publication", "arguments": {"check_id": check_id, "folder": folder}},
            "question": (f"Voulez-vous déposer le rapport PDF et le classeur Excel dans la GED du projet BIMData, "
                         f"dans le dossier « {folder} » ? Si vous préférez un autre dossier (existant ou à créer, "
                         "ex. « 03 - Synthèse/Audits »), indiquez son nom.")}


def list_ged_folders(call: Call) -> dict:
    client = make_client(_target(call), call.identity)
    folders = client.list_folders()
    by_id = {f["id"]: f for f in folders if f.get("id")}

    def path(f, depth=0):
        p = by_id.get(f.get("parent_id"))
        return (path(p, depth + 1) + "/" if p and p.get("parent_id") and depth < 20 else "") + str(f.get("name"))

    root = client.root_folder_id()
    rows = sorted({path(f) for f in folders if f.get("id") != root})
    return {"dossiers": rows[:300], "nombre": len(rows)}


def prepare_ged_publication(call: Call, check_id, file_ids, file_paths, folder) -> dict:
    """Plan de dépôt dans la GED : livrables d'un audit (PDF + Excel), fichiers produits par le serveur ou fichiers
    locaux ; dossier (chemin « A/B ») existant ou créé. N'écrit rien."""
    from . import ged

    st = call.state
    target, _, snap, _ = _ctx(call)
    docs: list[dict] = []
    if check_id:
        c = _load_check(call, check_id, target)
        r = c.get("report") or {}
        for key, typ in (("pdf_file_id", "Rapport PDF"), ("excel_file_id", "Annexe Excel")):
            if r.get(key):
                p = files.get_file(st, r[key])
                docs.append({"name": p.name, "file_id": r[key], "path": str(p), "type": typ})
        if not docs:
            raise ValueError("Ce contrôle n'a pas de livrables enregistrés : relancer le contrôle.")
    for fid in file_ids or []:
        p = files.get_file(st, fid)
        docs.append({"name": p.name, "file_id": fid, "path": str(p), "type": p.suffix[1:].upper()})
    for fp in file_paths or []:
        if files.http_links_enabled():
            raise ValueError("Chemin local refusé : le serveur MCP ne tourne pas sur votre poste (file_ids).")
        p = Path(fp).expanduser()
        if not p.is_file() or p.suffix.lower() not in GED_EXTENSIONS:
            raise ValueError(f"Fichier introuvable ou format non pris en charge : {p}")
        if p.stat().st_size > get_settings().max_upload_mb * 2**20:
            raise ValueError(f"Fichier trop volumineux : {p.name}")
        docs.append({"name": p.name, "file_id": None, "path": str(p), "type": p.suffix[1:].upper()})
    if not docs:
        raise ValueError("Aucun document : check_id, file_ids ou file_paths.")
    folder = folder or ged.default_folder(_model_name(snap))
    client = make_client(target, call.identity)
    folders = client.list_folders()
    root = client.root_folder_id()
    documents = client.list_documents() or []
    data = ged.build_plan(target.to_dict(), folder, docs, folders, documents, root_id=root)
    if data["summary"]["parent_id"] is None and root:
        data["summary"]["parent_id"] = root
    st.save("plans", data["plan_id"], data, gz=True)
    return {**plans.plan_preview(data), "dossier": data["summary"]["dossier"],
            "dossier_existant": data["summary"]["dossier_existant"],
            "documents": data["summary"]["documents"], "deja_presents": data["summary"]["deja_presents"]}


def _run_and_report(call: Call, reqs: list[Requirement], *, selection, phase, title, requirements_id,
                    auto_fill_threshold: float = 0.8, runner=None, prefix: str = "CONTROLE",
                    type_livrable: str = "Contrôle des données", avertissements=None, suite=None,
                    extra=None, audit_id: str | None = None, hypotheses=None, references=None,
                    extra_sheet=None) -> dict:
    target, _, snap, _ = _ctx(call)
    recs, sel_name = _selection_records(call, selection, target, snap)
    result = runner(recs) if runner else run_check(recs, reqs, phase=phase)
    check_id = new_id("chk")
    st = call.state
    ctx = _doc_ctx(call, snap, target, type_livrable, title, perimetre=sel_name if selection else "",
                   phase=phase)
    path = files.new_output_path(st, nom_fichier(prefix, ctx.projet, ctx.maquette, phase or ""))
    write_check_report(path, result, [r for r in reqs if r.applies_to_phase(phase)], records_by_uuid(snap),
                       check_id=check_id, ctx=ctx, requirements_id=requirements_id,
                       auto_fill_threshold=auto_fill_threshold, extra_sheet=extra_sheet)
    meta = files.register_file(st, path, kind="check-report", label=title)
    aid = audit_id or ("c3_informations" if requirements_id else
                       ("classifications" if prefix == "AUDIT-CLASSIF" else "nommage"))
    summary = result.summary()
    sheets = ["Synthèse", "Erreurs à corriger", "Données contrôlées", "Exigences"]
    st.save("checks", check_id, {
        "check_id": check_id, "title": title, "target": target.to_dict(), "requirements_id": requirements_id,
        "phase": phase, "selection": sel_name, "created_at": now_iso(), "summary": summary,
        "per_requirement": result.per_requirement, "issues": [i.to_dict() for i in result.issues],
        "checked_uuids": result.checked_uuids, "report_file_id": meta["file_id"],
        "value_types": {r.field: r.value_type for r in reqs if r.value_type}, "notes": list(avertissements or []),
        "report": {"audit_id": aid, "title": title, "type_livrable": type_livrable, "excel_name": path.name,
                   "excel_file_id": meta["file_id"], "perimetre": sel_name if selection else "",
                   "hypotheses": list(hypotheses or []), "references": list(references or []),
                   "excel_label": "Annexe Excel (à compléter pour corriger)", "excel_sheets": sheets},
    }, gz=True)
    # rapport PDF (livrable principal) ; le classeur Excel, annexe à compléter, sert aux corrections
    pdf_out = write_check_pdf(call, check_id, snap=snap, target=target)
    worst = sorted([p for p in result.per_requirement if p["non_conformes"]], key=lambda p: -p["non_conformes"])[:12]
    return {
        "livrable": pdf_out["livrable"], "analyse_automatique": pdf_out["analyse_automatique"],
        "proposition_ged": ged_offer(check_id, snap),
        "check_id": check_id, "selection": sel_name, "phase": phase, "summary": summary,
        "synthese": {"elements_controles": summary["elements_controlled"], "controles": summary["checks"],
                     "non_conformites": summary["non_conformities"], "taux_conformite": summary["conformity_rate"],
                     "propositions_fiables": summary["proposals_high_confidence"]},
        "worst_requirements": [{k: p[k] for k in ("req_id", "label", "ifc_classes", "field", "non_conformes", "applicable")}
                               for p in worst],
        "sample_issues": [{k: i.to_dict()[k] for k in ("issue_id", "ifc_class", "name", "field", "status", "actual",
                                                       "proposal", "confidence")} for i in result.issues[:10]],
        **(extra or {}),
        "avertissements": list(avertissements or []),
        "suite": list(suite or []) + [
            "get_check_issues(check_id, …) pour détailler les anomalies",
            "prepare_corrections_from_check(check_id) pour appliquer directement les propositions fiables",
            "ou : compléter le rapport Excel puis prepare_corrections_from_file",
            "proposer seulement, sans le préparer : « Je peux créer une Smart View / un sujet BCF pour ces éléments — voulez-vous ? »",
        ],
    }


def check_naming_completeness(call: Call, selection, custom_rules, include_default_rules, include_default_names_rule,
                              include_classification, phase, title) -> dict:
    reqs: list[Requirement] = []
    if include_default_rules:
        reqs += default_rules(include_classification=include_classification,
                              include_default_names=include_default_names_rule)
    for i, r in enumerate(custom_rules or [], start=1):
        reqs.append(r if r.id else r.model_copy(update={"id": f"U{i:03d}"}))
    if not reqs:
        raise ValueError("Aucune règle : activer include_default_rules ou fournir custom_rules.")
    return _run_and_report(call, reqs, selection=selection, phase=phase,
                           title=title or "Contrôle nommage et complétude", requirements_id=None)


#: Classes dont la résistance au feu est attendue dans une maquette exploitable (information seulement).
_FIRE_CLASSES = ["IfcWall", "IfcDoor", "IfcSlab"]


def general_rules() -> list[Requirement]:
    """Audit général : santé de la maquette par les données (sans géométrie ni référentiel)."""
    R = Requirement
    return [
        R(id="M01", label="Bâtiment nommé", field="Name", ifc_classes=["IfcBuilding"], category="nommage",
          severity="bloquant"),
        R(id="M02", label="Niveaux nommés et uniques", field="Name", ifc_classes=["IfcBuildingStorey"],
          category="nommage", severity="bloquant", unique_scope="building"),
        R(id="M03", label="Élément rattaché à un niveau", field="Storey", category="complétude", severity="majeur"),
        R(id="M04", label="Objet typé dans sa classe IFC (pas d'objet générique)", field="IfcClass",
          ifc_classes=["IfcBuildingElementProxy"], category="conformité", severity="mineur", required=False,
          forbidden_patterns=[r"^IfcBuildingElementProxy$"]),
        R(id="M05", label="Élément nommé", field="Name", category="nommage", severity="majeur"),
        R(id="M06", label="Type renseigné (ObjectType)", field="ObjectType", category="complétude", severity="mineur"),
        R(id="M07", label="Matériau renseigné (éléments d'ouvrage)", field="Material", ifc_classes=STRUCTURAL,
          category="complétude", severity="mineur"),
        R(id="M08", label="Paroi extérieure / intérieure (IsExternal)", field="*.IsExternal",
          ifc_classes=["IfcWall", "IfcDoor", "IfcWindow", "IfcSlab", "IfcRoof"], category="complétude",
          severity="mineur", value_type="boolean"),
        R(id="M09", label="Porteur / non porteur (LoadBearing)", field="*.LoadBearing",
          ifc_classes=["IfcWall", "IfcSlab", "IfcColumn", "IfcBeam"], category="complétude", severity="mineur",
          value_type="boolean"),
        R(id="M10", label="Classification renseignée", field="Classification", category="complétude",
          severity="mineur"),
        R(id="M11", label="Résistance au feu renseignée (FireRating)", field="*.FireRating", ifc_classes=_FIRE_CLASSES,
          category="complétude", severity="info"),
        R(id="M12", label="Locaux numérotés (Name)", field="Name", ifc_classes=["IfcSpace"], category="nommage",
          severity="majeur"),
        R(id="M13", label="Locaux désignés (LongName)", field="LongName", ifc_classes=["IfcSpace"],
          category="nommage", severity="mineur"),
    ]


def general_audit(call: Call, selection, title) -> dict:
    """Audit général de la maquette (hors contrôles type Solibri) : inventaire, structuration spatiale, typage,
    complétude des données de base. Lecture des données BIMData seulement, rapide, sans géométrie."""
    target, _, snap, _ = _ctx(call)
    recs = build_records(snap)
    ov = model_overview_from(recs, snap)
    phys = [r for r in recs if r.is_physical]
    n = len(phys) or 1
    proxies = sum(1 for r in phys if r.ifc_class == "IfcBuildingElementProxy")
    classified = sum(1 for r in phys if r.classifications)
    no_storey = sum(1 for r in phys if not r.storey)
    empty = [s["name"] for s in ov["storeys"] if not s["n_elements"]]
    pct = lambda k: f"{100 * k / n:.0f} %".replace(".", ",")  # noqa: E731
    notes = [f"Inventaire : {len(phys)} éléments physiques, {len(ov['storeys'])} niveau(x), "
             f"{len(ov['buildings'])} bâtiment(s), {ov['n_spaces']} local(aux), {ov['n_zones']} zone(s) ; "
             f"schéma {(snap.model or {}).get('version') or '—'}.",
             f"Objets génériques (IfcBuildingElementProxy) : {proxies} ({pct(proxies)}).",
             f"Éléments classifiés : {classified} ({pct(classified)}) ; sans niveau : {no_storey} ({pct(no_storey)})."]
    if empty:
        notes.append(f"Niveau(x) sans aucun élément : {', '.join(map(str, empty[:10]))}.")
    if not ov["n_spaces"]:
        notes.append("Aucun local (IfcSpace) : surfaces, contrôles PMR et évacuation non exploitables.")
    if len(ov["buildings"]) > 1:
        notes.append(f"Plusieurs bâtiments : {', '.join(ov['buildings'][:6])} — vérifier que c'est voulu.")
    out = _run_and_report(call, general_rules(), selection=selection, phase=None,
                          title=title or "Audit général de la maquette", requirements_id=None,
                          prefix="AUDIT-GENERAL", type_livrable="Audit général de la maquette",
                          avertissements=notes, audit_id="general",
                          extra={"inventaire": {k: ov[k] for k in ("n_elements", "n_spaces", "n_zones", "buildings")}
                                 | {"niveaux": len(ov["storeys"]), "objets_generiques": proxies,
                                    "elements_classifies": classified, "sans_niveau": no_storey,
                                    "classes_principales": dict(list(ov["ifc_classes"].items())[:12])}})
    return out


# ── Audit spécifique CCBIM ──────────────────────────────────────────────


def _local_file(path: str) -> Path:
    """Fichier local de l'utilisateur : seulement quand le serveur tourne sur son poste (stdio)."""
    if files.http_links_enabled():
        raise ValueError("Chemin local refusé : le serveur MCP ne tourne pas sur votre poste. Déposer le CCBIM dans "
                         "la GED BIMData (bimdata_document_id) ou via create_upload_link (file_id).")
    from . import ccbim

    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"Fichier introuvable : {p}")
    if p.suffix.lower() not in ccbim.SUPPORTED:
        raise ValueError(f"Format non pris en charge ({p.suffix}) : PDF, Word (.docx), Excel, texte.")
    if p.stat().st_size > get_settings().max_upload_mb * 2**20:
        raise ValueError("Fichier trop volumineux.")
    return p


def read_ccbim(call: Call, file_path, file_id, bimdata_document_id, file_url, file_base64, file_name, text,
               ccbim_id, page_from) -> dict:
    """Lit le CCBIM (ou la suite d'un CCBIM déjà lu) et rend son texte paginé et ses passages utiles."""
    from . import ccbim

    st = call.state
    if ccbim_id:
        d = st.load(ccbim.KIND, ccbim_id)
        if not d:
            raise ValueError(f"CCBIM {ccbim_id!r} introuvable.")
        pages = d["pages"]
    else:
        if text:
            pages, name = ccbim._chunks(text), file_name or "CCBIM (texte transmis)"
        else:
            if file_path:
                path = _local_file(file_path)
            else:
                client = make_client(_target(call), call.identity) if bimdata_document_id not in (None, "") else None
                path = files.resolve_input(st, file_id=file_id, bimdata_document_id=bimdata_document_id,
                                           file_url=file_url, file_base64=file_base64, file_name=file_name,
                                           client=client)
            pages, name = ccbim.read_pages(path), path.name
        ccbim_id = new_id("ccbim")
        d = {"ccbim_id": ccbim_id, "name": name, "pages": pages, "created_at": now_iso(), "expectations": None}
        st.save(ccbim.KIND, ccbim_id, d)
    chunk, nxt = ccbim.page_slice(pages, max(0, (page_from or 1) - 1))
    return {
        "ccbim_id": ccbim_id, "document": d["name"], "pages": len(pages), "texte": chunk, "page_suivante": nxt + 1 if nxt is not None else None,
        "passages_cles": ccbim.key_passages(pages) if not page_from or page_from <= 1 else [],
        "consigne": ("Lire tout le document (read_ccbim(ccbim_id, page_from=page_suivante) tant qu'il reste des "
                     "pages), puis extraire les attendus CONTRÔLABLES sur la maquette, chacun avec sa source (page, "
                     "article) : exigences de données (requirements : classes IFC, champ, obligatoire, valeurs "
                     "admises, format regex, unicité, criticité) et contrôles de maquette (model_checks : version IFC, "
                     "nom du fichier, nombre de bâtiments, niveaux, classification, locaux, objets génériques). Lister "
                     "aussi les attendus non contrôlables (not_checkable : méthode, planning, LOD géométrique…). "
                     "N'inventer aucune exigence absente du document. Puis set_ccbim_expectations, présenter les "
                     "attendus à l'utilisateur et les faire valider via run_audit('ccbim')."),
    }


def set_ccbim_expectations(call: Call, ccbim_id, requirements, model_checks, not_checkable, sources) -> dict:
    from . import ccbim

    st = call.state
    d = st.load(ccbim.KIND, ccbim_id)
    if not d:
        raise ValueError(f"CCBIM {ccbim_id!r} introuvable (read_ccbim d'abord).")
    exp = ccbim.Expectations(requirements=requirements or [], model_checks=model_checks or {},
                             not_checkable=not_checkable or [], sources=sources or {})
    if not exp.requirements and not exp.model_checks.model_dump(exclude_none=True):
        raise ValueError("Aucun attendu contrôlable : au moins une exigence de données ou un contrôle de maquette.")
    d["expectations"] = exp.model_dump()
    d["updated_at"] = now_iso()
    st.save(ccbim.KIND, ccbim_id, d)
    return {"ccbim_id": ccbim_id, "document": d["name"], **ccbim.summary(exp),
            "attendus": ccbim.hypotheses(exp), "attendus_non_controlables": exp.not_checkable,
            "suite": "run_audit('ccbim') : présenter les attendus pour validation, puis lancer avec le jeton."}


def latest_ccbim(call: Call, ccbim_id: str | None = None) -> dict | None:
    from . import ccbim

    if ccbim_id:
        return call.state.load(ccbim.KIND, ccbim_id)
    rows = [r for r in call.state.list(ccbim.KIND) if r.get("expectations")]
    rows.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
    return call.state.load(ccbim.KIND, rows[0]["ccbim_id"]) if rows else None


def ccbim_audit(call: Call, ccbim_id, selection, title) -> dict:
    from . import ccbim

    d = latest_ccbim(call, ccbim_id)
    if not d or not d.get("expectations"):
        raise ValueError("Aucun CCBIM analysé : read_ccbim puis set_ccbim_expectations.")
    exp = ccbim.Expectations(**d["expectations"])
    target, _, snap, _ = _ctx(call)
    notes = [f"Attendus tirés du CCBIM « {d['name']} » : {len(exp.requirements)} exigence(s) de données, "
             f"{len(exp.model_checks.model_dump(exclude_none=True))} contrôle(s) de maquette."]
    notes += [f"Non contrôlé automatiquement : {x.get('exigence')}" + (f" ({x.get('source')})" if x.get("source") else "")
              + (f" — {x.get('raison')}" if x.get("raison") else "") for x in exp.not_checkable]
    extra_sheet = {"name": "Attendus du CCBIM", "rows": ccbim.hypotheses(exp), "non_controlables": exp.not_checkable}
    return _run_and_report(call, ccbim.all_requirements(exp), selection=selection, phase=None,
                           title=title or f"Audit selon le CCBIM — {d['name']}", requirements_id=None,
                           runner=lambda recs: ccbim.run(exp, recs, snap), prefix="AUDIT-CCBIM",
                           type_livrable="Audit selon le CCBIM", avertissements=notes, audit_id="ccbim",
                           hypotheses=ccbim.hypotheses(exp),
                           references=[f"Cahier des charges BIM du projet : « {d['name']} »."],
                           extra_sheet=extra_sheet, extra={"ccbim_id": d["ccbim_id"], "ccbim": d["name"]})


def audit_classifications(call: Call, selection, system, title, auto_fill_threshold) -> dict:
    """Audit des classifications : manquantes, incohérentes (classe IFC, type, format, doublons) + propositions.

    Produit toujours le rapport Excel (charte BIMData) : synthèse, erreurs à corriger avec propositions
    préremplies, données contrôlées, règles appliquées.
    """
    from .quality.classification_audit import REQUIREMENTS as CLS_REQS
    from .quality.classification_audit import detect_system, is_uniformat, run_classification_audit

    target, _, snap, _ = _ctx(call)
    recs, _ = _selection_records(call, selection, target, snap)
    warnings = []
    used = detect_system(recs)
    sys_ = (system or "").strip() or used or "Uniformat"
    if not used:
        warnings.append("Aucune classification dans le périmètre : tous les éléments sont signalés « manquante », "
                        "sans proposition possible (aucun code de référence dans la maquette).")
    elif system and system.strip().lower() != used.lower():
        warnings.append(f"Système audité « {sys_} » ; le système le plus utilisé dans la maquette est « {used} ».")
    if not is_uniformat(sys_):
        warnings.append(f"« {sys_} » n'est pas UniFormat : cohérence classe IFC ↔ code et format non contrôlés "
                        "(contrôles de présence, doublons et cohérence au sein d'un type uniquement).")
    systems = Counter(c.partition(":")[0].strip() for r in recs for c in r.classifications if ":" in c)
    out = _run_and_report(
        call, CLS_REQS, selection=selection, phase=None, title=title or f"Audit des classifications {sys_}",
        requirements_id=None, auto_fill_threshold=auto_fill_threshold,
        runner=lambda rs: run_classification_audit(rs, sys_)[0], prefix="AUDIT-CLASSIF",
        type_livrable="Audit des classifications", avertissements=warnings,
        extra={"systeme_audite": sys_, "systemes_presents": dict(systems.most_common())},
        suite=["prepare_corrections_from_check(check_id) pour appliquer les propositions fiables (plan à valider)",
               "export_selection_excel(selection) pour un export Excel complet des éléments concernés "
               "(colonne Classification modifiable puis réimportable)"],
    )
    by_req = {p["req_id"]: p["non_conformes"] for p in out["worst_requirements"]}
    out["synthese"].update({
        "classifications_manquantes": by_req.get("K01", 0),
        "codes_multiples": by_req.get("K02", 0),
        "formats_invalides": by_req.get("K03", 0),
        "incoherences_classe_ifc": by_req.get("K04", 0),
        "incoherences_type": by_req.get("K05", 0),
    })
    return out


def requirements_template(call: Call) -> dict:
    titre = "Gabarit de cahier des charges de données"
    path = files.new_output_path(call.state, nom_fichier("GABARIT-CDC", "BIMData", "cahier-des-charges"))
    write_template(path, Contexte(type_livrable="Gabarit", titre=titre, auteur=call.identity.display))
    meta = files.register_file(call.state, path, kind="template", label=titre)
    return {"livrable": _livrable(meta, type_livrable="Gabarit", titre=titre, feuilles=["Synthèse", "Exigences"]),
            "synthese": {"colonnes": 16, "lignes_exemple": 7}, "avertissements": [],
            "suite": ["Compléter le gabarit puis import_data_requirements (file_id via create_upload_link ou "
                      "document de la GED BIMData)."]}


def import_data_requirements(call: Call, name, file_id, bimdata_document_id, file_url, file_base64, file_name,
                             sheet, header_row, column_map, default_ifc_classes) -> dict:
    st = call.state
    client = None
    if bimdata_document_id not in (None, ""):
        client = make_client(_target(call), call.identity)
    path = files.resolve_input(st, file_id=file_id, bimdata_document_id=bimdata_document_id, file_url=file_url,
                               file_base64=file_base64, file_name=file_name, client=client)
    parsed = parse_requirements(path, sheet=sheet, column_map=column_map, header_row=header_row,
                                default_classes=default_ifc_classes)
    rid = new_id("req")
    data = {"requirements_id": rid, "name": name or path.stem, "source_file": path.name, "created_at": now_iso(),
            **parsed}
    st.save("requirements", rid, data)
    reqs = parsed["requirements"]
    return {
        "requirements_id": rid, "name": data["name"], "n_requirements": len(reqs),
        "phases_available": parsed["phases_available"], "sheets": parsed["sheets"],
        "by_ifc_class": dict(Counter(c for r in reqs for c in r["ifc_classes"]).most_common(25)),
        "preview": [{k: r[k] for k in ("id", "ifc_classes", "field", "required", "value_type", "phases", "label")}
                    for r in reqs[:25]],
        "warnings": parsed["warnings"][:15], "n_warnings": parsed["n_warnings"],
        "next": "Vérifier l'interprétation (show_requirements / edit_requirements) puis run_requirements_check.",
    }


def _load_reqset(call: Call, rid: str) -> dict:
    d = call.state.load("requirements", rid)
    if not d:
        raise ValueError(f"Cahier des charges {rid!r} introuvable (voir list_requirement_sets).")
    return d


def list_requirement_sets(call: Call) -> dict:
    return {"requirement_sets": [{"requirements_id": d["requirements_id"], "name": d["name"],
                                  "n_requirements": len(d["requirements"]), "phases": d.get("phases_available"),
                                  "created_at": d["created_at"]} for d in call.state.list("requirements")]}


def show_requirements(call: Call, rid, ifc_class, offset, limit) -> dict:
    d = _load_reqset(call, rid)
    reqs = d["requirements"]
    if ifc_class:
        reqs = [r for r in reqs if any(ifc_class.lower() in c.lower() for c in r["ifc_classes"])]
    return {"requirements_id": rid, "total": len(reqs), "offset": offset, "requirements": reqs[offset:offset + limit]}


def edit_requirements(call: Call, rid, upsert: list[Requirement] | None, remove_ids: list[str] | None) -> dict:
    d = _load_reqset(call, rid)
    reqs = {r["id"]: r for r in d["requirements"]}
    for rm in remove_ids or []:
        reqs.pop(rm, None)
    n = len(reqs)
    for r in upsert or []:
        rr = r.model_dump()
        if not rr["id"]:
            n += 1
            rr["id"] = f"U{n:04d}"
        reqs[rr["id"]] = rr
    d["requirements"] = list(reqs.values())
    d["edited_at"] = now_iso()
    call.state.save("requirements", rid, d)
    return {"requirements_id": rid, "n_requirements": len(d["requirements"])}


def run_requirements_check(call: Call, rid, selection, phase, include_naming_rules, title, auto_fill_threshold) -> dict:
    d = _load_reqset(call, rid)
    reqs = [Requirement(**r) for r in d["requirements"]]
    if phase and d.get("phases_available") and phase.upper() not in d["phases_available"]:
        raise ValueError(f"Phase {phase!r} absente du cahier des charges (phases : {d['phases_available']}).")
    if include_naming_rules:
        reqs += default_rules()
    return _run_and_report(call, reqs, selection=selection, phase=phase, title=title or f"Contrôle — {d['name']}",
                           requirements_id=rid, auto_fill_threshold=auto_fill_threshold)


def list_checks(call: Call) -> dict:
    return {"checks": [{"check_id": c["check_id"], "title": c["title"], "created_at": c["created_at"], "phase": c.get("phase"),
                        "selection": c.get("selection"), "summary": c["summary"], "target": c["target"]}
                       for c in call.state.list("checks")[:50]]}


def get_check_issues(call: Call, check_id, status, req_id, ifc_class, severity, with_proposal_only, offset, limit) -> dict:
    target = _target(call)
    c = _load_check(call, check_id, target)
    iss = c["issues"]
    if status:
        iss = [i for i in iss if i["status"] == status or STATUS_LABELS.get(i["status"]) == status]
    if req_id:
        iss = [i for i in iss if i["req_id"] == req_id]
    if ifc_class:
        iss = [i for i in iss if ifc_class.lower() in i["ifc_class"].lower()]
    if severity:
        iss = [i for i in iss if i["severity"] == severity]
    if with_proposal_only:
        iss = [i for i in iss if i.get("proposal") not in (None, "")]
    report = c.get("report_file_id")
    rep = None
    if report:
        try:
            p = files.get_file(call.state, report)
            rep = files.download_url(call.state, report, p.name)
        except FileNotFoundError:
            rep = None
    return {"check_id": check_id, "total": len(iss), "offset": offset, "issues": iss[offset:offset + limit],
            "report_download_url": rep, "statuses": STATUS_LABELS}


# ── Corrections ─────────────────────────────────────────────────────────


def prepare_corrections_from_file(call: Call, file_id, bimdata_document_id, file_url, file_base64, file_name) -> dict:
    target, client, snap, _ = _ctx(call)
    st = call.state
    path = files.resolve_input(st, file_id=file_id, bimdata_document_id=bimdata_document_id, file_url=file_url,
                               file_base64=file_base64, file_name=file_name, client=client)
    fmt, meta, raw = corr.parse_corrections_file(path)
    meta_target = meta.get("target") if isinstance(meta.get("target"), dict) else None
    if meta_target and BIMDataTarget.from_dict(meta_target).key != target.key:
        raise ValueError(f"Le fichier a été produit pour la maquette {meta_target}, la maquette active est "
                         f"{target.to_dict()}. Activer la bonne maquette (set_active_model).")
    value_types = {}
    if meta.get("check_id"):
        chk = st.load("checks", str(meta["check_id"]))
        value_types = (chk or {}).get("value_types", {})
    items, rejected = corr.validate_changes(raw, records_by_uuid(snap), value_types=value_types)
    if not items:
        return {"format": fmt, "changes": 0, "rejected": len(rejected), "rejected_sample": rejected[:20],
                "message": "Aucun changement applicable trouvé dans le fichier."}
    plan = corr.build_plan(items, target=target.to_dict(), origin=f"fichier {path.name} ({fmt})", rejected=rejected)
    preview = plans.save_plan(st, plan)
    data = st.load("plans", plan.plan_id)
    data["rejected"] = rejected[:2000]
    st.save("plans", plan.plan_id, data, gz=True)
    return {"format": fmt, **preview, "rejected_sample": rejected[:15]}


def prepare_corrections_from_check(call: Call, check_id, min_confidence, req_ids, statuses, issue_ids) -> dict:
    target, _, snap, _ = _ctx(call)
    c = _load_check(call, check_id, target)
    raw = []
    for i in c["issues"]:
        if not i.get("writable") or i.get("proposal") in (None, ""):
            continue
        if issue_ids and i["issue_id"] not in issue_ids:
            continue
        if not issue_ids and (i.get("confidence") or 0) < min_confidence:
            continue
        if req_ids and i["req_id"] not in req_ids:
            continue
        if statuses and i["status"] not in statuses:
            continue
        fld = i["field"]
        if fld.startswith("*.") and i.get("location", "").startswith("pset:"):
            fld = i["location"].split(":", 1)[1]
        raw.append({"uuid": i["uuid"], "field": fld, "value": i["proposal"], "source": i["issue_id"]})
    items, rejected = corr.validate_changes(raw, records_by_uuid(snap), value_types=c.get("value_types"))
    if not items:
        return {"changes": 0, "rejected": len(rejected), "rejected_sample": rejected[:15],
                "message": "Aucune proposition retenue (abaisser min_confidence ou utiliser le fichier Excel)."}
    plan = corr.build_plan(items, target=target.to_dict(), origin=f"propositions du contrôle {check_id}", rejected=rejected)
    return {**plans.save_plan(call.state, plan), "rejected_sample": rejected[:10]}


# ── Fichiers ────────────────────────────────────────────────────────────


def create_upload_link(call: Call, purpose: str) -> dict:
    link = files.upload_link(call.state, purpose)
    return {**link, "instructions": "Ouvrir ce lien dans un navigateur, déposer le fichier : la page affiche son file_id "
                                    "(également visible via list_files). Le transmettre ensuite à l'outil voulu."}


def list_files(call: Call, kind: str | None) -> dict:
    fl = files.list_files(call.state)
    if kind:
        fl = [f for f in fl if f.get("kind") == kind]
    return {"files": fl[:100]}


def list_bimdata_documents(call: Call, search: str | None) -> dict:
    target = _target(call)
    client = make_client(target, call.identity)
    docs = client.list_documents()
    if search:
        s = search.lower()
        docs = [d for d in docs if s in (d.get("name") or "").lower() or s in (d.get("file_name") or "").lower()]
    return {"documents": [{"bimdata_document_id": d.get("id"), "name": d.get("name"), "file_name": d.get("file_name"),
                           "parent_id": d.get("parent_id"), "size": d.get("size"), "updated_at": d.get("updated_at")}
                          for d in docs][:200]}
