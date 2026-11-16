"""Configuration du serveur, lue **une seule fois** depuis l'environnement.

Toutes les variables sont préfixées ``BIMDATA_`` (accès API) ou ``MCP_``
(serveur). Voir ``.env.example`` pour la liste commentée.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "oui"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [p.strip() for p in (os.getenv(name) or "").split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    # ── API BIMData ──────────────────────────────────────────────────────
    api_url: str = "https://api.bimdata.io"
    platform_url: str = "https://platform.bimdata.io"
    iam_realm_url: str = "https://iam.bimdata.io/auth/realms/bimdata"
    # Identifiants *serveur* (mode token / none) : utilisés quand l'utilisateur
    # n'apporte pas son propre jeton BIMData (OAuth).
    api_key: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    # ── Serveur MCP ──────────────────────────────────────────────────────
    #: ``oauth`` : chaque utilisateur se connecte avec son compte BIMData
    #: (BIMData Connect) — recommandé pour un connecteur Claude.
    #: ``token`` : jetons statiques ``MCP_API_TOKENS`` (Bearer) + identifiants
    #: BIMData serveur. ``none`` : aucun contrôle (dev local uniquement).
    auth_mode: str = "none"
    api_tokens: tuple[str, ...] = ()
    public_url: str = "http://127.0.0.1:8000"
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_scopes: tuple[str, ...] = ("openid",)
    jwt_signing_key: str | None = None
    #: Clé HMAC des liens de téléchargement / dépôt de fichiers.
    file_secret: str = ""

    data_dir: Path = field(default_factory=lambda: Path("./data"))
    file_link_ttl_s: int = 24 * 3600
    upload_link_ttl_s: int = 2 * 3600
    max_upload_mb: int = 50
    allow_writes: bool = True
    max_plan_items: int = 20000
    environment: str = "development"

    @property
    def token_url(self) -> str:
        return f"{self.iam_realm_url}/protocol/openid-connect/token"

    @property
    def oidc_config_url(self) -> str:
        return f"{self.iam_realm_url}/.well-known/openid-configuration"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("MCP_DATA_DIR", "./data")).expanduser().resolve()
    secret = os.getenv("MCP_FILE_SECRET") or os.getenv("MCP_JWT_SIGNING_KEY") or ""
    if not secret:
        # Secret éphémère : les liens émis ne survivent pas à un redémarrage.
        # Acceptable en dev ; refusé en production (cf. __main__).
        import secrets

        secret = secrets.token_urlsafe(32)
    return Settings(
        api_url=os.getenv("BIMDATA_API_URL", "https://api.bimdata.io").rstrip("/"),
        platform_url=os.getenv("BIMDATA_PLATFORM_URL", "https://platform.bimdata.io").rstrip("/"),
        iam_realm_url=os.getenv(
            "BIMDATA_IAM_REALM_URL", "https://iam.bimdata.io/auth/realms/bimdata"
        ).rstrip("/"),
        api_key=os.getenv("BIMDATA_API_KEY") or None,
        client_id=os.getenv("BIMDATA_CLIENT_ID") or None,
        client_secret=os.getenv("BIMDATA_CLIENT_SECRET") or None,
        auth_mode=(os.getenv("MCP_AUTH_MODE") or "none").strip().lower(),
        api_tokens=tuple(_list("MCP_API_TOKENS")),
        public_url=(os.getenv("MCP_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/"),
        oauth_client_id=os.getenv("BIMDATA_OAUTH_CLIENT_ID") or None,
        oauth_client_secret=os.getenv("BIMDATA_OAUTH_CLIENT_SECRET") or None,
        oauth_scopes=tuple(_list("BIMDATA_OAUTH_SCOPES") or ["openid"]),
        jwt_signing_key=os.getenv("MCP_JWT_SIGNING_KEY") or None,
        file_secret=secret,
        data_dir=data_dir,
        file_link_ttl_s=_int("MCP_FILE_LINK_TTL", 24 * 3600),
        upload_link_ttl_s=_int("MCP_UPLOAD_LINK_TTL", 2 * 3600),
        max_upload_mb=_int("MCP_MAX_UPLOAD_MB", 50),
        allow_writes=_bool("MCP_ALLOW_WRITES", True),
        max_plan_items=_int("MCP_MAX_PLAN_ITEMS", 20000),
        environment=os.getenv("MCP_ENV", "development"),
    )
