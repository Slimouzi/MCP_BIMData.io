"""Plans d'écriture (pattern prepare → apply de ``bim_core.write_plan``).

Aucune écriture BIMData n'a lieu sans un plan préparé, persisté, relu, puis
appliqué avec ``confirm=True``. Le plan fige la cible : l'appliquer sur une
autre maquette est refusé.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from bim_core.write_plan import ActionResult, WritePlan, WritePlanKind

from .bimdata import BIMDataClient, mark_dirty
from .config import get_settings
from .state import UserState
from .target import BIMDataTarget

_PREVIEW = 15


def save_plan(state: UserState, plan: WritePlan) -> dict:
    s = get_settings()
    if len(plan.items) > s.max_plan_items:
        raise ValueError(
            f"Plan trop volumineux ({len(plan.items)} opérations > {s.max_plan_items}). "
            "Réduire la sélection ou découper le traitement."
        )
    state.save("plans", plan.plan_id, {**plan.model_dump(mode="json"), "status": "prepared"}, gz=True)
    return plan_preview(plan)


def plan_preview(plan: WritePlan | dict) -> dict:
    p = plan if isinstance(plan, dict) else plan.model_dump(mode="json")
    return {
        "plan_id": p["plan_id"],
        "kind": p["kind"],
        "target": p["target"],
        "summary": p["summary"],
        "risks": p["risks"],
        "n_items": len(p["items"]),
        "preview": [_short(i) for i in p["items"][:_PREVIEW]],
        "status": p.get("status", "prepared"),
        "next_step": "Montrer ce plan à l'utilisateur et lui demander son accord. N'appeler "
                     "apply_plan(plan_id, confirm=True) qu'après son accord explicite.",
    }


def _short(item: dict) -> dict:
    if item.get("action") in ("folder", "upload"):  # publication dans la GED
        return {"action": "Créer le dossier" if item["action"] == "folder" else "Déposer",
                "name": item.get("path") if item["action"] == "folder" else item.get("name"), "type": item.get("type")}
    if "action" in item:  # topic BCF préparé depuis un contrôle
        pl = item.get("payload") or {}
        vps = pl.get("viewpoints") or []
        return {"action": item["action"], "title": item.get("title"), "key": item.get("key"),
                "topic_guid": item.get("topic_guid"), "priority": pl.get("priority"),
                "points_de_vue": len(vps), "elements": len((vps[0].get("components") or {}).get("selection") or [])
                if vps else 0, "images": len(item.get("snapshot_files") or []), "comment": item.get("comment")}
    if "payload" in item:
        pl = item["payload"]
        n = sum(len(g.get("components", [])) for vp in pl.get("viewpoints", [])
                for g in (vp.get("components", {}).get("coloring") or []))
        return {"title": pl.get("title"), "format": pl.get("format", "standard"), "n_components": n}
    return item


def load_plan(state: UserState, plan_id: str) -> dict:
    data = state.load("plans", plan_id)
    if not data:
        raise ValueError(f"Plan {plan_id!r} introuvable (voir list_plans).")
    return data


# ── Exécuteurs ──────────────────────────────────────────────────────────


def _embed_snapshots(payload: dict) -> dict:
    """Images des points de vue : chemin local (« @file:… ») → PNG en base64, au moment de l'envoi."""
    import base64
    import copy

    out = copy.deepcopy(payload)
    for vp in out.get("viewpoints") or ([out] if "snapshot" in out else []):
        snap = vp.get("snapshot") or {}
        data = snap.get("snapshot_data")
        if isinstance(data, str) and data.startswith("@file:"):
            snap["snapshot_data"] = base64.b64encode(Path(data[6:]).read_bytes()).decode()
    return out


def _without_pins(payload: dict) -> dict:
    for vp in payload.get("viewpoints") or [payload]:
        vp.pop("pins", None)
    return payload


def _retry(fn, payload):
    """Un essai, puis un second sans les repères (extension BIMData) si le premier échoue."""
    try:
        return fn(payload)
    except Exception:  # noqa: BLE001
        return fn(_without_pins(payload))


_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "normal": 2, "low": 3, "information": 4, "info": 4}


def _adapt_to_project(payload: dict, refused: dict[str, set], existing: dict[str, list]) -> dict:
    """Valeurs que le projet BCF n'a pas pu déclarer (droits insuffisants) : valeur existante la plus proche, et la
    valeur souhaitée écrite dans la description."""
    if not refused:
        return payload
    notes = []

    def nearest(kind: str, wanted: str) -> str | None:
        have = existing.get(kind) or []
        if not have:
            return None
        if kind == "priority":
            r = _PRIORITY_RANK.get(wanted.lower(), 2)
            return min(have, key=lambda v: abs(_PRIORITY_RANK.get(v.lower(), 2) - r))
        for pref in ({"topic_type": ("Issue", "Error", "Request"), "topic_status": ("Open", "Active")}.get(kind) or ()):
            if pref in have:
                return pref
        return have[0]

    for kind, field in (("priority", "priority"), ("topic_type", "topic_type"), ("topic_status", "topic_status")):
        v = payload.get(field)
        if v in refused.get(kind, set()):
            alt = nearest(kind, v)
            if alt:
                payload[field] = alt
            else:
                payload.pop(field, None)
            notes.append(f"{ {'priority': 'priorité', 'topic_type': 'type', 'topic_status': 'statut'}[field]} "
                         f"{v} → {alt or 'non renseigné'}")
    lost = [lb for lb in payload.get("labels") or [] if lb in refused.get("topic_label", set())]
    if lost:
        payload["labels"] = [lb for lb in payload["labels"] if lb not in lost]
        notes.append("libellés non déclarés : " + ", ".join(lost))
    if notes:  # une seule ligne, en fin de description
        payload["description"] = (payload.get("description") or "") + "\nAdaptations au projet BCF : " + " ; ".join(notes)
    return payload


#: Envois simultanés de topics vers BIMData (un topic = une requête full-topic avec ses images).
SEND_WORKERS = 4


def _already_there(exc: Exception) -> bool:
    """Topic déjà créé (même GUID, envoi précédent interrompu) : pas une erreur, pas de doublon."""
    msg = str(exc).lower()
    code = getattr(getattr(exc, "response", None), "status_code", None)
    return code == 409 or ("guid" in msg and ("exist" in msg or "unique" in msg or "already" in msg))


def _apply_one(client: BIMDataClient, it: dict, refused: dict, existing: dict) -> dict:
    action = it.get("action", "create")
    label = it.get("title") or (it.get("payload") or {}).get("title", "?")
    if action == "create":
        payload = _adapt_to_project(_embed_snapshots(it["payload"]), refused, existing)
        try:
            res = _retry(client.create_bcf_full_topic, payload) or {}
        except Exception as exc:  # noqa: BLE001
            if not _already_there(exc):
                raise
            return {"action": "create", "title": label, "key": it.get("key"), "guid": payload.get("guid"),
                    "note": "déjà présent (envoi précédent)"}
        return {"action": "create", "title": label, "key": it.get("key"), "guid": res.get("guid") or payload.get("guid"),
                "index": res.get("index"), "topic_type": payload.get("topic_type"),
                "topic_status": payload.get("topic_status"), "priority": payload.get("priority"),
                "labels": payload.get("labels")}
    if action == "comment":
        vp_guid = None
        if it.get("new_viewpoint"):
            vp = _embed_snapshots({"viewpoints": it["payload"]["viewpoints"][:1]})["viewpoints"][0]
            guid = it["topic_guid"]
            vp_guid = (_retry(lambda v, g=guid: client.add_bcf_viewpoint(g, v), vp) or {}).get("guid") or vp.get("guid")
        client.add_bcf_comment(it["topic_guid"], it["comment"], viewpoint_guid=vp_guid)
        return {"action": "comment", "title": label, "key": it.get("key"), "guid": it["topic_guid"]}
    if action == "resolve":
        client.update_bcf_topic(it["topic_guid"], {"topic_status": it.get("status_value") or "Resolved"})
        client.add_bcf_comment(it["topic_guid"], it["comment"])
        return {"action": "resolve", "title": label, "key": it.get("key"), "guid": it["topic_guid"]}
    if action == "repair_viewer":
        for g in it.get("patch_viewpoints") or []:
            client.update_bcf_viewpoint(it["topic_guid"], g, {"originating_system": "ifc3d"})
        for g in it.get("delete_viewpoints") or []:
            client.delete_bcf_viewpoint(it["topic_guid"], g)
        return {"action": "repair_viewer", "title": label, "guid": it["topic_guid"]}
    raise ValueError(f"action inconnue : {action}")


def _apply_topics(client: BIMDataClient, items: list[dict], progress=None, save=None) -> tuple[list, list]:
    """Extensions d'abord, puis topics envoyés en parallèle. Reprenable : un élément fait est marqué ``_done`` (le
    plan est enregistré au fil de l'eau) et une nouvelle application ne renvoie que le reste."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    created, errors = [], []
    refused: dict[str, set] = {}
    existing: dict[str, list] = {}
    for it in items:
        if it.get("action") != "extensions":
            continue
        existing = {k: list(v) for k, v in (it.get("existing") or {}).items()}
        refused = {k: set(v) for k, v in (it.get("refused") or {}).items()}
        if it.get("_done"):
            for k, vals in (it.get("declared") or {}).items():
                existing.setdefault(k, []).extend(vals)
            continue
        declared: dict[str, list] = {}
        for kind, values in (it.get("missing") or {}).items():
            for v in values:
                try:
                    client.declare_bcf_extension(kind, v)
                    existing.setdefault(kind, []).append(v)
                    declared.setdefault(kind, []).append(v)
                    created.append({"action": "extension", "title": f"{kind} = {v}"})
                except Exception as exc:  # noqa: BLE001 — droits insuffisants : repli sur l'existant
                    refused.setdefault(kind, set()).add(v)
                    errors.append({"uuid": f"{kind} = {v}", "action": "extension",
                                   "message": f"déclaration refusée ({str(exc)[:120]}) : valeur la plus proche"})
        it.update(_done=True, declared=declared, refused={k: sorted(v) for k, v in refused.items()})
    todo = [it for it in items if it.get("action") != "extensions" and not it.get("_done")]
    total = len(todo)
    if progress:
        progress(0, total, f"envoi de {total} élément(s)")
    with ThreadPoolExecutor(max_workers=max(1, min(SEND_WORKERS, total or 1))) as ex:
        futs = {ex.submit(_apply_one, client, it, refused, existing): it for it in todo}
        for done, f in enumerate(as_completed(futs), start=1):
            it = futs[f]
            try:
                rec = f.result()
                created.append(rec)
                it["_done"] = True
                it["_result"] = {k: v for k, v in rec.items() if k not in ("title",)}
            except Exception as exc:  # noqa: BLE001 — échec unitaire, on continue
                errors.append({"uuid": it.get("title") or "?", "key": it.get("key"), "action": it.get("action"),
                               "message": str(exc)[:300]})
            if progress:
                progress(done, total, f"topics envoyés : {done} / {total}")
            if save and done % 10 == 0:
                save()
    return created, errors


_BOOL_TYPES = {"ifcboolean", "boolean", "bool", "ifclogical", "logical"}
_INT_TYPES = {"ifcinteger", "integer", "int", "ifccountmeasure"}


def coerce_value(value: Any, value_type: str | None) -> Any:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    vt = (value_type or "").lower()
    if vt in ("ifclogical", "logical") and str(value).strip().upper() == "UNKNOWN":
        return "UNKNOWN"  # 3e état d'un IfcLogical
    if vt in _BOOL_TYPES:
        from .selection import as_bool

        b = as_bool(value)
        if b is None:
            raise ValueError(f"valeur booléenne attendue, reçu {value!r}")
        return b
    if vt in _INT_TYPES:
        return int(float(str(value).replace(",", ".")))
    if vt.endswith("measure") or vt in {"ifcreal", "real", "float", "number", "double"}:
        return float(str(value).replace(",", "."))
    if isinstance(value, float) and value.is_integer() and vt in {"", "string", "ifclabel", "ifctext", "ifcidentifier"}:
        return str(int(value))
    return value if isinstance(value, (bool, int, float)) else str(value)


def _bimdata_value_type(value_type: str | None, value: Any) -> str:
    vt = (value_type or "").lower()
    if vt in _BOOL_TYPES or isinstance(value, bool):
        return "boolean"
    if vt in _INT_TYPES or (isinstance(value, int) and not isinstance(value, bool)):
        return "integer"
    if vt.endswith("measure") or vt in {"ifcreal", "real", "float", "number"} or isinstance(value, float):
        return "float"
    return "string"


def _find(psets: list[dict], name: str) -> dict | None:
    for p in psets:
        if (p.get("name") or "").lower() == name.lower():
            return p
    return None


def _find_prop(pset: dict, name: str) -> dict | None:
    for prop in pset.get("properties") or []:
        if ((prop.get("definition") or {}).get("name") or "").lower() == name.lower():
            return prop
    return None


def _apply_properties(client: BIMDataClient, items: list[dict]) -> tuple[list, list, int]:
    """Écrit les corrections, élément par élément.

    Pour chaque changement : PATCH de la propriété si elle existe (l'API
    reconstruit un pset propre à l'élément), sinon ajout de la propriété au
    pset, sinon création du pset. Les psets de l'élément sont relus après
    chaque écriture : un PATCH peut changer l'id du pset.
    """
    by_el: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_el[it["uuid"]].append(it)
    done, errors, skipped = [], [], 0
    for uuid, changes in by_el.items():
        try:
            psets = client.get_element_propertysets(uuid)
            element = None
            new_psets: dict[str, list[dict]] = defaultdict(list)
            for ch in changes:
                value = ch["new_value"]
                if ch["kind"] == "attribute":
                    element = element or client.get_element(uuid)
                    attrs = element.get("attributes") or {}
                    prop = _find_prop(attrs, ch["prop"])
                    if attrs.get("id") is None:
                        raise RuntimeError("attributs de l'élément sans identifiant")
                    if prop:
                        client.update_element_property(uuid, attrs["id"], prop["id"], value)
                    else:
                        client.create_element_property(uuid, attrs["id"], ch["prop"], value,
                                                       _bimdata_value_type(ch.get("value_type"), value))
                    element = None
                    done.append(ch)
                    continue
                pset = _find(psets, ch["pset"])
                if pset is None:
                    new_psets[ch["pset"]].append(ch)
                    continue
                prop = _find_prop(pset, ch["prop"])
                if prop is not None:
                    if prop.get("value") == value:
                        skipped += 1
                        continue
                    client.update_element_property(uuid, pset["id"], prop["id"], value)
                else:
                    client.create_element_property(uuid, pset["id"], ch["prop"], value,
                                                   _bimdata_value_type(ch.get("value_type"), value))
                done.append(ch)
                psets = client.get_element_propertysets(uuid)
            for pset_name, chs in new_psets.items():
                client.write_element_propertyset(uuid, {
                    "name": pset_name,
                    "description": f"Créé par {ORIGINATING}",
                    "properties": [
                        {"definition": {"name": c["prop"], "value_type": _bimdata_value_type(c.get("value_type"), c["new_value"]),
                                        "type": "IfcPropertySingleValue"}, "value": c["new_value"]}
                        for c in chs
                    ],
                })
                done.extend(chs)
        except Exception as exc:  # noqa: BLE001 — erreur unitaire par élément
            errors.append({"uuid": uuid, "message": str(exc)[:300]})
    return done, errors, skipped


ORIGINATING = "bimdata-mcp"

_RELATIONS_CHUNK = 500

AUTHORING_REMINDER = (
    "Reporter aussi ces corrections dans le logiciel auteur (sur le type, dans Revit) : sinon elles seront "
    "écrasées au prochain export IFC."
)


def _same_code(c: dict, system: str, notation: str) -> bool:
    return ((c.get("name") or "").strip().lower() == system.strip().lower()
            and (c.get("notation") or "").strip() == notation.strip())


def _apply_classifications(client: BIMDataClient, items: list[dict]) -> tuple[list, list, int]:
    """``items`` : ``[{uuid, system, notation, title?, replace_existing?}]``.

    Par code : le trouver dans la maquette (ou le créer) → l'associer aux éléments en bulk →
    retirer l'ancien code du même système (une association n'en remplace pas une autre).
    L'association précède le retrait : en cas d'échec, l'élément garde son ancien code.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        groups[(it["system"], it["notation"], it.get("title") or "")].append(it)
    existing = list(client.list_model_classifications() or [])
    done, errors, skipped = [], [], 0
    for (system, notation, title), its in groups.items():
        try:
            code = next((c for c in existing if _same_code(c, system, notation)), None)
            if code is None:
                code = client.create_model_classification(name=system, notation=notation, title=title or None)
                existing.append(code)
            code_id = code["id"]
        except Exception as exc:  # noqa: BLE001
            errors.extend({"uuid": it["uuid"], "message": f"code {system}:{notation} — {str(exc)[:250]}"} for it in its)
            continue
        todo, old_ids = [], {}
        for it in its:
            try:
                current = client.get_element(it["uuid"]).get("classifications") or []
            except Exception as exc:  # noqa: BLE001
                errors.append({"uuid": it["uuid"], "message": str(exc)[:300]})
                continue
            if it.get("replace_existing", True):
                old_ids[it["uuid"]] = [c["id"] for c in current if c.get("id") != code_id
                                       and (c.get("name") or "").strip().lower() == system.strip().lower()]
            if any(c.get("id") == code_id for c in current):
                skipped += 1
            else:
                todo.append(it)
        failed: set[str] = set()
        for i in range(0, len(todo), _RELATIONS_CHUNK):
            chunk = todo[i:i + _RELATIONS_CHUNK]
            try:
                client.assign_classification_elements(
                    [{"element_uuid": it["uuid"], "classification_id": code_id} for it in chunk])
                done.extend(chunk)
            except Exception as exc:  # noqa: BLE001
                failed.update(it["uuid"] for it in chunk)
                errors.extend({"uuid": it["uuid"], "message": str(exc)[:300]} for it in chunk)
        for uuid, ids in old_ids.items():
            if uuid in failed:
                continue  # association échouée : on garde l'ancien code
            for old_id in ids:
                try:
                    client.remove_element_classification(uuid, old_id)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"uuid": uuid, "message": f"retrait de l'ancien code {old_id} — {str(exc)[:250]}"})
    return done, errors, skipped


def _cls_system(c: dict) -> str:
    return (c.get("name") or c.get("type") or "").strip().lower()


def _remove_classifications(client: BIMDataClient, items: list[dict]) -> tuple[list, list, int]:
    """``items`` : ``[{uuid, system, notation?}]`` — retire l'association (le code reste dans le projet).

    Sans ``notation`` : tous les codes du système sont retirés de l'élément. L'état est relu
    sur l'élément (pas sur le snapshot) : un code déjà absent est compté comme ignoré.
    """
    by_uuid: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_uuid[it["uuid"]].append(it)
    done, errors, skipped = [], [], 0
    for uuid, its in by_uuid.items():
        try:
            current = client.get_element(uuid).get("classifications") or []
        except Exception as exc:  # noqa: BLE001
            errors.append({"uuid": uuid, "message": str(exc)[:300]})
            continue
        for it in its:
            system, notation = it["system"].strip().lower(), (it.get("notation") or "").strip()
            matches = [c for c in current if _cls_system(c) == system
                       and (not notation or (c.get("notation") or "").strip() == notation)]
            if not matches:
                skipped += 1
                continue
            for c in matches:
                label = f"{c.get('name') or it['system']}:{c.get('notation')}"
                try:
                    client.remove_element_classification(uuid, c["id"])
                    done.append({"uuid": uuid, "removed": label})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"uuid": uuid, "message": f"retrait de {label} — {str(exc)[:250]}"})
            current = [c for c in current if c not in matches]
    return done, errors, skipped


def apply_plan(state: UserState, client: BIMDataClient, target: BIMDataTarget, plan_id: str, progress=None) -> dict:
    s = get_settings()
    if not s.allow_writes:
        raise PermissionError("Écritures désactivées sur ce serveur (MCP_ALLOW_WRITES=false).")
    data = load_plan(state, plan_id)
    if data.get("status") == "applied":
        raise ValueError("Ce plan a déjà été appliqué. Préparer un nouveau plan si nécessaire.")
    resumed = sum(1 for i in data["items"] if i.get("_done"))
    if BIMDataTarget.from_dict(data["target"]).key != target.key:
        raise ValueError(
            f"Le plan vise la maquette {data['target']} mais la maquette active est {target.to_dict()} : "
            "refusé. Activer la bonne maquette (set_active_model) puis réessayer."
        )
    kind = WritePlanKind(data["kind"])
    items = data["items"]
    result: dict[str, Any]
    if kind in (WritePlanKind.SMART_VIEWS, WritePlanKind.BCF_TOPICS):
        created, errors = _apply_topics(client, items, progress,
                                        save=lambda: state.save("plans", plan_id, data, gz=True))
        ar = ActionResult(plan_id=plan_id, kind=kind, succeeded=len(created), failed=len(errors), errors=errors)
        result = {**ar.model_dump(mode="json"), "created": created[:300], "deja_faits_avant_reprise": resumed,
                  "restants": sum(1 for i in items if not i.get("_done"))}
    elif kind in (WritePlanKind.PROPERTY_INJECTION, WritePlanKind.CLASSIFICATION_UPDATE):
        props = [i for i in items if i.get("kind") != "classification" and kind == WritePlanKind.PROPERTY_INJECTION]
        cls = [{"uuid": i["uuid"], **c} for i in items if i.get("kind") == "classification" for c in i["codes"]]
        rm = [{"uuid": i["uuid"], **c} for i in items if i.get("kind") == "classification"
              for c in i.get("remove") or []]
        if kind == WritePlanKind.CLASSIFICATION_UPDATE:
            cls = [i for i in items if not i.get("remove_only")]
            rm = [i for i in items if i.get("remove_only")]
        done, errors, skipped = _apply_properties(client, props) if props else ([], [], 0)
        if cls:
            d2, e2, s2 = _apply_classifications(client, cls)
            done, errors, skipped = done + d2, errors + e2, skipped + s2
        if rm:
            d3, e3, s3 = _remove_classifications(client, rm)
            done, errors, skipped = done + d3, errors + e3, skipped + s3
        ar = ActionResult(plan_id=plan_id, kind=kind, succeeded=len(done), failed=len(errors), skipped=skipped,
                          impacted_uuids=sorted({d["uuid"] for d in done}), errors=errors)
        result = ar.model_dump(mode="json")
        result["impacted_uuids"] = result["impacted_uuids"][:200]
        result["report_logiciel_auteur"] = AUTHORING_REMINDER
        mark_dirty(target)
    else:
        raise ValueError(f"Type de plan non pris en charge : {kind.value}")
    data["status"] = "applied" if not result.get("failed") else "partially_applied"
    data["result"] = {k: v for k, v in result.items() if k != "impacted_uuids"}
    state.save("plans", plan_id, data, gz=True)
    state.journal("apply_plan", plan_id=plan_id, kind=kind.value, target=data["target"],
                  succeeded=result.get("succeeded"), failed=result.get("failed"))
    return result
