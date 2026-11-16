"""Mode oauth : câblage de l'OIDCProxy vers BIMData Connect (découverte OIDC simulée)."""

from __future__ import annotations


def test_oauth_provider_exposes_metadata(monkeypatch, tmp_path):
    from fastmcp.server.auth import oidc_proxy

    realm = "https://iam.bimdata.io/auth/realms/bimdata"
    conf = oidc_proxy.OIDCConfiguration(
        strict=False, issuer=realm,
        authorization_endpoint=f"{realm}/protocol/openid-connect/auth",
        token_endpoint=f"{realm}/protocol/openid-connect/token",
        jwks_uri=f"{realm}/protocol/openid-connect/certs",
        response_types_supported=["code"], subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
    )
    monkeypatch.setattr(oidc_proxy.OIDCConfiguration, "get_oidc_configuration",
                        classmethod(lambda cls, *a, **k: conf))
    for k, v in {"MCP_AUTH_MODE": "oauth", "BIMDATA_OAUTH_CLIENT_ID": "bimdata-mcp",
                 "BIMDATA_OAUTH_CLIENT_SECRET": "s3cret", "MCP_PUBLIC_URL": "https://mcp.example.com",
                 "MCP_JWT_SIGNING_KEY": "k" * 40, "MCP_DATA_DIR": str(tmp_path), "FASTMCP_HOME": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    from bimdata_mcp import config
    from bimdata_mcp.auth import build_auth_provider

    config.get_settings.cache_clear()
    try:
        provider = build_auth_provider()
        from fastmcp import FastMCP
        from starlette.testclient import TestClient

        app = FastMCP("t", auth=provider).http_app(path="/mcp")
        with TestClient(app, base_url="https://mcp.example.com") as c:
            meta = c.get("/.well-known/oauth-authorization-server").json()
            assert meta["authorization_endpoint"] == "https://mcp.example.com/authorize"
            assert meta["registration_endpoint"] == "https://mcp.example.com/register"  # DCR pour Claude
            prm = c.get("/.well-known/oauth-protected-resource/mcp")
            assert prm.status_code == 200
            assert c.post("/mcp", json={}).status_code == 401
    finally:
        config.get_settings.cache_clear()
