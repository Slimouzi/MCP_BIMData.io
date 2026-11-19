"""Publication de documents dans la GED du projet BIMData (dossiers et fichiers).

Après un audit, le rapport PDF et son classeur Excel peuvent être déposés dans un dossier de la GED. Le dossier
proposé par défaut regroupe les audits d'une maquette (« Audits BIM/<maquette> ») ; l'utilisateur peut en choisir un
autre (chemin « A/B/C », créé au besoin). Comme toute écriture dans BIMData, la publication passe par un plan montré
à l'utilisateur et appliqué seulement après son accord (``apply_plan``).
"""

from __future__ import annotations

import re
from pathlib import Path

from .state import new_id, now_iso

KIND = "ged_documents"
ROOT = "Audits BIM"


def default_folder(model_name: str) -> str:
    stem = re.sub(r"\.(ifc|ifczip|ifcxml)$", "", str(model_name or "Maquette"), flags=re.I).strip() or "Maquette"
    return f"{ROOT}/{_clean(stem)}"


def _clean(name: str) -> str:
    return re.sub(r"[\\/:*?\"<>|]+", "-", name).strip(" .-")[:120] or "Dossier"


def split_path(path: str) -> list[str]:
    parts = [_clean(p) for p in re.split(r"[/\\]+", str(path or "")) if p.strip()]
    if not parts:
        raise ValueError("Nom de dossier vide.")
    return parts


def folder_index(folders: list[dict]) -> dict[tuple, int]:
    """(parent_id, nom en minuscules) → id de dossier."""
    return {(f.get("parent_id"), str(f.get("name") or "").strip().lower()): f["id"] for f in folders if f.get("id")}


def resolve(folders: list[dict], parts: list[str], root_id=None) -> tuple[int | None, list[str]]:
    """Dossier existant le plus profond du chemin et noms des sous-dossiers restant à créer."""
    idx = folder_index(folders)
    cur = root_id
    for k, name in enumerate(parts):
        nxt = idx.get((cur, name.lower()))
        if nxt is None and cur == root_id and root_id is not None:
            nxt = idx.get((None, name.lower()))  # racine exprimée par parent_id nul
        if nxt is None:
            return cur, parts[k:]
        cur = nxt
    return cur, []


def build_plan(target: dict, folder: str, docs: list[dict], folders: list[dict], documents: list[dict],
               root_id=None) -> dict:
    """Plan de publication : dossiers à créer puis documents à déposer (un document de même nom déjà présent dans le
    dossier n'est pas redéposé)."""
    parts = split_path(folder)
    parent, missing = resolve(folders, parts, root_id)
    items = [{"action": "folder", "name": n, "path": "/".join(parts[: len(parts) - len(missing) + k + 1])}
             for k, n in enumerate(missing)]
    existing = {str(d.get("name") or d.get("file_name") or "").lower() for d in documents
                if not missing and d.get("parent_id") == parent}
    skipped = []
    for d in docs:
        if d["name"].lower() in existing:
            skipped.append(d["name"])
            continue
        items.append({"action": "upload", "name": d["name"], "file_id": d.get("file_id"), "path": d["path"],
                      "type": d.get("type", "")})
    n_up = sum(1 for i in items if i["action"] == "upload")
    if not n_up:
        raise ValueError(f"Rien à publier : {', '.join(skipped) or 'aucun fichier'} déjà présent(s) dans « {folder} ».")
    risks = [f"Dépose {n_up} document(s) dans la GED du projet BIMData, dossier « {'/'.join(parts)} » (visibles "
             "par les membres du projet selon les droits du dossier)."]
    if missing:
        risks.append(f"Crée {len(missing)} dossier(s) : {', '.join(missing)}.")
    plan_id = new_id("plan")
    return {"plan_id": plan_id, "kind": KIND, "target": target, "created_at": now_iso(), "status": "prepared",
            "summary": {"dossier": "/".join(parts), "dossier_existant": not missing, "dossiers_a_creer": missing,
                        "documents": [i["name"] for i in items if i["action"] == "upload"],
                        "deja_presents": skipped, "parent_id": parent},
            "risks": risks, "items": items}


def apply(client, data: dict, save=None) -> dict:
    """Crée les dossiers manquants puis dépose les documents ; reprenable (``_done``)."""
    parent = data["summary"].get("parent_id")
    created, errors = [], []
    for it in data["items"]:
        if it.get("_done"):
            if it["action"] == "folder":
                parent = it.get("_id", parent)
            continue
        try:
            if it["action"] == "folder":
                f = client.create_folder(it["name"], parent_id=parent) or {}
                parent = f.get("id")
                it["_id"] = parent
                created.append({"action": "folder", "title": it["path"], "id": parent})
            else:
                doc = client.upload_document(Path(it["path"]), name=it["name"], parent_id=parent) or {}
                created.append({"action": "upload", "title": it["name"], "bimdata_document_id": doc.get("id")})
            it["_done"] = True
        except Exception as exc:  # noqa: BLE001 — un dépôt en échec n'arrête pas les autres
            errors.append({"uuid": it["name"], "action": it["action"], "message": str(exc)[:300]})
            if it["action"] == "folder":
                break  # sans le dossier, inutile de déposer
        if save:
            save()
    return {"plan_id": data["plan_id"], "kind": KIND, "succeeded": len(created), "failed": len(errors),
            "errors": errors, "created": created, "dossier": data["summary"]["dossier"]}
