"""Tâches de fond : opérations longues (préparation de centaines de topics BCF avec images, envoi dans BIMData) qui
dépasseraient le délai d'attente du client MCP. L'outil répond tout de suite avec un ``job_id`` ; ``job_status`` donne
l'avancement (fait / total, message) puis le résultat. État persistant (``users/<u>/jobs``) : il survit au client.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable

from .state import UserState, new_id, now_iso

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}


def _save(state: UserState, job: dict) -> None:
    with _lock:
        state.save("jobs", job["job_id"], job)


def start(state: UserState, *, kind: str, label: str, total: int | None, fn: Callable[[Callable], dict],
          estimate_s: float | None = None) -> dict:
    """Lance ``fn(progress)`` dans un fil de fond ; ``progress(done, total=None, message=None)`` met l'état à jour."""
    job = {"job_id": new_id("job"), "kind": kind, "label": label, "status": "en_cours", "done": 0, "total": total,
           "message": "démarrage", "started_at": now_iso(), "_t0": time.time(), "estimation_s": estimate_s}
    _save(state, job)
    last = [0.0]

    def progress(done: int, total: int | None = None, message: str | None = None) -> None:
        job["done"] = done
        if total is not None:
            job["total"] = total
        if message:
            job["message"] = message
        if time.time() - last[0] > 0.5 or (job["total"] and done >= job["total"]):  # écritures limitées
            last[0] = time.time()
            _save(state, job)

    def run() -> None:
        try:
            result = fn(progress)
            job.update(status="termine", result=result, message="terminé", finished_at=now_iso())
        except Exception as exc:  # noqa: BLE001 — l'échec est rapporté par job_status
            job.update(status="echec", error=str(exc)[:1000], message="échec", finished_at=now_iso(),
                       trace=traceback.format_exc()[-2000:])
        _save(state, job)

    t = threading.Thread(target=run, name=job["job_id"], daemon=True)
    _threads[job["job_id"]] = t
    t.start()
    return public(job)


def public(job: dict) -> dict:
    out = {k: v for k, v in job.items() if not k.startswith("_") and k != "trace"}
    t0 = job.get("_t0")
    if t0 and job.get("status") == "en_cours":
        el = time.time() - t0
        out["ecoule_s"] = round(el)
        if job.get("total") and job.get("done"):
            out["restant_estime_s"] = round(el / job["done"] * (job["total"] - job["done"]))
    return out


def status(state: UserState, job_id: str) -> dict:
    job = state.load("jobs", job_id)
    if not job:
        raise ValueError(f"Tâche {job_id!r} introuvable.")
    if job.get("status") == "en_cours" and job_id not in _threads:
        job.update(status="interrompue", message="interrompue (redémarrage du serveur MCP) : relancer l'opération")
        _save(state, job)
    return public(job)
