"""
drive_utils.py
v4.0 - Google Drive v3 REST API helpers (async, aiohttp, no Google client libs)

Mirrors the shape of the previous Yandex disk_utils.py so main.py's overall
flow barely has to change: ensure_folder / upload_file / publish_and_get_url.

API docs: https://developers.google.com/workspace/drive/api/reference/rest/v3
Auth header format: `Authorization: Bearer <token>`.
"""
import json

import aiohttp

API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


class GoogleAuthError(Exception):
    """Raised on HTTP 401 so the caller can refresh the token and retry."""


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _check(resp: aiohttp.ClientResponse, ok=(200, 201)):
    if resp.status == 401:
        raise GoogleAuthError("Google token unauthorized")
    if resp.status in ok:
        return
    text = await resp.text()
    raise RuntimeError(f"Google API {resp.status}: {text}")


async def ensure_folder(session: aiohttp.ClientSession, token: str,
                        folder_name: str) -> str:
    """Find or create a folder by name in the app's visible root, return its id.

    Note: with drive.file scope, the app can only see files/folders IT
    created (or that the user explicitly opened with it) — so this search
    will only find a folder of this name if OUR bot made it before.
    """
    safe_name = folder_name.replace("'", "\\'")
    query = (
        f"mimeType='application/vnd.google-apps.folder' and name='{safe_name}' "
        "and trashed=false"
    )
    async with session.get(
        f"{API}/files", params={"q": query, "fields": "files(id,name)"},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200,))
        data = await resp.json()

    files = data.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    async with session.post(
        f"{API}/files", json=metadata, headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200, 201))
        created = await resp.json()
        return created["id"]


async def upload_file(session: aiohttp.ClientSession, token: str,
                      local_path: str, folder_id: str, name: str) -> None:
    """Multipart upload of a single (modest-size) file into folder_id."""
    metadata = {"name": name, "parents": [folder_id]}
    with open(local_path, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field(
        "metadata", json.dumps(metadata),
        content_type="application/json; charset=UTF-8",
    )
    form.add_field("file", file_bytes, content_type="application/octet-stream")

    async with session.post(
        f"{UPLOAD_API}/files", params={"uploadType": "multipart"},
        data=form, headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200, 201))


async def publish_and_get_url(session: aiohttp.ClientSession, token: str,
                              folder_id: str) -> str:
    """Grant 'anyone with the link' reader access to the folder, return its URL."""
    async with session.post(
        f"{API}/files/{folder_id}/permissions",
        json={"type": "anyone", "role": "reader"},
        headers=_headers(token),
    ) as resp:
        await _check(resp, ok=(200, 201))

    return f"https://drive.google.com/drive/folders/{folder_id}"
