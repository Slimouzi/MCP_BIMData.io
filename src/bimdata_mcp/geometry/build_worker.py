"""Processus de calcul de l'index géométrique : ``python -m bimdata_mcp.geometry.build_worker IFC RACINE CLÉ BIBLIO``.

Lancé par :func:`bimdata_mcp.geometry.index.build_isolated` : un plantage natif de la triangulation reste confiné à
ce processus, et sa mémoire est rendue au système à la fin du calcul.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    ifc_path, root, key, library = argv
    os.environ["MCP_GEOMETRY_LIBRARY"] = library
    from .index import build

    build(Path(ifc_path), Path(root), key=key)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
