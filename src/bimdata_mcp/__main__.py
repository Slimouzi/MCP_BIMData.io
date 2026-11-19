"""``bimdata-mcp`` — point d'entrée (stdio en local, HTTP streamable sur serveur)."""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="bimdata-mcp", description="Serveur MCP BIMData")
    parser.add_argument("--transport", default="http", choices=("stdio", "http", "streamable-http", "sse"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp", help="Chemin de l'endpoint MCP (HTTP).")
    args = parser.parse_args()
    import os

    os.environ["MCP_TRANSPORT"] = args.transport  # liens de fichiers : HTTP seulement si le serveur sert le web
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import get_settings

    s = get_settings()
    network = args.transport != "stdio"
    problems = []
    if network and s.auth_mode == "none" and args.host not in ("127.0.0.1", "localhost"):
        problems.append("MCP_AUTH_MODE=none interdit hors localhost : utiliser oauth ou token.")
    if s.is_production:
        if s.auth_mode == "none":
            problems.append("MCP_ENV=production exige MCP_AUTH_MODE=oauth ou token.")
        if not s.public_url.startswith("https://"):
            problems.append("MCP_PUBLIC_URL doit être en https en production.")
        import os

        if not (os.getenv("MCP_FILE_SECRET") or os.getenv("MCP_JWT_SIGNING_KEY")):
            problems.append("Définir MCP_FILE_SECRET (liens de fichiers stables entre redémarrages).")
        if s.auth_mode == "oauth" and not s.jwt_signing_key:
            problems.append("Définir MCP_JWT_SIGNING_KEY (sessions OAuth stables entre redémarrages).")
    if s.auth_mode != "oauth" and not (s.api_key or (s.client_id and s.client_secret)):
        problems.append("Identifiants BIMData serveur manquants : BIMDATA_API_KEY ou BIMDATA_CLIENT_ID/SECRET.")
    if problems:
        for p in problems:
            print(f"bimdata-mcp — refus de démarrer : {p}", file=sys.stderr)
        return 2

    s.data_dir.mkdir(parents=True, exist_ok=True)
    from .server import mcp

    if network:
        print(f"bimdata-mcp {args.transport} sur http://{args.host}:{args.port}{args.path} (auth={s.auth_mode})",
              file=sys.stderr)
        mcp.run(transport=args.transport, host=args.host, port=args.port, path=args.path)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
