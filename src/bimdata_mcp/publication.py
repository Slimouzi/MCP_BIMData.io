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
    """GlobalId uniques, dans l'ordre ; une clé d'index « GlobalId#id » (GlobalId en double) redevient le GlobalId."""
    seen, out = set(), []
    for u in uuids:
        u = str(u).split("#", 1)[0] if u else u
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


def _xyz(v) -> dict:
    return {"x": round(float(v[0]), 4), "y": round(float(v[1]), 4), "z": round(float(v[2]), 4)}


def camera_for_bbox(bbox: list[float], *, fov_deg: float = 60.0, margin: float = 1.6) -> dict:
    """Caméra perspective (BCF) qui cadre ``bbox`` (xmin, ymin, zmin, xmax, ymax, zmax, repère projet IFC) :
    regard en plongée oblique vers le centre, à une distance qui fait tenir la boîte dans le champ."""
    import math

    c = [(bbox[i] + bbox[i + 3]) / 2 for i in range(3)]
    diag = max(math.dist(bbox[:3], bbox[3:]), 1.0)
    dist = margin * (diag / 2) / math.tan(math.radians(fov_deg) / 2)
    d = (-0.55, -0.65, -0.52)  # vers le centre, depuis le sud-ouest, en plongée
    n = math.sqrt(sum(x * x for x in d))
    d = tuple(x / n for x in d)
    eye = [c[i] - d[i] * dist for i in range(3)]
    # vecteur haut orthogonal à la direction (z projeté)
    up = (-d[0] * d[2], -d[1] * d[2], 1 - d[2] * d[2])
    nu = math.sqrt(sum(x * x for x in up))
    return {"camera_view_point": _xyz(eye), "camera_direction": _xyz(d),
            "camera_up_vector": _xyz([x / nu for x in up]), "field_of_view": fov_deg}


def section_box(bbox: list[float], pad: float = 0.5) -> list[dict]:
    """Six plans de coupe autour de ``bbox`` (normale vers l'extérieur : côté coupé)."""
    lo = [bbox[i] - pad for i in range(3)]
    hi = [bbox[i + 3] + pad for i in range(3)]
    planes = []
    for k in range(3):
        n = [0.0, 0.0, 0.0]
        n[k] = -1.0
        loc = [(lo[i] + hi[i]) / 2 for i in range(3)]
        loc[k] = lo[k]
        planes.append({"location": _xyz(loc), "direction": _xyz(n)})
        n = [0.0, 0.0, 0.0]
        n[k] = 1.0
        loc = list(loc)
        loc[k] = hi[k]
        planes.append({"location": _xyz(loc), "direction": _xyz(n)})
    return planes


def bcf_topic(
    uuids, *, title: str, description: str = "", priority: str = "Medium",
    topic_type: str = "Issue", topic_status: str = "Open", labels: list[str] | None = None,
    assigned_to: str | None = None, color: str = HIGH, model_id=None, due_date: str | None = None,
    bbox: list[float] | None = None, isolate: bool = False, clip: bool = False,
) -> dict:
    """``bbox`` : point de vue qui cadre la zone ; ``isolate`` : seuls les éléments du topic visibles ;
    ``clip`` : boîte de coupe autour de ``bbox``."""
    if priority not in BCF_PRIORITIES:
        raise ValueError(f"priority doit être l'une de {BCF_PRIORITIES}.")
    comps = [{"ifc_guid": u, "originating_system": ORIGINATING_SYSTEM} for u in _dedupe(uuids)]
    viewpoint = {
        "originating_system": ORIGINATING_SYSTEM,
        "components": {"selection": comps, "coloring": [{"color": "80" + _hex6(color), "components": comps}]},
    }
    if isolate:
        viewpoint["components"]["visibility"] = {"default_visibility": False, "exceptions": comps}
    if bbox and len(bbox) == 6:
        viewpoint["perspective_camera"] = camera_for_bbox(bbox)
        if clip:
            viewpoint["clipping_planes"] = section_box(bbox)
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
