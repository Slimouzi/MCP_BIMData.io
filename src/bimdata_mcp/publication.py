"""Payloads Smart Views / BCF depuis des sélections ou des résultats de contrôle.

Structure minimale alignée sur l'UI BIMData (``title`` + ``models`` + ``format`` +
``viewpoints[0].components.coloring``) : tout champ d'issue BCF ajouté à une Smart View la
fait disparaître du panneau dédié du viewer. Aucun vocabulaire propre à un maître d'ouvrage.
"""

from __future__ import annotations

from .charte import HIGH, SERIES, SEVERITY_COLORS, SUCCESS

ORIGINATING_SYSTEM = "bimdata-mcp"

PALETTE = SERIES  # palette de la charte BIMData (états exclus)
STATUS_COLORS = {"conforme": SUCCESS, **SEVERITY_COLORS}
BCF_PRIORITIES = ("Critical", "High", "Medium", "Low", "Information")


def _hex6(color: str) -> str:
    c = (color or "").lstrip("#").upper()
    if len(c) == 8:
        c = c[2:]
    if len(c) != 6 or any(ch not in "0123456789ABCDEF" for ch in c):
        raise ValueError(f"Couleur invalide : {color!r} (attendu #RRGGBB).")
    return c


def _dedupe(uuids) -> list[str]:
    seen, out = set(), []
    for u in uuids:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _model_int(model_id) -> int | None:
    try:
        return int(model_id)
    except (TypeError, ValueError):
        return None


def smartview_single(uuids, *, title: str, color: str, model_id, names: dict[str, str]) -> dict:
    return smartview_groups([{"color": color, "uuids": list(uuids)}], title=title, model_id=model_id, names=names)


def smartview_groups(groups: list[dict], *, title: str, model_id, names: dict[str, str]) -> dict:
    """``groups`` : ``[{label, color, uuids}]`` — un groupe de couleur par entrée."""
    mid = _model_int(model_id)

    def comp(u: str) -> dict:
        c = {"ifc_guid": u, "originating_system": names.get(u, "")}
        if mid is not None:
            c["authoring_tool_id"] = mid
        return c

    coloring = [
        {"color": "#" + _hex6(g["color"]), "components": [comp(u) for u in _dedupe(g["uuids"])]}
        for g in groups if g.get("uuids")
    ]
    payload = {"title": title, "viewpoints": [{"components": {"coloring": coloring}}],
               "format": "bimdata-smartview"}
    if mid is not None:
        payload["models"] = [mid]
    return payload


def bcf_topic(
    uuids, *, title: str, description: str = "", priority: str = "Medium",
    topic_type: str = "Issue", topic_status: str = "Open", labels: list[str] | None = None,
    assigned_to: str | None = None, color: str = HIGH, model_id=None, due_date: str | None = None,
) -> dict:
    if priority not in BCF_PRIORITIES:
        raise ValueError(f"priority doit être l'une de {BCF_PRIORITIES}.")
    comps = [{"ifc_guid": u, "originating_system": ORIGINATING_SYSTEM} for u in _dedupe(uuids)]
    viewpoint = {
        "originating_system": ORIGINATING_SYSTEM,
        "components": {"selection": comps, "coloring": [{"color": "80" + _hex6(color), "components": comps}]},
    }
    mid = _model_int(model_id)
    payload = {
        "title": title[:250],
        "description": description[:6000],
        "topic_type": topic_type,
        "topic_status": topic_status,
        "priority": priority,
        "labels": labels or [],
        "viewpoints": [viewpoint],
    }
    if assigned_to:
        payload["assigned_to"] = assigned_to
    if due_date:
        payload["due_date"] = due_date
    if mid is not None:
        viewpoint["models"] = [mid]
        payload["models"] = [mid]
    return payload
