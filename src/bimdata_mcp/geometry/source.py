"""Fichier IFC source de la maquette active (GED BIMData), avec cache disque.

- URL signée lue sur ``get_model()`` (``document.file``), valable une heure : jamais
  mise en cache, relue à chaque téléchargement ; pas d'en-tête ``Authorization``
  (stockage objet).
- Le stockage BIMData peut servir le fichier **compressé gzip** sans en-tête
  ``Content-Encoding`` : la signature gzip est détectée et le fichier décompressé.
- Écriture en flux dans un ``.part`` puis renommage atomique ; plafond de taille.
- Cache : ``cloud_project_model_modified-date.ifc`` ; une nouvelle version de la
  maquette change ``modified_date`` donc le nom.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shutil
from pathlib import Path

from ..config import get_settings

_CHUNK = 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"


def _max_bytes() -> int:
    try:
        mb = int(os.getenv("MCP_MAX_IFC_MB", "") or 800)
    except ValueError:
        mb = 800
    return mb * 1024 * 1024


def _safe(value) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("_") or "x"


def ifc_url(model: dict) -> str | None:
    doc = model.get("document")
    if isinstance(doc, dict) and doc.get("file"):
        return str(doc["file"])
    for key in ("file", "source_file", "ifc_file"):
        if isinstance(model.get(key), str) and model[key]:
            return model[key]
    return None


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, target: Path, timeout: int) -> int:
    import requests

    max_bytes = _max_bytes()
    tmp = target.with_name(target.name + ".part")
    written = 0
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(_CHUNK):
                    written += len(chunk)
                    if written > max_bytes:
                        raise ValueError(f"Fichier IFC trop volumineux (> {max_bytes // 2**20} Mo, MCP_MAX_IFC_MB).")
                    fh.write(chunk)
        with tmp.open("rb") as fh:
            gz = fh.read(2) == _GZIP_MAGIC
        if gz:
            raw = tmp.with_name(tmp.name + ".raw")
            with gzip.open(tmp, "rb") as src, raw.open("wb") as dst:
                shutil.copyfileobj(src, dst, _CHUNK)
            raw.replace(tmp)
        tmp.replace(target)
        return target.stat().st_size
    finally:
        tmp.unlink(missing_ok=True)
        tmp.with_name(tmp.name + ".raw").unlink(missing_ok=True)


def fetch_model_ifc(client, target, *, refresh: bool = False) -> tuple[Path, dict]:
    """Chemin local du fichier IFC de la maquette (téléchargé au besoin) + métadonnées."""
    model = client.get_model() or {}  # preuve d'accès avec les droits de l'appelant
    stamp = str(model.get("modified_date") or model.get("updated_at") or "nodate")
    d = get_settings().cache_dir / "ifc"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe(target.cloud_id)}_{_safe(target.project_id)}_{_safe(target.model_id)}_{_safe(stamp)}.ifc"
    from_cache = path.exists() and not refresh
    if not from_cache:
        url = ifc_url(model)
        if not url:
            raise ValueError("Fichier IFC introuvable sur la maquette (champ document.file absent) : "
                             "la maquette n'expose pas son fichier source.")
        _download(url, path, timeout=max(getattr(client, "timeout", 60), 300))
        for old in d.glob(f"{_safe(target.cloud_id)}_{_safe(target.project_id)}_{_safe(target.model_id)}_*.ifc"):
            if old != path:
                old.unlink(missing_ok=True)  # anciennes versions de la même maquette
    return path, {"file": model.get("name") or path.name, "size_mb": round(path.stat().st_size / 2**20, 1),
                  "from_cache": from_cache, "modified_date": stamp}
