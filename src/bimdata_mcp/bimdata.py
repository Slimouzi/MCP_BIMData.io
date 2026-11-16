"""Accès BIMData : client, snapshot (avec cache) et écritures complémentaires.

La lecture et les écritures « historiques » (BCF / Smart Views / psets) passent
par ``bimdata-read`` / ``bimdata-write``. Ce module ajoute, **par sous-classe**,
les routes dont ce serveur a besoin et qui n'existent pas encore dans
``bimdata-write`` :

- navigation (clouds, projets, modèles) et GED (documents) ;
- mise à jour **ciblée** d'une propriété existante sur un élément
  (``PATCH …/element/{uuid}/propertyset/{pset}/property/{id}``) — l'API BIMData
  reconstruit alors un pset propre à l'élément, sans toucher aux autres
  éléments qui partageaient le pset d'origine ;
- ajout d'une propriété à un pset existant, création d'un pset.

Candidates naturelles à une remontée dans ``bimdata-write`` (v0.2).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from bim_core.model_snapshot import ModelSnapshot
from bimdata_read.cache import cached_extract_snapshot
from bimdata_write import BIMDataAuthError, BIMDataWriteClient

from .auth import Identity
from .config import get_settings
from .target import BIMDataTarget

__all__ = ["BIMDataClient", "BIMDataAuthError", "make_client", "load_snapshot", "mark_dirty"]


class BIMDataClient(BIMDataWriteClient):
    """Client lecture + écriture étendu pour le serveur MCP BIMData."""

    # ── Transports complémentaires ─────────────────────────────────────
    def _patch(self, path: str, json: Any) -> Any:
        resp = self.session.patch(self._url(path), json=json, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise BIMDataAuthError(f"BIMData {resp.status_code} on {path}")
        resp.raise_for_status()
        return resp.json() if resp.content else None

    # ── Navigation ─────────────────────────────────────────────────────
    def list_clouds(self) -> list[dict]:
        return self._get("/cloud")

    def list_projects(self, cloud_id) -> list[dict]:
        return self._get(f"/cloud/{cloud_id}/project")

    def list_models(self, cloud_id, project_id) -> list[dict]:
        return self._get(f"/cloud/{cloud_id}/project/{project_id}/model")

    # ── GED (documents du projet) ──────────────────────────────────────
    def list_documents(self) -> list[dict]:
        return self._get(self._project_path("/document"))

    def get_document(self, document_id) -> dict:
        return self._get(self._project_path(f"/document/{document_id}"))

    def download_document(self, document_id, max_bytes: int) -> tuple[str, bytes]:
        doc = self.get_document(document_id)
        url = doc.get("file")
        if not url:
            raise ValueError(f"Le document {document_id} n'a pas de fichier téléchargeable.")
        # URL signée du stockage objet : pas d'en-tête Authorization à transmettre.
        import requests

        with requests.get(url, timeout=self.timeout, stream=True) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Document trop volumineux.")
                chunks.append(chunk)
        return doc.get("file_name") or doc.get("name") or f"document_{document_id}", b"".join(chunks)

    def upload_document(self, path: Path, *, name: str | None = None, parent_id=None) -> dict:
        data: dict[str, Any] = {"name": name or path.name}
        if parent_id:
            data["parent_id"] = parent_id
        with path.open("rb") as fh:
            resp = self.session.post(
                self._url(self._project_path("/document")),
                data=data,
                files={"file": (path.name, fh)},
                timeout=max(self.timeout, 180),
            )
        if resp.status_code in (401, 403):
            raise BIMDataAuthError(f"BIMData {resp.status_code} on document upload")
        resp.raise_for_status()
        return resp.json()

    # ── Éléments : lecture détaillée ───────────────────────────────────
    def get_element(self, uuid: str) -> dict:
        return self._get(self._model_path(f"/element/{uuid}"))

    def get_element_propertysets(self, uuid: str) -> list[dict]:
        return self._get(self._model_path(f"/element/{uuid}/propertyset"))

    # ── Éléments : écritures ciblées ───────────────────────────────────
    def update_element_property(self, uuid: str, pset_id, property_id, value) -> Any:
        return self._patch(
            self._model_path(f"/element/{uuid}/propertyset/{pset_id}/property/{property_id}"),
            {"value": value},
        )

    def create_element_property(self, uuid: str, pset_id, name: str, value, value_type: str) -> Any:
        return self._post(
            self._model_path(f"/element/{uuid}/propertyset/{pset_id}/property"),
            {
                "definition": {
                    "name": name,
                    "value_type": value_type,
                    "type": "IfcPropertySingleValue",
                },
                "value": value,
            },
        )


def make_client(target: BIMDataTarget, identity: Identity) -> BIMDataClient:
    s = get_settings()
    return BIMDataClient(
        base_url=s.api_url,
        cloud_id=target.cloud_id,
        project_id=target.project_id,
        model_id=target.model_id,
        timeout=90,
        **identity.client_credentials(s),
    )


def make_navigation_client(identity: Identity) -> BIMDataClient:
    s = get_settings()
    return BIMDataClient(base_url=s.api_url, timeout=60, **identity.client_credentials(s))


# ── Snapshot : cache disque (bimdata-read) + cache mémoire court ───────────

_MEM_TTL_S = 15 * 60
_MEM_MAX = 6
_mem: OrderedDict[str, tuple[float, ModelSnapshot]] = OrderedDict()
_dirty: set[str] = set()
_lock = threading.Lock()


def mark_dirty(target: BIMDataTarget) -> None:
    """Après une écriture : le prochain chargement relit l'API (pas de cache).

    Une écriture de propriété ne change pas forcément ``modified_date`` du
    modèle, qui est la clé du cache disque : sans ce marqueur, un contrôle
    relancé juste après une correction relirait l'état d'avant.
    """
    with _lock:
        _dirty.add(target.key)
        _mem.pop(target.key, None)


def load_snapshot(
    client: BIMDataClient, target: BIMDataTarget, *, refresh: bool = False
) -> tuple[ModelSnapshot, str]:
    """Charge le snapshot du modèle. Retourne ``(snapshot, source)``.

    L'accès est **toujours** revérifié avec les identifiants de l'appelant
    (``get_model``) avant de servir une copie en cache : le cache est partagé
    entre utilisateurs, les droits ne le sont pas.
    """
    model = client.get_model()  # preuve d'accès + modified_date
    stamp = str(model.get("modified_date") or model.get("updated_at") or "")
    key = target.key
    with _lock:
        dirty = key in _dirty
        hit = _mem.get(key)
        if hit and not refresh and not dirty and time.time() - hit[0] < _MEM_TTL_S:
            snap = hit[1]
            if str((snap.model or {}).get("modified_date") or (snap.model or {}).get("updated_at") or "") == stamp:
                _mem.move_to_end(key)
                return snap, "memory"
    s = get_settings()
    s.cache_dir.mkdir(parents=True, exist_ok=True)
    snap, from_disk = cached_extract_snapshot(
        client, cache_dir=s.cache_dir, use_cache=not (refresh or dirty)
    )
    with _lock:
        _dirty.discard(key)
        _mem[key] = (time.time(), snap)
        _mem.move_to_end(key)
        while len(_mem) > _MEM_MAX:
            _mem.popitem(last=False)
    return snap, ("disk_cache" if from_disk else "api")
