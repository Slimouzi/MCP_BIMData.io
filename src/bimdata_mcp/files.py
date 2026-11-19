"""Fichiers : sorties téléchargeables (liens signés), dépôts, résolution d'entrées.

Un serveur distant ne partage pas de disque avec l'utilisateur. Les échanges
passent donc par :

- **sorties** : écrites sous ``users/<u>/files/``, exposées par un lien HTTPS
  signé (HMAC, expirant) ``/files/<owner>/<file_id>/<nom>`` ; publication
  optionnelle dans la GED du projet BIMData ;
- **entrées** (cahier des charges, fichier corrigé) : au choix
  ``file_id`` (fichier déjà sur le serveur : sortie précédente ou dépôt via le
  lien ``create_upload_link``), ``bimdata_document_id`` (document de la GED du
  projet), ``file_url`` (HTTPS public) ou ``file_base64``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import socket
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import get_settings
from .state import UserState, new_id, now_iso, slug

ALLOWED_INPUT_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv", ".json", ".ifc", ".txt", ".bcfzip", ".bcf"}  # BCF : exports de clash


def _sign(*parts: str) -> str:
    key = get_settings().file_secret.encode()
    return hmac.new(key, "|".join(parts).encode(), hashlib.sha256).hexdigest()[:32]


def _safe_name(name: str) -> str:
    p = Path(name or "fichier")
    stem = slug(p.stem, "fichier")
    suffix = "".join(c for c in p.suffix.lower() if c.isalnum() or c == ".")[:8]
    return f"{stem}{suffix}"


def files_dir(state: UserState) -> Path:
    d = state.root / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: Livrables copiés dans le dossier des rapports (les images BCF et fichiers techniques n'y vont pas).
REPORT_KINDS = {"check-report", "audit-report", "export", "cobie", "template", "report"}


def http_links_enabled() -> bool:
    """Liens HTTP de téléchargement actifs seulement quand le serveur MCP sert le web (transport HTTP). En stdio
    (Claude Desktop, Claude Code), aucun serveur web : les liens pointent vers le fichier local."""
    import os

    return os.getenv("MCP_TRANSPORT", "http").strip().lower() != "stdio"


def reports_dir() -> Path | None:
    """Dossier lisible où sont copiés les rapports : MCP_REPORTS_DIR, sinon ~/Documents/BIMData MCP/Rapports pour un
    serveur local (sans authentification, hors production) ; aucun sur un serveur partagé."""
    import os

    raw = os.getenv("MCP_REPORTS_DIR", "").strip()
    if raw.lower() in ("0", "non", "none", "off"):
        return None
    if raw:
        return Path(raw).expanduser()
    s = get_settings()
    if s.auth_mode == "none" and not s.is_production:
        return Path.home() / "Documents" / "BIMData MCP" / "Rapports"
    return None


def _publish_local(path: Path) -> Path | None:
    d = reports_dir()
    if d is None:
        return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        dest = d / path.name
        k = 2
        while dest.exists() and dest.read_bytes() != path.read_bytes():
            dest = d / f"{path.stem}_{k}{path.suffix}"
            k += 1
        import shutil

        shutil.copyfile(path, dest)
        return dest.resolve()
    except OSError:
        return None


def register_file(state: UserState, path: Path, *, kind: str, label: str = "") -> dict:
    """Enregistre un fichier déjà écrit dans ``files_dir`` et renvoie ses métadonnées (avec ``chemin_local`` : copie
    lisible du rapport dans le dossier des rapports, quand le serveur tourne sur le poste)."""
    file_id = path.parent.name
    meta = {
        "file_id": file_id,
        "name": path.name,
        "kind": kind,
        "label": label,
        "size": path.stat().st_size,
        "created_at": now_iso(),
    }
    local = _publish_local(path) if kind in REPORT_KINDS or path.suffix.lower() in (".pdf", ".xlsx") else None
    if local:
        meta["chemin_local"] = str(local)
    (path.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    meta["download_url"] = download_url(state, file_id, path.name)
    return meta


def new_output_path(state: UserState, filename: str) -> Path:
    fid = new_id("f")
    d = files_dir(state) / fid
    d.mkdir(parents=True, exist_ok=True)
    return d / _safe_name(filename)


def download_url(state: UserState, file_id: str, name: str) -> str:
    """Lien du fichier : HTTP signé si le serveur sert le web, sinon ``file://`` (copie dans le dossier des rapports,
    ou fichier du serveur) — jamais un lien vers un serveur web qui n'existe pas."""
    if not http_links_enabled():
        local = files_dir(state) / file_id / name
        meta = local.parent / "meta.json"
        try:
            copy = json.loads(meta.read_text()).get("chemin_local") if meta.exists() else None
        except Exception:  # noqa: BLE001
            copy = None
        return Path(copy or local).resolve().as_uri()
    s = get_settings()
    exp = str(int(time.time()) + s.file_link_ttl_s)
    owner = state.identity.storage_key
    sig = _sign("dl", owner, file_id, exp)
    return f"{s.public_url}/files/{owner}/{file_id}/{quote(name)}?exp={exp}&sig={sig}"


def verify_download(owner: str, file_id: str, exp: str, sig: str) -> Path | None:
    try:
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(_sign("dl", owner, file_id, exp), sig or ""):
        return None
    d = get_settings().users_dir / slug(owner) / "files" / slug(file_id)
    meta = d / "meta.json"
    if not meta.exists():
        return None
    name = json.loads(meta.read_text()).get("name")
    path = d / name
    return path if path.exists() else None


def list_files(state: UserState) -> list[dict]:
    out = []
    for meta in sorted(files_dir(state).glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = json.loads(meta.read_text())
        except Exception:  # noqa: BLE001
            continue
        m["download_url"] = download_url(state, m["file_id"], m["name"])
        out.append(m)
    return out


def get_file(state: UserState, file_id: str) -> Path:
    d = files_dir(state) / slug(file_id)
    meta = d / "meta.json"
    if not meta.exists():
        raise FileNotFoundError(f"Fichier {file_id!r} introuvable (voir list_files).")
    return d / json.loads(meta.read_text())["name"]


# ── Liens de dépôt ──────────────────────────────────────────────────────


def upload_link(state: UserState, purpose: str = "") -> dict:
    s = get_settings()
    exp = str(int(time.time()) + s.upload_link_ttl_s)
    owner = state.identity.storage_key
    sig = _sign("up", owner, exp)
    url = f"{s.public_url}/upload/{owner}/{exp}/{sig}"
    if purpose:
        url += f"?purpose={quote(purpose)}"
    return {"upload_url": url, "expires_in_s": s.upload_link_ttl_s}


def verify_upload(owner: str, exp: str, sig: str) -> bool:
    try:
        if int(exp) < time.time():
            return False
    except ValueError:
        return False
    return hmac.compare_digest(_sign("up", owner, exp), sig or "")


def store_upload(owner: str, filename: str, content: bytes) -> dict:
    s = get_settings()
    if len(content) > s.max_upload_mb * 1024 * 1024:
        raise ValueError(f"Fichier trop volumineux (max {s.max_upload_mb} Mo).")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_INPUT_SUFFIXES:
        raise ValueError(f"Extension non acceptée : {suffix or '(aucune)'}.")
    fid = new_id("up")
    d = s.users_dir / slug(owner) / "files" / fid
    d.mkdir(parents=True, exist_ok=True)
    path = d / _safe_name(filename)
    path.write_bytes(content)
    meta = {"file_id": fid, "name": path.name, "kind": "upload", "label": filename,
            "size": len(content), "created_at": now_iso()}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return meta


# ── Résolution d'une entrée ─────────────────────────────────────────────


def _assert_public_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("file_url doit être une URL https.")
    for info in socket.getaddrinfo(parsed.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("file_url pointe vers une adresse non publique : refusé.")


def resolve_input(
    state: UserState,
    *,
    file_id: str | None = None,
    bimdata_document_id: int | str | None = None,
    file_url: str | None = None,
    file_base64: str | None = None,
    file_name: str | None = None,
    client=None,
) -> Path:
    """Matérialise une entrée sur disque et renvoie son chemin."""
    given = [x for x in (file_id, bimdata_document_id, file_url, file_base64) if x not in (None, "")]
    if len(given) != 1:
        raise ValueError(
            "Fournir exactement une source : file_id, bimdata_document_id, file_url ou file_base64. "
            "Pour un fichier local, utiliser create_upload_link."
        )
    s = get_settings()
    max_bytes = s.max_upload_mb * 1024 * 1024
    if file_id:
        return get_file(state, file_id)
    if bimdata_document_id not in (None, ""):
        if client is None:
            raise ValueError("Maquette active requise pour lire un document BIMData (set_active_model).")
        name, content = client.download_document(bimdata_document_id, max_bytes)
        meta = store_upload(state.identity.storage_key, name, content)
        return get_file(state, meta["file_id"])
    if file_url:
        import requests

        _assert_public_https(file_url)
        with requests.get(file_url, timeout=60, stream=True, allow_redirects=False) as resp:
            resp.raise_for_status()
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("Fichier distant trop volumineux.")
                chunks.append(chunk)
        name = file_name or Path(urlparse(file_url).path).name or "fichier.xlsx"
        meta = store_upload(state.identity.storage_key, name, b"".join(chunks))
        return get_file(state, meta["file_id"])
    content = base64.b64decode(file_base64 or "", validate=False)
    meta = store_upload(state.identity.storage_key, file_name or "fichier.xlsx", content)
    return get_file(state, meta["file_id"])


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
