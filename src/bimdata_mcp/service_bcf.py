"""Préparation des topics BCF d'un contrôle : un point de vue complet par topic (caméra, sélection, couleurs, coupe,
chemin, image), clé d'anomalie stable et rapprochement avec les topics existants. N'écrit rien : produit un plan."""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from bim_core.write_plan import WritePlan, WritePlanKind

from . import bcf, files, plans
from .model_view import records_by_uuid
from .quality.engine import STATUS_LABELS
from .state import now_iso

#: Valeurs d'extension que le MCP déclare dans le projet BCF s'il ne les a pas (réglage du projet, pas du serveur).
WANTED_PRIORITIES = ("Critical", "High", "Low", "Information")
WANTED_STATUSES = ("Open", "Resolved")
#: Équivalents admis dans un projet BCF qui a son propre vocabulaire (utilisés tels quels, sans rien déclarer).
SYNONYMS = {
    "topic_status": {"Open": ("Open", "Opened", "Active", "Ouvert", "New", "Nouveau"),
                     "Resolved": ("Resolved", "Closed", "Résolu", "Fermé", "Done", "Terminé")},
    "topic_type": {"Issue": ("Issue", "Error", "Problem", "Problème", "Defect", "Anomalie"),
                   "Clash": ("Clash", "Conflict", "Conflit", "Collision")},
    "priority": {"Critical": ("Critical", "Critique", "Bloquant", "Blocking"), "High": ("High", "Haute", "Major", "Majeur"),
                 "Low": ("Low", "Basse", "Minor", "Mineur"), "Information": ("Information", "Info")},
}


def project_value(kind: str, wanted: str, ext: dict | None) -> str | None:
    """Valeur du projet équivalente à ``wanted`` (même nom à la casse près, ou synonyme), sinon None."""
    have = (ext or {}).get(kind) or []
    low = {h.lower(): h for h in have}
    for cand in (wanted, *SYNONYMS.get(kind, {}).get(wanted, ())):
        if cand.lower() in low:
            return low[cand.lower()]
    return None


PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3, "information": 4, "info": 4}
VIEW_RE = re.compile(r"Vue MCP : ([0-9a-f]{8})")
CLOSED = {"resolved", "closed", "fermé", "résolu"}


def _group_key(i: dict, by: str) -> str:
    return {"requirement": f"{i['req_id']} — {i['req_label']}", "severity": i["severity"],
            "element": f"{i['ifc_class']} — {i['name'] or i['uuid']}",
            "status": STATUS_LABELS.get(i["status"], i["status"]), "issue": i["issue_id"]}[by]


#: Au-delà de ce nombre de topics, sans filtre, on demande d'abord quoi envoyer (gravité, règle, type de conflit).
BCF_CHOICE_MIN = 20
SEV_ORDER = ["bloquant", "majeur", "mineur", "info"]


def issue_filter(severities=None, rules=None, types=None):
    """Filtre des anomalies à transmettre : gravités, règles (A2, S4, CLASH…), types de conflit ou cases de matrice
    (doublon, inclus, chevauchement, ARC-STR…)."""
    sev = {str(x).lower() for x in severities or []}
    rl = {str(x).upper() for x in rules or []}
    ty = {str(x).lower() for x in types or []}

    def ok(i: dict) -> bool:
        if i.get("status") == "exclu":
            return False
        if sev and str(i.get("severity")).lower() not in sev:
            return False
        if rl and str(i.get("rule_id") or i.get("req_id")).upper() not in rl:
            return False
        return not ty or str(i.get("clash_type") or "").lower() in ty or str(i.get("cell") or "").lower() in ty

    return ok


def bcf_scope(check: dict, group_by: str, insight: dict | None = None, *, title: str | None = None) -> dict:
    """Étape de choix : répartition des topics par gravité, par règle et par type de conflit, et sélections
    proposées (la recommandée d'abord : bloquants et majeurs des règles fiables)."""
    issues = [i for i in check.get("issues") or [] if i.get("status") != "exclu"]
    topics: dict[str, dict] = {}
    for i in issues:
        k = _group_key(i, group_by)
        t = topics.setdefault(k, {"sev": i["severity"], "rule": i.get("rule_id") or i["req_id"],
                                  "label": i.get("req_label") or "", "type": i.get("clash_type") or i.get("cell")})
        if SEV_ORDER.index(i["severity"]) < SEV_ORDER.index(t["sev"]):
            t["sev"] = i["severity"]
    doubtful = set((insight or {}).get("regles_douteuses") or [])
    verdicts = {r["regle"]: r["verdict"] for r in (insight or {}).get("regles") or []}
    by_sev = {s: sum(1 for t in topics.values() if t["sev"] == s) for s in SEV_ORDER}
    rules: dict[str, dict] = {}
    for t in topics.values():
        r = rules.setdefault(t["rule"], {"regle": t["rule"], "libelle": t["label"], "topics": 0,
                                         "par_gravite": {}, "fiabilite": verdicts.get(t["rule"], "non évaluée")})
        r["topics"] += 1
        r["par_gravite"][t["sev"]] = r["par_gravite"].get(t["sev"], 0) + 1
    types = dict(Counter(t["type"] for t in topics.values() if t["type"]).most_common(12))
    base = {"check_id": check["check_id"], "check_group_by": group_by}
    top = [s for s in ("bloquant", "majeur") if by_sev.get(s)]
    sure = sorted(r for r in rules if r not in doubtful)
    options = []
    if top:
        args = {**base, "severities": top}
        n = sum(1 for t in topics.values() if t["sev"] in top)
        if doubtful and sure:
            args["rules"] = sure
            n = sum(1 for t in topics.values() if t["sev"] in top and t["rule"] in sure)
        options.append({"choix": "Bloquants et majeurs" + (" des règles fiables" if args.get("rules") else ""),
                        "topics": n, "recommande": True, "arguments": args})
    for s in SEV_ORDER:
        if by_sev[s]:
            options.append({"choix": f"Gravité {s} seulement", "topics": by_sev[s],
                            "arguments": {**base, "severities": [s]}})
    for r in sorted(rules.values(), key=lambda r: -r["topics"])[:8]:
        options.append({"choix": f"Règle {r['regle']} {r['libelle']}".strip(), "topics": r["topics"],
                        "arguments": {**base, "rules": [r["regle"]]}})
    for ty, n in list(types.items())[:5]:
        options.append({"choix": f"Type de conflit « {ty} »", "topics": n, "arguments": {**base, "types": [ty]}})
    options.append({"choix": "Tout envoyer", "topics": len(topics), "arguments": {**base, "perimetre_valide": True}})
    lines = [f"{len(topics)} topics BCF seraient créés pour « {title or check.get('title')} ». Pour ne pas surcharger "
             "le projet, que voulez-vous envoyer ?",
             "Par gravité : " + ", ".join(f"{n} {s}" for s, n in by_sev.items() if n) + ".",
             "Par règle : " + ", ".join(f"{r['regle']} ({r['topics']})" for r in
                                        sorted(rules.values(), key=lambda r: -r["topics"])[:8]) + "."]
    if types:
        lines.append("Par type de conflit : " + ", ".join(f"{k} ({v})" for k, v in types.items()) + ".")
    if doubtful:
        lines.append(f"Règles dont les résultats sont à confirmer : {', '.join(sorted(doubtful))} — à écarter pour "
                     "l'instant.")
    if options and options[0].get("recommande"):
        lines.append(f"Je recommande : {options[0]['choix'].lower()} ({options[0]['topics']} topics).")
    return {"etape": "choix_perimetre_bcf", "topics_total": len(topics), "par_gravite": by_sev,
            "par_regle": sorted(rules.values(), key=lambda r: -r["topics"]), "par_type": types,
            "regles_douteuses": sorted(doubtful), "options": options, "question": "\n".join(lines),
            "consigne": ("Poser la question telle quelle (choix simple parmi options, la recommandée en premier) ; "
                         "rien n'est préparé. Puis rappeler prepare_bcf_topics avec les arguments de l'option "
                         "choisie (severities, rules, types, ou perimetre_valide=True pour tout envoyer).")}


def _elements(iss: list[dict]) -> list[dict]:
    """Tous les éléments des anomalies, avec leur rôle (le premier rôle connu l'emporte)."""
    out: dict[str, dict] = {}
    for i in iss:
        els = list(i.get("elements") or [])
        if i.get("uuid") and not any(e.get("uuid") == i["uuid"] for e in els):
            els.insert(0, {"uuid": i["uuid"], "role": "élément"})
        for e in els:
            if e.get("uuid") and e["uuid"] not in out:
                out[e["uuid"]] = {"uuid": e["uuid"], "role": e.get("role") or "élément"}
    return list(out.values())


def _union(boxes: list[list[float]]) -> list[float] | None:
    boxes = [b for b in boxes if b and len(b) == 6]
    if not boxes:
        return None
    return [min(b[k] for b in boxes) for k in range(3)] + [max(b[k] for b in boxes) for k in range(3, 6)]


def _split(elements: list[dict], idx, limit: int) -> list[list[dict]]:
    """Au-delà de ``limit`` éléments : un découpage par étage, puis par lots de ``limit``."""
    if len(elements) <= limit:
        return [elements]
    by_storey: dict[str, list[dict]] = defaultdict(list)
    for e in elements:
        st = (idx.elements.get(e["uuid"]) or {}).get("storey") if idx else None
        by_storey[st or "?"].append(e)
    parts = []
    for els in by_storey.values():
        parts += [els[k:k + limit] for k in range(0, len(els), limit)]
    return parts


def _storey_z(idx, uuid) -> float | None:
    if idx is None:
        return None
    st = (idx.elements.get(uuid) or {}).get("storey")
    return next((s["z"] for s in idx.ifc.get("storeys") or [] if s["guid"] == st), None)


def _render(idx, jobs: list[dict], progress=None) -> list[dict]:
    """Images de tous les points de vue : processus séparé, plusieurs processus de rendu en parallèle, avancement
    relu dans ``<job>.progress`` (MCP_GEOMETRY_SUBPROCESS=0 : dans ce processus, à la suite)."""
    if not jobs:
        return []
    from .geometry.render_worker import run_job

    job = {"root": str(idx.root), "views": jobs}
    if os.getenv("MCP_GEOMETRY_SUBPROCESS", "1").strip().lower() in ("0", "false", "non", "no"):
        return run_job(job, progress)
    jp = Path(jobs[0]["out"]).parent / "render_job.json"
    jp.write_text(json.dumps(job))
    from .geometry.index import memory_limit_mb

    env = {**os.environ, "MCP_GEOMETRY_MAX_MEMORY_MB": str(memory_limit_mb())}
    proc = subprocess.Popen([sys.executable, "-m", "bimdata_mcp.geometry.render_worker", str(jp)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    prog, deadline = jp.with_suffix(".progress"), time.time() + max(300, 4 * len(jobs))
    while proc.poll() is None:
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("Rendu des images BCF interrompu : délai dépassé.")
        if progress and prog.exists():
            try:
                done, total = (int(x) for x in prog.read_text().split("/"))
                progress(done, total)
            except ValueError:
                pass
        time.sleep(1.0)
    _, err = proc.communicate()
    out = jp.with_suffix(".out.json")
    if proc.returncode != 0 or not out.exists():
        last = (err or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"Rendu des images BCF en échec : {last[0][:300]}")
    return json.loads(out.read_text())


def _existing_topics(client) -> list[dict]:
    try:
        return client.list_bcf_topics() or []
    except Exception:  # noqa: BLE001 — sans liste, pas de rapprochement (signalé)
        return []


def _extensions(client) -> dict[str, list[str]] | None:
    """Extensions du projet BCF (types, priorités, statuts, libellés) ; None si illisibles."""
    try:
        ext = client.get_bcf_extensions() or {}
    except Exception:  # noqa: BLE001
        return None
    return {k: [str(x) for x in ext.get(k) or []] for k in ("topic_type", "priority", "topic_status", "topic_label")}


def prepare_check_topics(call, *, check_id: str, title: str | None, check_group_by: str, max_topics: int,
                         frame_bbox: bool, isolate: bool, section_box, snapshot: bool, camera: str,
                         field_of_view_deg: float, extra_viewpoint: str, max_components: int, dedupe: bool,
                         labels, assigned_to, due_date, topic_type: str | None, severities=None,
                         rules=None, types=None, progress=None) -> dict:
    from .service import _ctx, _load_check, _save_selection, make_client
    from .service_geometry import _geometry

    target, _, snap, _ = _ctx(call)
    st = call.state
    check = _load_check(call, check_id, target)
    if camera not in ("auto", "iso", "plan", "elevation"):
        raise ValueError("camera : auto, iso, plan ou elevation.")
    if extra_viewpoint not in ("auto", "none", "plan", "section"):
        raise ValueError("extra_viewpoint : auto, none, plan ou section.")
    idx = None
    try:
        _, _, idx, _ = _geometry(call)
    except (ValueError, RuntimeError) as exc:
        if snapshot:
            raise ValueError(f"Image du point de vue impossible : index géométrique indisponible ({exc}). "
                             "snapshot=False pour préparer des topics sans image (déconseillé).") from exc
    recs = records_by_uuid(snap)
    off = bcf.viewer_offset()  # repère IFC, en mètres, sans décalage (ce qu'attend Solibri)
    client = make_client(target, call.identity)
    existing = _existing_topics(client) if dedupe else []
    ext = _extensions(client)
    by: dict[str, list[dict]] = defaultdict(list)
    keep = issue_filter(severities, rules, types)
    filtered = bool(severities or rules or types)
    for i in check["issues"]:
        if keep(i):
            by[_group_key(i, check_group_by)].append(i)
    if not by:
        crit = ", ".join(f"{k} {v}" for k, v in (("gravités", severities), ("règles", rules), ("types", types)) if v)
        raise ValueError("Aucune anomalie à transmettre" + (f" pour {crit}." if crit else "."))
    model = snap.model or {}
    doc = model.get("document") or {}
    rules_of_check = {i.get("rule_id") or i["req_id"] for i in check["issues"]} | {
        r["req_id"] for r in check.get("per_requirement") or []}
    topics, jobs, warnings = [], [], []
    for gkey, iss in sorted(by.items(), key=lambda kv: -len(kv[1])):
        rule = iss[0].get("rule_id") or iss[0]["req_id"]
        family = bcf.family_of(rule)
        fam = bcf.FAMILIES[family]
        elements = _elements(iss)
        parts = _split(elements, idx, max_components)
        sev = min((i["severity"] for i in iss), key=lambda s: list(bcf.PRIORITY).index(s) if s in bcf.PRIORITY else 9)
        main = iss[0]
        sz = _storey_z(idx, main.get("uuid"))
        storey = main.get("storey") or ""
        for k, part in enumerate(parts):
            key = bcf.issue_key(rule, [e["uuid"] for e in part])
            boxes = ([i.get("bbox") for i in iss] if len(parts) == 1 else
                     [(idx.elements.get(e["uuid"]) or {}).get("aabb") for e in part] if idx else [])
            path3d = None
            chemin = (main.get("measure") or {}).get("chemin") if len(iss) == 1 else None
            if chemin and sz is not None:
                path3d = [[p[0], p[1], sz + 0.10] for p in chemin]
                boxes = boxes + [[min(p[0] for p in chemin), min(p[1] for p in chemin), sz,
                                  max(p[0] for p in chemin), max(p[1] for p in chemin), sz + 2.5]]
            raw = _union(boxes) if frame_bbox else None
            if raw is None and idx is not None:
                raw = _union([(idx.elements.get(e["uuid"]) or {}).get("aabb") for e in part])
            if raw is None:  # lot sans géométrie connue : zone de l'anomalie (jamais d'élément perdu)
                raw = _union([i.get("bbox") for i in iss])
            if raw is None:
                warnings.append(f"{gkey} : aucune boîte englobante, topic sans caméra.")
                continue
            zone = bcf.dilate(raw)
            view = fam["camera"] if camera == "auto" else camera
            if section_box in (None, "auto"):
                section = fam["section"]
            else:
                section = ("box" if view == "iso" else "plane") if section_box else None
            vps = [bcf.viewpoint(main, part, zone=zone, view=view, section=section, storey_z=sz,
                                 fov_deg=field_of_view_deg, isolate=isolate, family=family, model_id=target.model_id,
                                 off=off, path3d=path3d)]
            extra = fam["extra"] if extra_viewpoint == "auto" else (None if extra_viewpoint == "none" else extra_viewpoint)
            if extra == "plan":
                vps.append(bcf.viewpoint(main, part, zone=zone, view="plan",
                                         section="center" if family == "clash" else "plane", storey_z=sz,
                                         fov_deg=field_of_view_deg, isolate=isolate, family=family,
                                         model_id=target.model_id, off=off, path3d=path3d))
            elif extra == "iso":
                vps.append(bcf.viewpoint(main, part, zone=zone, view="iso", section="box", storey_z=sz,
                                         fov_deg=field_of_view_deg, isolate=isolate, family=family,
                                         model_id=target.model_id, off=off, path3d=path3d))
            elif extra == "section":
                vps.append(bcf.viewpoint(main, part, zone=zone, view="elevation", section="box", storey_z=sz,
                                         fov_deg=field_of_view_deg, isolate=isolate, family=family,
                                         model_id=target.model_id, off=off, path3d=path3d))
            view_hash = bcf.issue_key("vue", [json.dumps(vps[0].get("perspective_camera")
                                                         or vps[0].get("orthogonal_camera"), sort_keys=True)])[:8]
            # description
            lines = [f"Règle {rule} — {main.get('req_label') or ''}".strip(),
                     f"Contrôle « {check.get('title')} » (check_id {check_id}) du {str(check.get('created_at'))[:10]}."]
            for i in iss[:10]:
                m, t = i.get("measure") or {}, i.get("threshold") or {}
                mv = f" — mesure {m.get('value')} {m.get('unit', '')}" if m.get("value") is not None else ""
                tv = f", seuil {t.get('value')} {t.get('unit', '')} ({t.get('source')})" if t.get("value") is not None else ""
                lines.append(f"• {i['message']}{mv}{tv}")
            if len(iss) > 10:
                lines.append(f"… et {len(iss) - 10} autre(s) anomalie(s) (rapport Excel du contrôle).")
            lines.append(f"Éléments ({len(part)}" + (f" sur {len(elements)}" if len(parts) > 1 else "") + ") :")
            dup = [e for e in part if "#" in e["uuid"]]
            for e in part[:40]:
                el = (idx.elements.get(e["uuid"]) if idx else None) or {}
                rec = recs.get(e["uuid"].split("#", 1)[0])
                lines.append(f"  - {(rec.name if rec else el.get('name')) or '—'} ({el.get('ifc_class') or (rec.ifc_class if rec else '')}) "
                             f"GlobalId {e['uuid'].split('#', 1)[0]}, rôle {e['role']}")
            if len(part) > 40:
                lines.append(f"  … et {len(part) - 40} autre(s).")
            if dup:
                lines.append(f"GlobalId en double dans le fichier IFC : {', '.join(e['uuid'] for e in dup[:10])} "
                             "(clé GlobalId#idSTEP) — la sélection BCF ne peut pas distinguer les occurrences.")
            if len(parts) > 1:
                sel_name = f"bcf_{bcf.issue_key(rule, [e['uuid'] for e in elements])}"
                _save_selection(call, sel_name, target, [recs[u] for u in dict.fromkeys(
                    e["uuid"].split("#", 1)[0] for e in elements) if u in recs], {"bcf": key},
                    f"Éléments de l'anomalie {rule} (topic découpé)")
                lines.append(f"Topic découpé ({k + 1}/{len(parts)}) : {len(elements)} éléments en tout, liste complète "
                             f"dans la sélection MCP « {sel_name} ».")
            lines.append(f"Niveau : {storey or '—'}. Maquette : {model.get('name') or doc.get('file_name') or '—'} "
                         f"(IFC du {str(doc.get('created_at') or '?')[:10]}). Vue MCP : {view_hash}.")
            lines.append(f"clé : {key}")
            if family == "clash":
                a = next((e for e in part if e["role"] == "A"), part[0])
                b = next((e for e in part if e["role"] == "B"), part[-1])
                na = (idx.elements.get(a["uuid"]) or {}).get("name") if idx else a["uuid"]
                nb = (idx.elements.get(b["uuid"]) or {}).get("name") if idx else b["uuid"]
                summary = f"« {na} » × « {nb} »"
            else:
                summary = f"{main.get('ifc_class') or ''} « {main.get('name') or main.get('uuid') or ''} »".strip()
            suffix = f" ({k + 1}/{len(parts)})" if len(parts) > 1 else ""
            ttl = f"[{rule}] {summary} — {storey or '—'}{suffix}"
            if title:
                ttl = f"{title} — {ttl}"
            ttype = topic_type or ("Clash" if family == "clash" else "Issue")  # déclaré dans le projet s'il manque
            tguid = bcf.topic_guid(target.project_id, target.model_id, key)
            for n, vp in enumerate(vps):
                vp["guid"] = bcf.viewpoint_guid(tguid, n, view_hash)
            payload = {"guid": tguid, "title": ttl[:250], "description": "\n".join(lines)[:6000], "topic_type": ttype,
                       "topic_status": "Open", "priority": bcf.PRIORITY.get(sev, "Low"), "format": "standard",
                       "labels": list(dict.fromkeys([fam["label"], rule] + ([storey] if storey else []) + list(labels or []))),
                       "reference_links": [bcf.reference_urn(rule, [e["uuid"] for e in part], key)], "viewpoints": vps}
            try:
                payload["models"] = [int(target.model_id)]
                for vp in vps:
                    vp["models"] = [int(target.model_id)]
            except (TypeError, ValueError):
                pass
            if assigned_to:
                payload["assigned_to"] = assigned_to
            if due_date:
                payload["due_date"] = due_date
            roles = {e["uuid"]: bcf.role_color(e["role"]) for e in part}
            m, th = main.get("measure") or {}, main.get("threshold") or {}
            meta = {"regle": rule, "libelle_regle": main.get("req_label"), "severite": sev, "niveau": storey,
                    "constat": main.get("message"), "anomalies": len(iss),
                    "mesure": None if m.get("value") is None else f"{m.get('value')} {m.get('unit') or ''}".strip(),
                    "seuil": None if th.get("value") is None else f"{th.get('value')} {th.get('unit') or ''}".strip(),
                    "elements": [{"guid": e["uuid"].split("#", 1)[0], "role": e["role"],
                                  "nom": ((idx.elements.get(e["uuid"]) or {}).get("name") if idx else None),
                                  "classe": ((idx.elements.get(e["uuid"]) or {}).get("ifc_class") if idx else None)}
                                 for e in part[:200]], "check_id": check_id}
            topics.append({"key": key, "payload": payload, "view_hash": view_hash, "roles": roles, "region": zone,
                           "path3d": path3d, "caption": bcf.caption(main), "title": payload["title"], "meta": meta})
    # clés de toutes les anomalies du contrôle (hors filtre) : une anomalie filtrée n'est pas « disparue »
    all_keys = {bcf.issue_key(i.get("rule_id") or i["req_id"], [e["uuid"] for e in _elements([i])])
                for i in check["issues"]} if filtered else set()
    # rapprochement avec les topics existants : d'abord par GUID dérivé de la clé, sinon par reference_links
    by_guid = {str(t.get("guid")): t for t in existing if t.get("guid")}
    by_key: dict[str, dict] = {}
    for t in existing:
        for link in t.get("reference_links") or []:
            k = bcf.key_from_reference(link)
            if k:
                by_key[k] = t
        k = re.search(r"(?m)^clé : ([0-9a-f]{16})$", t.get("description") or "")
        if k:
            by_key.setdefault(k.group(1), t)
    items, keys_now = [], {t["key"] for t in topics}
    for t in topics:
        ex = by_guid.get(t["payload"]["guid"]) or by_key.get(t["key"])
        if ex is None:
            items.append({"action": "create", **t})
        else:
            old = VIEW_RE.search(ex.get("description") or "")
            changed = not old or old.group(1) != t["view_hash"]
            items.append({"action": "comment", **t, "topic_guid": ex.get("guid"),
                          "comment": f"Toujours présent au contrôle du {now_iso()[:10]} (check_id {check_id})."
                                     + (" Vue recalculée : nouveau point de vue." if changed else ""),
                          "new_viewpoint": changed})
    for t in existing:  # topics du MCP dont l'anomalie a disparu
        k = next((x for x in (bcf.key_from_reference(link) for link in t.get("reference_links") or []) if x), None)
        if not k:
            continue
        rule_labels = set(t.get("labels") or [])
        if k in keys_now or str(t.get("topic_status") or "").lower() in CLOSED or not (rule_labels & rules_of_check):
            continue
        if filtered and k in all_keys:  # anomalie toujours présente, simplement hors du filtre (gravité, règle, type)
            continue
        items.append({"action": "resolve", "key": k, "topic_guid": t.get("guid"), "title": t.get("title"),
                      "comment": f"Anomalie absente du contrôle du {now_iso()[:10]} (check_id {check_id}) : "
                                 "proposée au statut Resolved."})
    # extensions du projet : utiliser les équivalents du projet (Opened pour Open…), déclarer seulement ce qui manque
    resolved_value = (project_value("topic_status", "Resolved", ext) if ext is not None else None) or "Resolved"
    for i in items:
        if i["action"] == "resolve":
            i["status_value"] = resolved_value
        if i["action"] != "create" or ext is None:
            continue
        pl = i["payload"]
        for kind, field in (("topic_type", "topic_type"), ("topic_status", "topic_status"), ("priority", "priority")):
            eq = project_value(kind, pl[field], ext)
            if eq:
                pl[field] = eq
    want = {"topic_type": sorted({i["payload"]["topic_type"] for i in items if i["action"] == "create"}),
            "priority": list(WANTED_PRIORITIES), "topic_status": list(WANTED_STATUSES),
            "topic_label": sorted({lb for i in items if i["action"] == "create" for lb in i["payload"]["labels"]})}
    missing = ({k: [v for v in vals if not project_value(k, v, ext)] for k, vals in want.items()} if ext is not None
               else {})
    missing = {k: v for k, v in missing.items() if v}
    if sum(1 for i in items if i["action"] == "create") > max_topics:
        raise ValueError(f"{sum(1 for i in items if i['action'] == 'create')} topics seraient créés (> max_topics="
                         f"{max_topics}). Regrouper (check_group_by) ou augmenter max_topics.")
    # images : seulement pour ce qui sera envoyé (création, ou nouveau point de vue)
    timings = []
    if snapshot:
        for it in items:
            if it["action"] == "create" or (it["action"] == "comment" and it.get("new_viewpoint")):
                # une seule image par topic : le viewer BIMData n'ouvre directement (caméra, maquette, sélection)
                # qu'un topic à un seul point de vue illustré ; les vues complémentaires restent sans image
                vps = it["payload"]["viewpoints"][:1]
                it["snapshot_files"] = []
                for n, vp in enumerate(vps):
                    out = files.new_output_path(st, f"bcf_{it['key']}_{n}.png")
                    jobs.append({"viewpoint": vp, "roles": it["roles"], "region": it["region"], "lines": [it["path3d"]]
                                 if it.get("path3d") else None, "caption": it["caption"], "offset": list(off),
                                 "out": str(out), "_item": it, "_vp": vp})
        n_img = len(jobs)

        def img_progress(done, total):
            if progress:
                progress(done, total, f"images des points de vue : {done} / {total}")

        results = _render(idx, [{k: v for k, v in j.items() if not k.startswith("_")} for j in jobs], img_progress)
        if progress:
            progress(n_img, n_img, "images terminées, enregistrement du plan")
        for j, r in zip(jobs, results, strict=True):
            if r.get("error"):
                warnings.append(f"Image en échec ({j['_item']['title']}) : {r['error']}")
                continue
            meta = files.register_file(st, Path(r["path"]), kind="bcf-snapshot", label=j["_item"]["title"])
            j["_vp"]["snapshot"] = {"snapshot_type": "png", "snapshot_data": "@file:" + r["path"]}
            j["_item"]["snapshot_files"].append({"file_id": meta["file_id"], "name": meta["name"], "path": r["path"],
                                                 "bytes": r["bytes"]})
            timings.append(r["duree_s"])
    for it in items:
        for k in ("roles", "region", "path3d", "caption"):
            it.pop(k, None)
    if missing:  # premier élément du plan : déclaration des extensions manquantes
        items.insert(0, {"action": "extensions", "title": "Extensions du projet BCF à déclarer", "missing": missing,
                         "existing": ext})
    counts = {a: sum(1 for i in items if i["action"] == a) for a in ("create", "comment", "resolve")}
    summary = {"check_id": check_id, "topics_a_creer": counts["create"], "topics_a_commenter": counts["comment"],
               "topics_a_cloturer": counts["resolve"], "images": len(timings),
               "duree_image_mediane_s": round(statistics.median(timings), 3) if timings else None,
               "rapprochement": "actif" if dedupe else "désactivé",
               "topics_existants_lus": len(existing), "extensions_a_declarer": missing,
               "extensions_lues": ext is not None}
    risks = [f"Crée {counts['create']} topic(s) BCF dans le projet (visibles par l'équipe)."]
    if missing:
        risks.append("Déclare dans le projet BCF les valeurs manquantes : " + " ; ".join(
            f"{k} {', '.join(v)}" for k, v in missing.items()) + " (réglage du projet ; si refusé, valeur existante la "
            "plus proche, signalée dans la description).")
    if ext is None:
        risks.append("Extensions du projet illisibles : type, priorité et libellés envoyés tels quels.")
    if counts["comment"]:
        risks.append(f"Commente {counts['comment']} topic(s) existant(s) (anomalie toujours présente).")
    if counts["resolve"]:
        risks.append(f"Passe {counts['resolve']} topic(s) au statut Resolved (anomalie disparue).")
    plan = WritePlan(kind=WritePlanKind.BCF_TOPICS, target=target.to_dict(), summary=summary, items=items,
                     risks=risks + warnings[:10])
    out = plans.save_plan(st, plan)
    out["miniatures"] = [{"title": i["title"], "action": i["action"],
                          "images": [files.download_url(st, f["file_id"], f["name"]) for f in i.get("snapshot_files", [])]}
                         for i in items[:20]]
    return out


# ── Réparation des topics déjà envoyés (ouverture dans le viewer BIMData) ─────────────

def _is_mcp_viewpoint(vp: dict) -> bool:
    return str(vp.get("originating_system") or "").startswith("MCP BIMData")


def prepare_bcf_viewer_repair(call) -> dict:
    """Plan de réparation des topics créés par le MCP avant la correction : point de vue principal marqué « ifc3d »
    (fenêtre 3D du viewer) et suppression des points de vue complémentaires illustrés (le viewer n'ouvre directement
    qu'un topic à une seule image). Ne touche ni aux topics d'autres origines ni aux commentaires. N'écrit rien."""
    from .service import _ctx, make_client

    target, _, _, _ = _ctx(call)
    client = make_client(target, call.identity)
    topics = client.list_bcf_full_topics() or []
    items = []
    for t in topics:
        vps = [v for v in t.get("viewpoints") or [] if _is_mcp_viewpoint(v)]
        if not vps:
            continue
        vps.sort(key=lambda v: (v.get("index") is None, v.get("index") or 0))
        others = [v for v in vps[1:] if v.get("snapshot")]
        items.append({"action": "repair_viewer", "title": t.get("title") or t.get("guid"), "topic_guid": t["guid"],
                      "index": t.get("index"), "patch_viewpoints": [v["guid"] for v in vps],
                      "delete_viewpoints": [v["guid"] for v in others]})
    n_del = sum(len(i["delete_viewpoints"]) for i in items)
    summary = {"topics_lus": len(topics), "topics_a_reparer": len(items),
               "points_de_vue_corriges": sum(len(i["patch_viewpoints"]) for i in items),
               "vues_complementaires_supprimees": n_del}
    if not items:
        return {"summary": summary, "message": "Aucun topic du MCP à réparer : ils s'ouvrent déjà dans le viewer."}
    risks = [f"Modifie {len(items)} topic(s) BCF créés par le MCP : origine des points de vue « ifc3d » (ouverture "
             "dans la fenêtre 3D avec la maquette, la caméra et la sélection)."]
    if n_del:
        risks.append(f"Supprime {n_del} vue(s) complémentaire(s) (vue de plan ou de coupe ajoutée par le MCP) : le "
                     "viewer BIMData n'ouvre directement qu'un topic à une seule image. La vue principale, le titre, "
                     "la description, les commentaires et les statuts sont conservés.")
    plan = WritePlan(kind=WritePlanKind.BCF_TOPICS, target=target.to_dict(), summary=summary, items=items,
                     risks=risks)
    return plans.save_plan(call.state, plan)


# ── Rapport Excel des topics BCF ─────────────────────────────────────────

ACTION_LABEL = {"create": "Créé", "comment": "Commenté (toujours présent)", "resolve": "Clôture proposée"}


def _thumb(path: str, out_dir: Path, w: int = 240) -> Path | None:
    try:
        from PIL import Image

        im = Image.open(path)
        im.thumbnail((w, int(w * 9 / 16)))
        dest = out_dir / ("thumb_" + Path(path).name)
        im.save(dest, format="PNG", optimize=True)
        return dest
    except Exception:  # noqa: BLE001 — image absente : la ligne reste sans miniature
        return None


def export_bcf_report(call, plan_id: str, title: str | None = None) -> dict:
    """Rapport Excel à la charte des topics BCF d'un plan envoyé : synthèse (par priorité, règle, niveau, statut) et
    liste des topics avec leur image, constat, éléments, GlobalId, clé et GUID."""
    from collections import Counter

    from .charte import Col, Livrable, nom_fichier
    from .config import get_settings
    from .service import _ctx, _doc_ctx, _livrable

    st = call.state
    data = plans.load_plan(st, plan_id)
    if data.get("kind") != "bcf_topics" and "bcf" not in str(data.get("kind", "")).lower():
        raise ValueError("Ce plan n'est pas un plan de topics BCF.")
    target, _, snap, _ = _ctx(call)
    items = [i for i in data["items"] if i.get("action") in ("create", "comment", "resolve")]
    if not items:
        raise ValueError("Aucun topic dans ce plan.")
    title = title or "Topics BCF de l'audit"
    ctx = _doc_ctx(call, snap, target, "Rapport BCF", title)
    path = files.new_output_path(st, nom_fichier("BCF", ctx.projet, ctx.maquette))
    viewer = target.viewer_url(get_settings().platform_url)
    rows, thumbs = [], []
    for it in items:
        pl, meta, res = it.get("payload") or {}, it.get("meta") or {}, it.get("_result") or {}
        sent = "Envoyé" if it.get("_done") else ("Non envoyé" if data.get("status") else "Plan non appliqué")
        els = meta.get("elements") or []
        rows.append([res.get("index") or "", pl.get("title") or it.get("title"), meta.get("regle"),
                     meta.get("severite"), res.get("priority") or pl.get("priority"),
                     res.get("topic_status") or pl.get("topic_status"), res.get("topic_type") or pl.get("topic_type"),
                     meta.get("niveau"), meta.get("constat"), meta.get("mesure"), meta.get("seuil"),
                     " | ".join(f"{e.get('nom') or '—'} ({e.get('classe') or ''}, {e.get('role')})" for e in els[:20]),
                     " | ".join(e["guid"] for e in els[:50]), ACTION_LABEL.get(it["action"], it["action"]), sent,
                     it.get("key"), res.get("guid") or pl.get("guid") or it.get("topic_guid"), ""])
        img = next((f["path"] for f in it.get("snapshot_files") or [] if Path(f["path"]).exists()), None)
        thumbs.append(_thumb(img, path.parent) if img else None)
    by = lambda k: Counter(r[k] or "—" for r in rows)  # noqa: E731
    lv = Livrable(path, ctx)
    n_sent = sum(1 for r in rows if r[14] == "Envoyé")
    lv.synthese(kpis=[("Topics", len(rows), "integer"), ("Envoyés", n_sent, "integer"),
                      ("Créés", sum(1 for i in items if i["action"] == "create"), "integer"),
                      ("Commentés", sum(1 for i in items if i["action"] == "comment"), "integer"),
                      ("Clôtures", sum(1 for i in items if i["action"] == "resolve"), "integer")],
                intro=f"Topics BCF issus des contrôles de la maquette (plan {plan_id}). Chaque topic porte une vue "
                      f"cadrée, les éléments colorés par rôle et une image. Visionneuse : {viewer}")
    for label, k in (("Par priorité", 4), ("Par règle", 2), ("Par niveau", 7), ("Par statut", 5)):
        lv.tableau(label, [Col(label.replace("Par ", "").capitalize()), Col("Topics", kind="integer")],
                   [[v, n] for v, n in by(k).most_common()])
    cols = [Col("N°", "Topic", kind="integer", width=6), Col("Titre", "Topic", width=48), Col("Règle", "Topic", width=8),
            Col("Sévérité", "Topic", kind="severity", width=10), Col("Priorité", "BCF", width=10),
            Col("Statut", "BCF", width=10), Col("Type", "BCF", width=9), Col("Niveau", "Localisation", width=14),
            Col("Constat", "Anomalie", width=50), Col("Mesure", "Anomalie", width=10), Col("Seuil", "Anomalie", width=10),
            Col("Éléments (nom, classe, rôle)", "Éléments", width=50), Col("GlobalId", "Éléments", width=26),
            Col("Action", "Suivi", width=16), Col("Envoi", "Suivi", width=11), Col("Clé d'anomalie", "Suivi", width=18),
            Col("GUID du topic", "Suivi", width=38), Col("Image", "Vue", width=36)]
    ws = lv.feuille("Topics BCF", cols, rows, freeze_cols=2,
                    description="Un topic par ligne, avec la miniature de son point de vue.")
    for r, th in enumerate(thumbs):
        if th:
            ws.set_row(2 + r, 105)
            ws.insert_image(2 + r, len(cols) - 1, str(th), {"x_offset": 4, "y_offset": 4, "object_position": 1})
    lv.lisez_moi(f"Rapport des topics BCF du plan {plan_id} : synthèse par priorité, règle, niveau et statut, puis la "
                 "liste des topics avec leur image. La clé d'anomalie et le GUID permettent de retrouver le topic dans "
                 "BIMData ou dans Solibri, et de suivre l'anomalie d'un contrôle à l'autre.")
    lv.meta({"format": "bimdata-mcp/bcf-report/v1", "plan_id": plan_id})
    lv.close()
    meta = files.register_file(st, path, kind="report", label=title)
    return {"livrable": _livrable(meta, type_livrable="Rapport BCF (Excel)", titre=title, feuilles=["Synthèse",
                                                                                                   "Topics BCF"]),
            "topics": len(rows), "envoyes": n_sent, "synthese": {"par_priorite": dict(by(4)), "par_regle": dict(by(2))}}


def report_offer(plan_id: str, n: int) -> dict:
    return {"question": f"Voulez-vous un rapport Excel des {n} topic(s) BCF (synthèse par priorité, règle et niveau, et "
                        "liste des topics avec leur image, constat, éléments et GlobalId) ?",
            "appel": {"outil": "export_bcf_report", "arguments": {"plan_id": plan_id}}}
