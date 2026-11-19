"""Comparaison d'un contrôle de conflits du MCP avec les résultats d'un autre outil (Solibri, BCF ou Excel).

Méthode (spécification) : même fichier IFC, mêmes réglages (General Intersection, tolérances), export des
résultats avec les GlobalId des deux éléments de chaque conflit, puis comparaison **paire par paire** :
conflits trouvés par les deux outils, par le MCP seulement, par l'autre outil seulement.

Lecture de l'export sans hypothèse sur ses colonnes : on y cherche les GlobalId IFC (22 caractères de
l'alphabet IFC). BCF : une entrée par topic (GlobalId des composants de ses points de vue) ; Excel / CSV :
une entrée par ligne portant au moins deux GlobalId.
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path

GUID_RE = re.compile(r"(?<![0-9A-Za-z_$])[0-3][0-9A-Za-z_$]{21}(?![0-9A-Za-z_$])")
_TITLE_RE = re.compile(r"<Title>(.*?)</Title>", re.DOTALL)


def read_external(path: Path, known: set[str] | None = None) -> tuple[list[dict], dict]:
    """Entrées ``{id, guids, label}`` de l'export ; ``known`` filtre les GlobalId absents de la maquette."""
    suffix = path.suffix.lower()
    entries: list[dict] = []
    unknown: set[str] = set()

    def keep(gs: list[str]) -> list[str]:
        out = []
        for g in gs:
            if known is not None and g not in known:
                unknown.add(g)
                continue
            if g not in out:
                out.append(g)
        return out

    if suffix not in (".xlsx", ".xlsm") and zipfile.is_zipfile(path):  # un .xlsx est aussi une archive zip
        with zipfile.ZipFile(path) as z:
            topics: dict[str, dict] = {}
            for name in z.namelist():
                parts = name.split("/")
                if len(parts) < 2 or not parts[-1]:
                    continue
                folder, leaf = parts[-2], parts[-1].lower()
                if leaf.endswith((".bcfv", ".bcf")):
                    text = z.read(name).decode("utf-8", "replace")
                    t = topics.setdefault(folder, {"guids": [], "label": ""})
                    if leaf.endswith(".bcfv"):
                        t["guids"] += re.findall(r'IfcGuid="([^"]+)"', text)
                    elif leaf == "markup.bcf":
                        m = _TITLE_RE.search(text)
                        t["label"] = (m.group(1).strip() if m else "")[:200]
            for folder, t in topics.items():
                gs = keep(t["guids"])
                if len(gs) >= 2:
                    entries.append({"id": folder, "guids": gs, "label": t["label"]})
        fmt = "BCF"
    elif suffix in (".xlsx", ".xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
                gs = keep(GUID_RE.findall(" ".join(str(c) for c in row if c is not None)))
                if len(gs) >= 2:
                    label = " | ".join(str(c) for c in row if c is not None and not GUID_RE.fullmatch(str(c)))[:200]
                    entries.append({"id": f"{ws.title}!{n}", "guids": gs, "label": label})
        fmt = "Excel"
    elif suffix in (".csv", ".txt"):
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t") if raw.strip() else csv.excel
        for n, row in enumerate(csv.reader(raw.splitlines(), dialect), start=1):
            gs = keep(GUID_RE.findall(" ".join(row)))
            if len(gs) >= 2:
                entries.append({"id": f"ligne {n}", "guids": gs, "label": " | ".join(row)[:200]})
        fmt = "CSV"
    else:
        raise ValueError(f"Format non reconnu ({path.name}) : export BCF (.bcfzip), Excel (.xlsx) ou CSV attendu.")
    if not entries:
        raise ValueError(f"Aucun conflit lisible dans {path.name} : chaque conflit doit porter les GlobalId de ses deux "
                         "éléments (export Solibri avec GlobalId, ou BCF).")
    return entries, {"format": fmt, "guids_inconnus": len(unknown)}


def compare(mcp_pairs: list[dict], entries: list[dict]) -> dict:
    """``mcp_pairs`` : ``[{issue_id, a, b}]``. Une entrée externe correspond à une paire MCP si elle contient ses deux
    GlobalId ; chaque entrée et chaque paire ne servent qu'une fois."""
    by_guid: dict[str, list[int]] = {}
    for k, e in enumerate(entries):
        for g in e["guids"]:
            by_guid.setdefault(g, []).append(k)
    used: set[int] = set()
    both, only_mcp = [], []
    for p in mcp_pairs:
        cands = [k for k in by_guid.get(p["a"], []) if k not in used and p["b"] in entries[k]["guids"]]
        if cands:
            k = min(cands, key=lambda k: len(entries[k]["guids"]))  # l'entrée la plus précise
            used.add(k)
            both.append({**p, "externe": entries[k]["id"], "libelle_externe": entries[k]["label"]})
        else:
            only_mcp.append(p)
    only_ext = [e for k, e in enumerate(entries) if k not in used]
    n_mcp, n_ext = len(mcp_pairs), len(entries)
    return {
        "mcp": n_mcp, "externe": n_ext, "communs": len(both), "mcp_seulement": len(only_mcp),
        "externe_seulement": len(only_ext),
        "ecart_nombre_pct": round(100 * abs(n_mcp - n_ext) / n_ext, 1) if n_ext else None,
        "ecart_paires_pct": round(100 * (len(only_mcp) + len(only_ext)) / n_ext, 1) if n_ext else None,
        "details": {"communs": both, "mcp_seulement": only_mcp, "externe_seulement": only_ext},
    }
