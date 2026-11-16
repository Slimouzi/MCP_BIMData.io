"""Implémentations (synchrones) des outils MCP.

Chaque fonction reçoit un :class:`Call` (qui appelle, depuis quelle session)
et retourne un dict JSON-sérialisable compact. ``server.py`` ne fait que
déclarer les outils et déporter ces appels hors de la boucle d'événements.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

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
from .quality.requirements import Requirement, default_rules
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
    issues = check["issues"]
    bad = {i["uuid"] for i in issues}
    sev_of: dict[str, str] = {}
    order = {"bloquant": 0, "majeur": 1, "mineur": 2, "info": 3}
    for i in issues:
        prev = sev_of.get(i["uuid"])
        if prev is None or order[i["severity"]] < order[prev]:
            sev_of[i["uuid"]] = i["severity"]
    colors = publication.STATUS_COLORS
    groups = [{"label": f"Non conforme — {s}", "color": colors[s], "uuids": [u for u, v in sev_of.items() if v == s]}
              for s in ("bloquant", "majeur", "mineur", "info")]
    groups.append({"label": "Conforme", "color": publication.STATUS_COLORS["conforme"],
                   "uuids": [u for u in check.get("checked_uuids", []) if u not in bad]})
    return [g for g in groups if g["uuids"]]


def prepare_smart_view(call: Call, title, selection, color, color_by, selections, colors, check_id) -> dict:
    target, _, snap, _ = _ctx(call)
    recs_all = build_records(snap)
    names = _names(recs_all)
    st = call.state
    if check_id:
        check = _load_check(call, check_id, target)
        groups = _check_groups(check)
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
    plan = WritePlan(kind=WritePlanKind.SMART_VIEWS, target=target.to_dict(),
                     summary={"title": title, "groups": len(legend), "legend": legend},
                     items=[{"payload": payload}],
                     risks=["Crée une Smart View visible par les membres du projet BIMData."])
    return plans.save_plan(st, plan)


def prepare_bcf_topics(call: Call, title, selection, description, priority, topic_type, labels, assigned_to,
                       due_date, group_by, one_topic_per_element, check_id, check_group_by, max_topics) -> dict:
    target, _, snap, _ = _ctx(call)
    st = call.state
    items: list[dict] = []
    if check_id:
        check = _load_check(call, check_id, target)
        by: dict[str, list[dict]] = {}
        for i in check["issues"]:
            key = {"requirement": f"{i['req_id']} — {i['req_label']}", "severity": i["severity"],
                   "element": f"{i['ifc_class']} — {i['name'] or i['uuid']}", "status": STATUS_LABELS.get(i["status"], i["status"])
                   }[check_group_by]
            by.setdefault(key, []).append(i)
        prio_of = {"bloquant": "Critical", "majeur": "High", "mineur": "Medium", "info": "Low"}
        order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Information": 4}
        for key, iss in sorted(by.items(), key=lambda kv: -len(kv[1])):
            worst = min((prio_of.get(i["severity"], "Medium") for i in iss), key=lambda p: order[p])
            lines = [f"{len(iss)} anomalie(s) — contrôle « {check.get('title')} »."]
            if iss[0].get("expected"):
                lines.append(f"Attendu : {iss[0]['expected']}")
            for i in iss[:15]:
                prop = f" → proposé : {i['proposal']}" if i.get("proposal") not in (None, "") else ""
                lines.append(f"• {i['ifc_class']} « {i['name'] or i['uuid']} » ({i['storey'] or '—'}) : "
                             f"{STATUS_LABELS.get(i['status'], i['status'])}, actuel = {i['actual']!r}{prop}")
            if len(iss) > 15:
                lines.append(f"… et {len(iss) - 15} autre(s) (voir le rapport Excel).")
            items.append({"payload": publication.bcf_topic(
                [i["uuid"] for i in iss], title=f"{title} — {key}", description="\n".join(lines), priority=worst,
                topic_type=topic_type, labels=(labels or []) + ["controle-donnees"], assigned_to=assigned_to,
                due_date=due_date, model_id=target.model_id)})
    else:
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


def apply_plan(call: Call, plan_id: str, confirm: bool) -> dict:
    if not confirm:
        return {"applied": False, **plans.plan_preview(plans.load_plan(call.state, plan_id)),
                "message": "Rien n'a été écrit. Relancer avec confirm=True après validation par l'utilisateur."}
    target = _target(call)
    client = make_client(target, call.identity)
    result = plans.apply_plan(call.state, client, target, plan_id)
    result["viewer_url"] = target.viewer_url(get_settings().platform_url)
    return result


def list_plans(call: Call, limit: int) -> dict:
    return {"plans": [{"plan_id": p["plan_id"], "kind": p["kind"], "status": p.get("status"), "created_at": p["created_at"],
                       "summary": p["summary"], "n_items": len(p["items"])} for p in call.state.list("plans")[:limit]]}


def show_plan(call: Call, plan_id: str, offset: int, limit: int) -> dict:
    p = plans.load_plan(call.state, plan_id)
    return {**plans.plan_preview(p), "items": [plans._short(i) for i in p["items"][offset: offset + limit]],
            "offset": offset, "result": p.get("result"), "rejected": p.get("rejected", [])[:30]}


# ── Contrôles ───────────────────────────────────────────────────────────


def _load_check(call: Call, check_id: str, target: BIMDataTarget) -> dict:
    c = call.state.load("checks", check_id)
    if not c:
        raise ValueError(f"Contrôle {check_id!r} introuvable (voir list_checks).")
    if BIMDataTarget.from_dict(c["target"]).key != target.key:
        raise ValueError("Ce contrôle porte sur une autre maquette que la maquette active.")
    return c


def _run_and_report(call: Call, reqs: list[Requirement], *, selection, phase, title, requirements_id,
                    auto_fill_threshold: float = 0.8) -> dict:
    target, _, snap, _ = _ctx(call)
    recs, sel_name = _selection_records(call, selection, target, snap)
    result = run_check(recs, reqs, phase=phase)
    check_id = new_id("chk")
    st = call.state
    ctx = _doc_ctx(call, snap, target, "Contrôle des données", title, perimetre=sel_name if selection else "",
                   phase=phase)
    path = files.new_output_path(st, nom_fichier("CONTROLE", ctx.projet, ctx.maquette, phase or ""))
    write_check_report(path, result, [r for r in reqs if r.applies_to_phase(phase)], records_by_uuid(snap),
                       check_id=check_id, ctx=ctx, requirements_id=requirements_id,
                       auto_fill_threshold=auto_fill_threshold)
    meta = files.register_file(st, path, kind="check-report", label=title)
    summary = result.summary()
    st.save("checks", check_id, {
        "check_id": check_id, "title": title, "target": target.to_dict(), "requirements_id": requirements_id,
        "phase": phase, "selection": sel_name, "created_at": now_iso(), "summary": summary,
        "per_requirement": result.per_requirement, "issues": [i.to_dict() for i in result.issues],
        "checked_uuids": result.checked_uuids, "report_file_id": meta["file_id"],
        "value_types": {r.field: r.value_type for r in reqs if r.value_type},
    }, gz=True)
    worst = sorted([p for p in result.per_requirement if p["non_conformes"]], key=lambda p: -p["non_conformes"])[:12]
    return {
        "livrable": _livrable(meta, type_livrable="Contrôle des données", titre=title,
                              feuilles=["Synthèse", "Erreurs à corriger", "Données contrôlées", "Exigences"]),
        "check_id": check_id, "selection": sel_name, "phase": phase, "summary": summary,
        "synthese": {"elements_controles": summary["elements_controlled"], "controles": summary["checks"],
                     "non_conformites": summary["non_conformities"], "taux_conformite": summary["conformity_rate"],
                     "propositions_fiables": summary["proposals_high_confidence"]},
        "worst_requirements": [{k: p[k] for k in ("req_id", "label", "ifc_classes", "field", "non_conformes", "applicable")}
                               for p in worst],
        "sample_issues": [{k: i.to_dict()[k] for k in ("issue_id", "ifc_class", "name", "field", "status", "actual",
                                                       "proposal", "confidence")} for i in result.issues[:10]],
        "avertissements": [],
        "suite": [
            "get_check_issues(check_id, …) pour détailler les anomalies",
            "prepare_corrections_from_check(check_id) pour appliquer directement les propositions fiables",
            "ou : compléter le rapport Excel puis prepare_corrections_from_file",
            "prepare_smart_view(check_id=…) / prepare_bcf_topics(check_id=…) pour publier dans BIMData",
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
