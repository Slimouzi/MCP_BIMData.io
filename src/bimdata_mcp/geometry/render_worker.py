"""Processus de rendu des images BCF : ``python -m bimdata_mcp.geometry.render_worker <job.json>``.

Lancé à part, comme le calcul de l'index : un plantage ou une consommation de mémoire excessive n'emporte pas le
serveur. Les images sont réparties sur plusieurs processus (``MCP_RENDER_WORKERS``, 4 au plus par défaut), chacun
chargeant l'index une fois ; l'avancement est écrit dans ``<job>.progress`` (nombre d'images faites), le résultat dans
``<job>.out.json``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_IDX = None


def _init(root: str) -> None:
    global _IDX
    from .index import GeometryIndex

    _IDX = GeometryIndex.load(Path(root))


def _render_one(v: dict) -> dict:
    from .render import render_viewpoint

    t0 = time.time()
    try:
        r = render_viewpoint(_IDX, v["viewpoint"], v["roles"], Path(v["out"]), region=v["region"],
                             lines=v.get("lines"), caption=v.get("caption", ""), offset=tuple(v.get("offset", (0, 0, 0))))
        r["duree_s"] = round(time.time() - t0, 3)
        return r
    except Exception as exc:  # noqa: BLE001 — une image en échec n'arrête pas les suivantes
        return {"path": v["out"], "error": str(exc)[:300], "duree_s": round(time.time() - t0, 3)}


def workers() -> int:
    raw = os.getenv("MCP_RENDER_WORKERS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return max(1, min(4, os.cpu_count() or 1))


def run_job(job: dict, progress=None, *, parallel: bool = False) -> list[dict]:
    """Rend toutes les vues ; ``progress(done, total)`` après chaque image ; ``parallel`` : plusieurs processus."""
    views = job["views"]
    total = len(views)
    if not parallel or workers() == 1 or total < 4:
        _init(job["root"])
        out = []
        for k, v in enumerate(views, start=1):
            out.append(_render_one(v))
            if progress:
                progress(k, total)
        return out
    from concurrent.futures import ProcessPoolExecutor

    out: list[dict | None] = [None] * total
    with ProcessPoolExecutor(max_workers=min(workers(), total), initializer=_init, initargs=(job["root"],)) as ex:
        futs = {ex.submit(_render_one, v): i for i, v in enumerate(views)}
        from concurrent.futures import as_completed

        for done, f in enumerate(as_completed(futs), start=1):
            out[futs[f]] = f.result()
            if progress:
                progress(done, total)
    return out  # type: ignore[return-value]


def main(argv: list[str]) -> int:
    job_path = Path(argv[1])
    prog = job_path.with_suffix(".progress")

    def progress(done: int, total: int) -> None:
        prog.write_text(f"{done}/{total}")

    res = run_job(json.loads(job_path.read_text()), progress, parallel=True)
    job_path.with_suffix(".out.json").write_text(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
