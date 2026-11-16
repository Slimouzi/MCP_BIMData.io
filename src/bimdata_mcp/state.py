"""État serveur : persistant **par utilisateur** + cible active **par session**.

- Les objets nommés (sélections, cahiers des charges, contrôles, plans
  d'écriture, fichiers) sont persistés sur disque sous
  ``MCP_DATA_DIR/users/<hash utilisateur>/`` : ils survivent à la fin d'une
  conversation et se réutilisent d'une conversation à l'autre par leur nom/id.
- La **maquette active** est tenue par session MCP (deux conversations peuvent
  travailler sur deux maquettes), avec repli sur la dernière maquette utilisée
  par l'utilisateur.
"""

from __future__ import annotations

import gzip
import json
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import Identity
from .config import get_settings
from .target import BIMDataTarget

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_session_targets: dict[tuple[str, str], tuple[float, BIMDataTarget]] = {}
_SESSION_TTL_S = 12 * 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"


def slug(text: str, default: str = "item") -> str:
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")
    return (s or default)[:80]


def _user_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


class UserState:
    KINDS = ("selections", "requirements", "checks", "plans", "files", "journal")

    def __init__(self, identity: Identity):
        self.identity = identity
        self.root = get_settings().users_dir / identity.storage_key
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = _user_lock(identity.storage_key)

    # ── stockage JSON générique ────────────────────────────────────────
    def _path(self, kind: str, obj_id: str, gz: bool = False) -> Path:
        assert kind in self.KINDS
        d = self.root / kind
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{slug(obj_id)}.json{'.gz' if gz else ''}"

    def save(self, kind: str, obj_id: str, data: dict, *, gz: bool = False) -> Path:
        path = self._path(kind, obj_id, gz)
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self.lock:
            tmp.write_bytes(gzip.compress(raw) if gz else raw)
            tmp.replace(path)
        return path

    def load(self, kind: str, obj_id: str) -> dict | None:
        for gz in (False, True):
            path = self._path(kind, obj_id, gz)
            if path.exists():
                raw = path.read_bytes()
                return json.loads(gzip.decompress(raw) if gz else raw)
        return None

    def delete(self, kind: str, obj_id: str) -> bool:
        done = False
        for gz in (False, True):
            path = self._path(kind, obj_id, gz)
            if path.exists():
                path.unlink()
                done = True
        return done

    def list(self, kind: str) -> list[dict]:
        d = self.root / kind
        if not d.exists():
            return []
        out = []
        for path in sorted(d.glob("*.json*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name.endswith(".tmp"):
                continue
            try:
                raw = path.read_bytes()
                data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
            except Exception:  # noqa: BLE001
                continue
            out.append(data)
        return out

    def journal(self, event: str, **payload: Any) -> None:
        """Journal append-only des écritures BIMData (traçabilité)."""
        path = self.root / "journal" / f"{datetime.now(timezone.utc):%Y%m}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": now_iso(), "event": event, **payload}, ensure_ascii=False, default=str)
        with self.lock, path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ── cible ──────────────────────────────────────────────────────────
    def set_last_target(self, target: BIMDataTarget, meta: dict | None = None) -> None:
        (self.root / "last_target.json").write_text(
            json.dumps({**target.to_dict(), **(meta or {}), "at": now_iso()}, ensure_ascii=False)
        )

    def last_target(self) -> BIMDataTarget | None:
        p = self.root / "last_target.json"
        if not p.exists():
            return None
        try:
            return BIMDataTarget.from_dict(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001
            return None


def set_session_target(identity: Identity, session_id: str, target: BIMDataTarget) -> None:
    now = time.time()
    _session_targets[(identity.user_id, session_id)] = (now, target)
    for key, (ts, _) in list(_session_targets.items()):
        if now - ts > _SESSION_TTL_S:
            _session_targets.pop(key, None)


def get_session_target(identity: Identity, session_id: str) -> BIMDataTarget | None:
    hit = _session_targets.get((identity.user_id, session_id))
    if hit:
        _session_targets[(identity.user_id, session_id)] = (time.time(), hit[1])
        return hit[1]
    return None
