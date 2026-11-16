"""Authentification du serveur et résolution des identifiants BIMData par appel.

Trois modes (``MCP_AUTH_MODE``) :

``oauth`` (recommandé pour un connecteur Claude)
    Le serveur se comporte comme un serveur d'autorisation OAuth 2.1 vis-à-vis
    de Claude et **délègue la connexion à BIMData Connect** (Keycloak) via
    l'``OIDCProxy`` de FastMCP. Chaque utilisateur se connecte avec son propre
    compte BIMData ; ses appels API partent avec **son** jeton, donc avec ses
    droits — le serveur ne voit jamais que ce que l'utilisateur voit.

``token``
    Jetons Bearer statiques (``MCP_API_TOKENS``), comparés en temps constant.
    Les appels BIMData utilisent les identifiants *serveur*
    (``BIMDATA_API_KEY`` ou ``BIMDATA_CLIENT_ID``/``SECRET``). Adapté à l'API
    Claude (MCP connector) ou à Claude Code.

``none``
    Aucun contrôle : développement local uniquement (refusé en production).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from .config import Settings, get_settings


class _ConstantTimeTokenVerifier(TokenVerifier):
    """Vérifie un Bearer contre une liste de jetons, en temps constant."""

    def __init__(self, tokens: tuple[str, ...]):
        super().__init__()
        self._tokens = [t.encode() for t in tokens if t]

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = (token or "").encode()
        for index, expected in enumerate(self._tokens):
            if hmac.compare_digest(candidate, expected):
                return AccessToken(
                    token=token,
                    client_id=f"token-{index + 1}",
                    scopes=[],
                    expires_at=None,
                    claims={"sub": f"token-{index + 1}"},
                )
        return None


def build_auth_provider(settings: Settings | None = None):
    """Construit le fournisseur d'auth FastMCP selon le mode configuré."""
    s = settings or get_settings()
    if s.auth_mode == "none":
        return None
    if s.auth_mode == "token":
        if not s.api_tokens:
            raise RuntimeError("MCP_AUTH_MODE=token exige MCP_API_TOKENS (liste séparée par des virgules).")
        return _ConstantTimeTokenVerifier(s.api_tokens)
    if s.auth_mode == "oauth":
        if not (s.oauth_client_id and s.oauth_client_secret):
            raise RuntimeError(
                "MCP_AUTH_MODE=oauth exige BIMDATA_OAUTH_CLIENT_ID et BIMDATA_OAUTH_CLIENT_SECRET "
                "(application créée sur BIMData Connect, redirect URI = "
                f"{s.public_url}/auth/callback)."
            )
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        kwargs = dict(
            config_url=s.oidc_config_url,
            client_id=s.oauth_client_id,
            client_secret=s.oauth_client_secret,
            base_url=s.public_url,
            redirect_path="/auth/callback",
            required_scopes=list(s.oauth_scopes) or None,
            # Claude.ai et Claude Desktop/Code utilisent ces callbacks.
            allowed_client_redirect_uris=[
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
                "http://localhost:*",
                "http://127.0.0.1:*",
            ],
        )
        if s.jwt_signing_key:
            # Clé stable : les sessions OAuth survivent aux redémarrages. Le
            # stockage des clients/jetons amont est chiffré par FastMCP sous
            # ``FASTMCP_HOME`` (à placer sur le volume persistant).
            kwargs["jwt_signing_key"] = s.jwt_signing_key
        return OIDCProxy(**kwargs)
    raise RuntimeError(f"MCP_AUTH_MODE inconnu : {s.auth_mode!r} (oauth | token | none).")


@dataclass(frozen=True)
class Identity:
    """Qui appelle, et avec quels identifiants BIMData."""

    user_id: str
    display: str
    access_token: str | None = None  # jeton BIMData de l'utilisateur (oauth)

    @property
    def storage_key(self) -> str:
        """Clé disque non réversible (le ``sub`` n'est jamais écrit en clair)."""
        return hashlib.sha256(self.user_id.encode()).hexdigest()[:24]

    def client_credentials(self, settings: Settings | None = None) -> dict:
        s = settings or get_settings()
        if self.access_token:
            return {"access_token": self.access_token}
        if s.api_key:
            return {"api_key": s.api_key}
        if s.client_id and s.client_secret:
            return {
                "client_id": s.client_id,
                "client_secret": s.client_secret,
                "iam_url": s.token_url,
            }
        raise PermissionError(
            "Aucun identifiant BIMData disponible : configurer BIMDATA_API_KEY ou "
            "BIMDATA_CLIENT_ID/BIMDATA_CLIENT_SECRET sur le serveur, ou utiliser MCP_AUTH_MODE=oauth."
        )


def current_identity() -> Identity:
    s = get_settings()
    token = None
    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001 — hors requête HTTP (stdio, tests)
        token = None
    if s.auth_mode == "oauth":
        if token is None:
            raise PermissionError("Non authentifié : reconnecter le connecteur BIMData.")
        claims = getattr(token, "claims", {}) or {}
        sub = claims.get("sub") or token.client_id
        label = claims.get("email") or claims.get("preferred_username") or sub
        return Identity(user_id=f"oidc:{sub}", display=str(label), access_token=token.token)
    if s.auth_mode == "token":
        if token is None:
            raise PermissionError("Jeton MCP manquant ou invalide.")
        return Identity(user_id=f"tok:{token.client_id}", display=token.client_id)
    return Identity(user_id="local", display="local")
