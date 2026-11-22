"""Local password auth + generic OIDC authorization-code flow."""

from __future__ import annotations

import base64
import os
import secrets
import urllib.parse

import bcrypt
import httpx

from . import db

OIDC_ENABLED = os.getenv("OIDC_ENABLED", "false").lower() in ("1", "true", "yes")
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.getenv("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URL = os.getenv("OIDC_REDIRECT_URL", "")
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid profile email")
OIDC_USER_CLAIM = os.getenv("OIDC_USER_CLAIM", "preferred_username")
OIDC_BASE_URL = os.getenv("OIDC_BASE_URL", "").rstrip("/")
OIDC_SLUG = os.getenv("OIDC_SLUG", "dockio")


def _oidc_endpoint(env_name: str, default_path: str) -> str:
    explicit = os.getenv(env_name, "").strip()
    if explicit:
        return explicit
    if OIDC_BASE_URL:
        return f"{OIDC_BASE_URL}/application/o/{default_path}/"
    return ""


OIDC_AUTHORIZE_URL = _oidc_endpoint("OIDC_AUTHORIZE_URL", "authorize")
OIDC_TOKEN_URL = _oidc_endpoint("OIDC_TOKEN_URL", "token")
OIDC_USERINFO_URL = _oidc_endpoint("OIDC_USERINFO_URL", "userinfo")


# ---------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except (ValueError, TypeError):
        return False


def init_local_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "admin")
    encoded = os.getenv("ADMIN_PASSWORD_HASH_B64", "").strip()
    if encoded:
        # Base64 keeps bcrypt's "$" characters away from Compose's env interpolation.
        password_hash = base64.b64decode(encoded).decode()
    else:
        password_hash = os.getenv("ADMIN_PASSWORD_HASH", "").strip().strip("'\"")
    if not password_hash:
        raise RuntimeError("ADMIN_PASSWORD_HASH_B64 is not set in .env")
    db.upsert_local_user(username, password_hash, role="admin")


def check_local_login(username: str, password: str) -> bool:
    row = db.get_user(username)
    if not row or not row["is_local"] or not row["password_hash"]:
        return False
    return verify_password(password, row["password_hash"])


# ----------------------------------------------------------------------------- OIDC
def authorization_url() -> tuple[str, str]:
    """Return (url, state). The caller stores state in the session."""
    if not OIDC_ENABLED:
        raise RuntimeError("OIDC is disabled")
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": OIDC_CLIENT_ID,
        "response_type": "code",
        "scope": OIDC_SCOPES,
        "redirect_uri": OIDC_REDIRECT_URL,
        "state": state,
    }
    return f"{OIDC_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state


def exchange_code_and_get_user(code: str) -> str:
    """Exchange an authorization code, fetch userinfo, return the username."""
    with httpx.Client(timeout=15.0) as client:
        token_response = client.post(
            OIDC_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": OIDC_REDIRECT_URL,
                "client_id": OIDC_CLIENT_ID,
                "client_secret": OIDC_CLIENT_SECRET,
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        userinfo = client.get(
            OIDC_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo.raise_for_status()
        claims = userinfo.json()

    username = str(claims.get(OIDC_USER_CLAIM) or claims.get("sub") or "").strip()
    if not username:
        raise RuntimeError(f"OIDC claim '{OIDC_USER_CLAIM}' was missing from userinfo")
    return username


def oidc_public_config() -> dict:
    """Non-secret values used by the login template."""
    return {
        "enabled": OIDC_ENABLED,
        "authorize_ready": bool(OIDC_AUTHORIZE_URL and OIDC_CLIENT_ID),
    }
