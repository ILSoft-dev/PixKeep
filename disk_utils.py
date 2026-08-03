"""
disk_utils.py
v3.1 - Yandex.Disk REST API helpers (async, aiohttp)

Changelog:
- v3.1: added get_disk_info() for /space command (total/used quota).
- v3.0: initial Yandex.Disk backend (ensure_folder, upload_file, publish).

Replaces the previous Google drive_utils.py.
API docs: https://yandex.ru/dev/disk-api/doc/ru/reference/
Auth header format for Disk API is `Authorization: OAuth <token>`.
"""
import os

import aiohttp

API = "https://cloud-api.yandex.net/v1/disk"


class YandexAuthError(Exception):
    """Raised on HTTP 401 so the caller can refresh the token and retry."""


def _headers(token: str) -> dict:
    return {"Authorization": f"OAuth {token}"}


async def _check(resp: aiohttp.ClientResponse, ok=(200, 201), allow=()):
    if resp.status == 401:
        raise YandexAuthError("Yandex token unauthorized")
    if resp.status in ok or resp.status in allow:
        return
    text = await resp.text()
    raise RuntimeError(f"Yandex API {resp.status}: {text}")


async def ensure_folder(session: aiohttp.ClientSession, token: str, path: str) -> None:
    """Create a folder at path. 409 (already exists) is fine."""
    async with session.put(
        f"{API}/resources", params={"path": path}, headers=_headers(token)
    ) as resp:
        # 201 created, 409 already exists
        await _check(resp, ok=(201,), allow=(409,))


async def get_disk_info(session: aiohttp.ClientSession, token: str) -> dict:
    """
    GET /v1/disk - general account info: total_space, used_space, trash_size.
    This is account-level info, not a resource under a specific path, so in
    principle it should be reachable even with the cloud_api:disk.app_folder
    scope (untested against a real restricted-scope token — worth confirming
    once deployed; if Yandex returns 403 here, the scope doesn't cover it and
    we'd need a broader one, e.g. cloud_api:disk.info, to show quota).
    """
    async with session.get(f"{API}/", headers=_headers(token)) as resp:
        await _check(resp, ok=(200,))
        return await resp.json()


async def upload_file(session: aiohttp.ClientSession, token: str,
                      local_path: str, remote_path: str) -> None:
    """Two-step upload: get an upload href, then PUT the file bytes to it."""
    async with session.get(
        f"{API}/resources/upload",
        params={"path": remote_path, "overwrite": "true"},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
        href = (await resp.json())["href"]

    with open(local_path, "rb") as f:
        async with session.put(href, data=f) as resp2:
            # the upload href is presigned; no auth header needed
            if resp2.status not in (201, 202):
                text = await resp2.text()
                raise RuntimeError(f"Yandex upload {resp2.status}: {text}")


async def publish_and_get_url(session: aiohttp.ClientSession, token: str,
                              path: str) -> str:
    """Publish a resource (anyone with link -> view) and return its public URL."""
    async with session.put(
        f"{API}/resources/publish", params={"path": path}, headers=_headers(token)
    ) as resp:
        await _check(resp, ok=(200,), allow=(201, 202))

    async with session.get(
        f"{API}/resources",
        params={"path": path, "fields": "public_url"},
        headers=_headers(token),
    ) as resp2:
        await _check(resp2, ok=(200,))
        data = await resp2.json()
        return data.get("public_url", "")
