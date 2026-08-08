"""
oauth.py
v4.1 - per-user Google OAuth (authorization-code flow, plain REST via aiohttp)

Changelog:
- v4.1: added get_user_email() — needed to label multiple connected Google
        accounts distinctly in /accounts.

Each user authorizes their own Google Drive on Google's own consent page.
The bot only receives an authorization code -> tokens; it never sees the
user's Google password.
"""
import secrets
from urllib.parse import urlencode

import aiohttp

from config import config

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# short-lived map: state nonce -> telegram_id (survives only until callback)
pending_states: dict[str, int] = {}


def build_auth_url(telegram_id: int) -> str:
    state = secrets.token_urlsafe(24)
    pending_states[state] = telegram_id
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": config.GOOGLE_SCOPE,
        "access_type": "offline",   # required to receive a refresh token
        "prompt": "consent",        # force refresh token even on re-auth
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(state: str, code: str) -> tuple[int, str, str]:
    """Return (telegram_id, access_token, refresh_token) for a completed consent."""
    telegram_id = pending_states.pop(state, None)
    if telegram_id is None:
        raise ValueError("Unknown or expired state")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token exchange failed: {payload}")

    if "refresh_token" not in payload:
        raise ValueError(
            "No refresh token returned by Google (usually means the user "
            "already authorized before without revoking access — ask them "
            "to revoke access at myaccount.google.com/permissions and retry)"
        )
    return telegram_id, payload["access_token"], payload["refresh_token"]


async def get_user_email(access_token: str) -> str:
    """Used to label multiple connected accounts distinctly in /accounts."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise ValueError(f"userinfo failed: {data}")
            return data.get("email", "unknown@account")


async def refresh_access_token(refresh_token: str) -> dict:
    """Return {access_token, refresh_token} using a stored refresh token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token refresh failed: {payload}")
    return {
        "access_token": payload["access_token"],
        # Google normally does NOT return a new refresh_token on refresh —
        # keep reusing the one we already have unless a new one is given.
        "refresh_token": payload.get("refresh_token", refresh_token),
    }
