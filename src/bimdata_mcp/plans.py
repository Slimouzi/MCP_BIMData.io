"""Plans d'écriture (pattern prepare → apply de ``bim_core.write_plan``).

Aucune écriture BIMData n'a lieu sans un plan préparé, persisté, relu, puis
appliqué avec ``confirm=True``. Le plan fige la cible : l'appliquer sur une
autre maquette est refusé.
"""

from __future__ import annotations

from collections import defaultdict
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
        "next_step": "Relire le plan puis appeler apply_plan(plan_id, confirm=True) pour écrire dans BIMData.",
    }


def _short(item: dict) -> dict:
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


def _apply_topics(client: BIMDataClient, items: list[dict]) -> tuple[list, list]:
    created, errors = [], []
    for it in items:
        try:
            res = client.create_bcf_full_topic(it["payload"]) or {}
            created.append({"title": it["payload"].get("title"), "guid": res.get("guid")})
        except Exception as exc:  # noqa: BLE001 — échec unitaire, on continue
            errors.append({"uuid": it["payload"].get("title", "?"), "message": str(exc)[:300]})
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


def apply_plan(state: UserState, client: BIMDataClient, target: BIMDataTarget, plan_id: str) -> dict:
    s = get_settings()
    if not s.allow_writes:
        raise PermissionError("Écritures désactivées sur ce serveur (MCP_ALLOW_WRITES=false).")
    data = load_plan(state, plan_id)
    if data.get("status") == "applied":
        raise ValueError("Ce plan a déjà été appliqué. Préparer un nouveau plan si nécessaire.")
    if BIMDataTarget.from_dict(data["target"]).key != target.key:
        raise ValueError(
            f"Le plan vise la maquette {data['target']} mais la maquette active est {target.to_dict()} : "
            "refusé. Activer la bonne maquette (set_active_model) puis réessayer."
        )
    kind = WritePlanKind(data["kind"])
    items = data["items"]
    result: dict[str, Any]
    if kind in (WritePlanKind.SMART_VIEWS, WritePlanKind.BCF_TOPICS):
        created, errors = _apply_topics(client, items)
        ar = ActionResult(plan_id=plan_id, kind=kind, succeeded=len(created), failed=len(errors), errors=errors)
        result = {**ar.model_dump(mode="json"), "created": created}
    elif kind == WritePlanKind.PROPERTY_INJECTION:
        done, errors, skipped = _apply_properties(client, items)
        ar = ActionResult(plan_id=plan_id, kind=kind, succeeded=len(done), failed=len(errors), skipped=skipped,
                          impacted_uuids=sorted({d["uuid"] for d in done}), errors=errors)
        result = ar.model_dump(mode="json")
        result["impacted_uuids"] = result["impacted_uuids"][:200]
        mark_dirty(target)
    else:
        raise ValueError(f"Type de plan non pris en charge : {kind.value}")
    data["status"] = "applied" if not result.get("failed") else "partially_applied"
    data["result"] = {k: v for k, v in result.items() if k != "impacted_uuids"}
    state.save("plans", plan_id, data, gz=True)
    state.journal("apply_plan", plan_id=plan_id, kind=kind.value, target=data["target"],
                  succeeded=result.get("succeeded"), failed=result.get("failed"))
    return result
