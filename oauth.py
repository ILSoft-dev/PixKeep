"""
oauth.py
v3.0 - per-user Yandex OAuth (authorization-code flow)

Each user authorizes their own Yandex.Disk on Yandex's own consent page.
The bot only receives an authorization code -> tokens; it never sees the
user's Yandex password.
"""
import secrets
from urllib.parse import urlencode

import aiohttp

from config import config

AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"

# short-lived map: state nonce -> telegram_id (survives only until callback)
pending_states: dict[str, int] = {}


def build_auth_url(telegram_id: int) -> str:
    state = secrets.token_urlsafe(24)
    pending_states[state] = telegram_id
    params = {
        "response_type": "code",
        "client_id": config.YANDEX_CLIENT_ID,
        "redirect_uri": config.OAUTH_REDIRECT_URI,
        "scope": config.YANDEX_SCOPE,
        "state": state,
        "force_confirm": "yes",  # always show consent -> reliably get refresh token
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
        "client_id": config.YANDEX_CLIENT_ID,
        "client_secret": config.YANDEX_CLIENT_SECRET,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token exchange failed: {payload}")

    return telegram_id, payload["access_token"], payload.get("refresh_token", "")


async def refresh_access_token(refresh_token: str) -> dict:
    """Return {access_token, refresh_token} using a stored refresh token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": config.YANDEX_CLIENT_ID,
        "client_secret": config.YANDEX_CLIENT_SECRET,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token refresh failed: {payload}")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", refresh_token),
    }
