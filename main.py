"""
main.py
v5.3 - CleanDrive Bot (multi-user, multi-account, Google Drive backend, admin-only)

Changelog:
- v5.3: added auto_date_folder setting (/settings, third checkbox). When on,
        the bot skips the "укажите имя папки" text prompt entirely: it reads
        each file's EXIF capture date (falling back to the Telegram message's
        send time, then to today, if no EXIF date is present), picks one date
        for the whole batch (most common; ties go to the earliest), announces
        "Папка будет создана автоматически: DD.MM.YY", and uploads right away
        — no confirmation click, matching the "remove friction" goal from
        /settings itself. GPS/date extraction reuses the same exiftool pass
        already done for the GPS warning, no extra subprocess calls added.
        Upload+report+cleanup logic extracted into _do_upload() shared by
        both this path and the manual on_folder_name handler.
- v5.2: fixed the long freeze after sending files. Root cause: exiftool
        calls (inspect_metadata / strip_exif) are blocking OS subprocess
        calls; running them directly in an async handler froze the ENTIRE
        bot (no other updates processed) for the full duration. Now offloaded
        via asyncio.to_thread, and downloads / exif calls / Drive uploads all
        run concurrently (bounded by semaphores — DOWNLOAD_CONCURRENCY,
        EXIF_CONCURRENCY, UPLOAD_CONCURRENCY) instead of strictly one-by-one.
        Also added a Telegram command menu (the blue "menu" button) via
        bot.set_my_commands() at startup, listing /start /accounts /settings
        /logout.
- v5.1: added a global IsAdmin filter (dp.message.filter / dp.callback_query.filter)
        so ONLY config.ADMIN_ID can interact with the bot at all — anyone else's
        messages are dropped before reaching any handler, silently. This closes
        the "any stranger who finds the bot can connect their own Google
        account and use our infra/quota" exposure raised earlier.
- v5.0: multiple Google accounts per Telegram user. /start now offers
        "add another account" once one is connected; /accounts lists all
        connected accounts and lets you tap one to make it active (that's
        the one uploads go to). Any Google account can connect — not just
        the one that created the OAuth client — since the app is Published
        (In Production) with a non-sensitive scope, so there's no test-user
        allow-list. Settings (clean/anonymize) are now independent of which
        account is active — one set of preferences per Telegram user.
- v4.1: moved "чистить метаданные?" / "обезличить имена?" out of the
        per-upload flow and into standing settings, toggled anytime via
        /settings (checkboxes, persisted in Supabase). The upload flow is
        now just: files -> folder name -> link. Settings are applied
        automatically and silently (the note before the folder-name prompt
        just states what will happen, doesn't ask).
- v4.0: storage backend Yandex.Disk -> Google Drive. Reason: Yandex forces
        login for public FOLDER links (only individual files are login-free
        by design), which broke the "anyone with the link, no auth" goal.
        Google Drive's "anyone with the link" folder sharing has no such
        restriction. Uses drive.file scope (non-sensitive) via plain REST
        (aiohttp), no Google client libraries needed. App is published
        "In Production" so there's no 100-test-user cap and (to be
        confirmed over the coming week) no 7-day refresh-token expiry.
- v3.5: reverted /space (v3.4) — confirmed live that GET /v1/disk returns
        403 under cloud_api:disk.app_folder scope. Showing quota would
        require broadening to a whole-disk read scope, which isn't worth
        trading away the "bot only sees its own folder" guarantee. Back to
        app_folder-only, no disk-quota command.
- v3.3: added a "❌ Отменить загрузку" button at every step (clean choice,
        rename choice, folder-name prompt). Cancelling wipes any downloaded/
        cleaned temp files and clears FSM state — nothing gets uploaded.
- v3.2: fixed batching for files sent as separate messages (no shared
        media_group_id) — some Telegram clients send multi-file "as
        document" uploads this way instead of a real album. Now buffered
        per-chat with a debounce: each new file resets a short timer, and
        the whole burst is processed together once uploads pause.
- v3.1: per-file tracking (message_id -> path -> upload name -> status).
        Upload continues past individual failures instead of aborting the
        whole batch. Final report lists successes/failures by exact
        filename. Successfully uploaded messages are deleted from the chat
        (Telegram lets bots delete their own incoming private messages);
        deletion failures are reported honestly, not hidden.
- v3.0: storage backend Google Drive -> Yandex.Disk (accessible from BY/CIS).
        Per-user Yandex OAuth tokens (access+refresh) in Supabase, auto-refresh.
- v2.1: optional "обезличить имена" -> rename to 001, 002, 003…
- v2.0: multi-user OAuth, optional cleaning, shareable link.

Flow: files -> (settings applied silently: clean? anonymize names?) ->
folder name -> upload to the active Google account's Drive (per-file,
continue on error) -> report + public link -> delete succeeded messages.

Runs an aiohttp server (OAuth callback + Render port) alongside aiogram polling.
"""
import asyncio
import logging
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from db import (
    get_settings,
    set_setting,
    add_or_update_account,
    list_accounts,
    get_active_account,
    set_active_account,
    update_account_tokens,
    remove_all_accounts,
)
from exif_utils import inspect_metadata, strip_exif
from drive_utils import (
    ensure_folder,
    upload_file,
    publish_and_get_url,
    GoogleAuthError,
)
from oauth import build_auth_url, exchange_code, refresh_access_token, get_user_email

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class IsAdmin(BaseFilter):
    """Global gate: only config.ADMIN_ID may use the bot. Everyone else's
    messages/callbacks are simply not handled — no reply, no acknowledgment
    that the bot exists or does anything. Applied once here to the whole
    dispatcher, so every handler below is covered automatically."""
    async def __call__(self, event) -> bool:
        user = event.from_user
        return user is not None and user.id == config.ADMIN_ID


dp.message.filter(IsAdmin())
dp.callback_query.filter(IsAdmin())

media_groups: dict[str, list[Message]] = {}
media_group_tasks: dict[str, asyncio.Task] = {}

# Some Telegram clients send multi-file uploads (esp. "as file"/document)
# as several separate messages WITHOUT a shared media_group_id, instead of
# a real album. To still treat them as one batch, we buffer such messages
# per chat and debounce: each new arrival resets the wait timer, and only
# once SINGLES_DEBOUNCE_SECONDS pass with no new file do we process the batch.
SINGLES_DEBOUNCE_SECONDS = 2.0
pending_singles: dict[int, list[Message]] = {}
pending_singles_tasks: dict[int, asyncio.Task] = {}


class Flow(StatesGroup):
    waiting_folder_name = State()


# ---------------------------------------------------------------- commands ---
def _accounts_keyboard(accounts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for a in accounts:
        mark = "✅ " if a["is_active"] else ""
        rows.append([InlineKeyboardButton(
            text=f"{mark}{a['email']}", callback_data=f"acct:{a['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    accounts = list_accounts(uid)
    auth_url = build_auth_url(uid)

    if accounts:
        active = next((a for a in accounts if a["is_active"]), None)
        active_email = active["email"] if active else "не выбран — см. /accounts"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Подключить ещё аккаунт", url=auth_url)]
        ])
        await message.answer(
            f"Подключено аккаунтов: {len(accounts)}. Активный: {active_email}\n\n"
            "Пришли фото или файлы (можно альбомом) — загружу на активный "
            "аккаунт (спрошу только имя папки) и после успешной загрузки "
            "удалю исходные сообщения из чата.\n\n"
            "Чистка метаданных и обезличивание имён — настраиваются заранее в "
            "/settings.\n\n"
            "⚠️ Присылай как файл (📎 → Файл), а не как обычное фото — иначе "
            "Telegram сам пережмёт изображение.\n\n"
            "/accounts — посмотреть все аккаунты и переключиться\n"
            "/settings — настройки очистки/переименования\n"
            "/logout — отключить все аккаунты.",
            reply_markup=kb,
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подключить Google Drive", url=auth_url)]
    ])
    await message.answer(
        "Привет! Я CleanDrive — чищу метаданные (EXIF/GPS/IPTC/XMP) из фото и файлов "
        "без потери качества и складываю результат на твой Google Drive.\n\n"
        "Сначала подключи свой Диск — откроется страница Google, войди и "
        "разреши доступ. Пароль вводится только у Google, я его не вижу. "
        "Доступ ограничен: я вижу только файлы и папки, которые сам создам "
        "(scope drive.file) — остальной твой Диск мне не виден.\n\n"
        "Можно подключить сразу несколько своих Google-аккаунтов и "
        "переключаться между ними через /accounts.",
        reply_markup=kb,
    )


@dp.message(Command("accounts"))
async def accounts_cmd(message: Message):
    accounts = list_accounts(message.from_user.id)
    if not accounts:
        await message.answer("Нет подключённых аккаунтов. /start чтобы подключить.")
        return
    await message.answer(
        "Твои Google-аккаунты — нажми, чтобы сделать активным для загрузки:",
        reply_markup=_accounts_keyboard(accounts),
    )


@dp.callback_query(F.data.startswith("acct:"))
async def on_switch_account(cq: CallbackQuery):
    account_id = int(cq.data.split(":", 1)[1])
    set_active_account(cq.from_user.id, account_id)
    accounts = list_accounts(cq.from_user.id)
    await cq.message.edit_reply_markup(reply_markup=_accounts_keyboard(accounts))
    await cq.answer("Активный аккаунт изменён")


@dp.message(Command("logout"))
async def logout(message: Message):
    remove_all_accounts(message.from_user.id)
    await message.answer(
        "Отключил все твои Google-аккаунты. Чтобы подключить снова — /start.\n"
        "Токены удалены из базы. Доступ приложения можно также отозвать вручную "
        "на странице myaccount.google.com/permissions."
    )


def _settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    clean_mark = "✅" if s["clean_metadata"] else "⬜"
    rename_mark = "✅" if s["anonymize_names"] else "⬜"
    date_mark = "✅" if s["auto_date_folder"] else "⬜"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{clean_mark} Чистить метаданные (EXIF/GPS)",
            callback_data="toggle:clean_metadata",
        )],
        [InlineKeyboardButton(
            text=f"{rename_mark} Обезличивать имена (001, 002…)",
            callback_data="toggle:anonymize_names",
        )],
        [InlineKeyboardButton(
            text=f"{date_mark} Папка по дате съёмки (без вопроса)",
            callback_data="toggle:auto_date_folder",
        )],
    ])


@dp.message(Command("settings"))
async def settings_cmd(message: Message):
    if not get_active_account(message.from_user.id):
        await message.answer("Сначала подключи Google Drive командой /start.")
        return
    s = get_settings(message.from_user.id)
    await message.answer(
        "⚙️ Настройки загрузки — применяются к каждой следующей загрузке "
        "автоматически, спрашивать не буду:",
        reply_markup=_settings_keyboard(s),
    )


@dp.callback_query(F.data.startswith("toggle:"))
async def on_toggle_setting(cq: CallbackQuery):
    field = cq.data.split(":", 1)[1]
    s = get_settings(cq.from_user.id)
    if field not in s:
        await cq.answer()
        return
    new_value = not s[field]
    set_setting(cq.from_user.id, field, new_value)
    s[field] = new_value
    await cq.message.edit_reply_markup(reply_markup=_settings_keyboard(s))
    await cq.answer("Обновлено")


# ------------------------------------------------------------ media intake ---
async def _download(message: Message, dest_dir: str) -> str | None:
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or f"{uuid.uuid4().hex}.jpg"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = f"{uuid.uuid4().hex}.jpg"
    else:
        return None

    path = os.path.join(dest_dir, file_name)
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=path)
    return path


@dp.message(F.photo | F.document)
async def handle_media(message: Message, state: FSMContext):
    if not get_active_account(message.from_user.id):
        await message.answer("Сначала подключи Google Drive командой /start.")
        return

    if message.media_group_id:
        gid = message.media_group_id
        media_groups.setdefault(gid, []).append(message)
        if gid not in media_group_tasks:
            media_group_tasks[gid] = asyncio.create_task(
                _finish_group(gid, state, message.chat.id)
            )
    else:
        chat_id = message.chat.id
        pending_singles.setdefault(chat_id, []).append(message)
        # Cancel any previously scheduled flush for this chat and reschedule —
        # this is what lets a burst of individually-sent files get grouped.
        old_task = pending_singles_tasks.get(chat_id)
        if old_task and not old_task.done():
            old_task.cancel()
        pending_singles_tasks[chat_id] = asyncio.create_task(
            _finish_singles(chat_id, state)
        )


async def _finish_singles(chat_id: int, state: FSMContext):
    try:
        await asyncio.sleep(SINGLES_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return  # a newer file arrived and rescheduled us; that task will run
    messages = pending_singles.pop(chat_id, [])
    pending_singles_tasks.pop(chat_id, None)
    if messages:
        await _process(messages, state, chat_id)


async def _finish_group(gid: str, state: FSMContext, chat_id: int):
    await asyncio.sleep(1.0)
    messages = media_groups.pop(gid, [])
    media_group_tasks.pop(gid, None)
    if messages:
        await _process(messages, state, chat_id)


# Concurrency caps for the steps below — modest by design: Render's free
# tier has limited CPU, and exiftool spawns a real OS subprocess per call,
# so too much parallelism there would thrash rather than help. Tune if needed.
DOWNLOAD_CONCURRENCY = 5
EXIF_CONCURRENCY = 4

# Used only as a display/fallback timezone for the auto-date-folder feature
# (EXIF timestamps have no timezone info at all, and Telegram's message.date
# is UTC) — approximates "today" the way a person in Belarus would see it.
LOCAL_TZ = ZoneInfo("Europe/Minsk")

_EXIF_DATETIME_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _parse_exif_date(raw: str | None):
    """Parse an EXIF DateTimeOriginal/CreateDate string into a plain date.
    Returns None if raw is empty or doesn't match a known format."""
    if not raw:
        return None
    for fmt in _EXIF_DATETIME_FORMATS:
        try:
            return datetime.strptime(raw[:19], fmt).date()
        except ValueError:
            continue
    return None


def _pick_folder_date(exif_dates: list, messages: list[Message]):
    """One date for the whole batch: the most common EXIF capture date
    (ties broken by the earliest), or — if no file had a usable EXIF date
    at all — the earliest message's send time, converted to LOCAL_TZ."""
    if exif_dates:
        counts = Counter(exif_dates)
        top = max(counts.values())
        return min(d for d, c in counts.items() if c == top)

    send_times = [m.date for m in messages if m.date]
    if send_times:
        dt = min(send_times)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).date()

    return datetime.now(LOCAL_TZ).date()


async def _process(messages: list[Message], state: FSMContext, chat_id: int):
    """Download each message's media, apply the user's standing settings
    (clean metadata / anonymize names — see /settings) immediately, and go
    straight to asking for a folder name. No per-upload questions anymore.

    Downloads and exiftool calls run concurrently (bounded by semaphores)
    instead of one-by-one. Crucially, exiftool itself is a blocking OS
    subprocess call — running it directly in this coroutine would freeze
    the whole bot (no other messages could be handled) for as long as it
    takes to process every file. asyncio.to_thread() offloads each call to
    a worker thread so the event loop stays responsive throughout.

    Each item carries its own message_id so we know which chat message to
    delete later, and its own status once uploaded."""
    telegram_id = messages[0].from_user.id
    settings = get_settings(telegram_id)

    work_dir = os.path.join(config.TEMP_DIR, str(chat_id), uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)

    dl_sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def _download_bounded(msg: Message) -> str | None:
        async with dl_sem:
            return await _download(msg, work_dir)

    paths = await asyncio.gather(*[_download_bounded(msg) for msg in messages])

    items = []
    for msg, path in zip(messages, paths):
        if not path:
            continue
        items.append({
            "message_id": msg.message_id,
            "path": path,
            "orig_name": os.path.basename(path),
        })

    if not items:
        await bot.send_message(chat_id, "Не нашёл файлов для обработки.")
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    exif_sem = asyncio.Semaphore(EXIF_CONCURRENCY)

    async def _inspect(it: dict) -> dict:
        async with exif_sem:
            return await asyncio.to_thread(inspect_metadata, it["path"])

    metas = await asyncio.gather(*[_inspect(it) for it in items])
    gps_count = sum(1 for m in metas if m["has_gps"])
    exif_dates = [d for d in (_parse_exif_date(m["datetime"]) for m in metas) if d]

    if settings["clean_metadata"]:
        clean_dir = os.path.join(work_dir, "clean")
        os.makedirs(clean_dir, exist_ok=True)

        async def _clean_one(it: dict) -> None:
            out = os.path.join(clean_dir, it["orig_name"])
            async with exif_sem:
                try:
                    await asyncio.to_thread(strip_exif, it["path"], out)
                except Exception as e:
                    logging.error(f"strip failed {it['path']}: {e}")
                    shutil.copy(it["path"], out)
            it["upload_path"] = out

        await asyncio.gather(*[_clean_one(it) for it in items])
    else:
        for it in items:
            it["upload_path"] = it["path"]

    width = max(3, len(str(len(items))))
    for i, it in enumerate(items, start=1):
        if settings["anonymize_names"]:
            ext = os.path.splitext(it["upload_path"])[1] or ".jpg"
            it["upload_name"] = f"{i:0{width}d}{ext}"
        else:
            it["upload_name"] = it["orig_name"]

    note = f"Получил {len(items)} файл(ов)."
    if gps_count:
        note += f"\n⚠️ В {gps_count} из них есть GPS-координаты съёмки."
    note += (
        f"\nМетаданные: {'чищу' if settings['clean_metadata'] else 'оставляю как есть'}. "
        f"Имена: {'обезличиваю (001, 002…)' if settings['anonymize_names'] else 'оставляю оригинальные'}. "
        "(меняется в /settings)"
    )

    if settings["auto_date_folder"]:
        folder_name = _pick_folder_date(exif_dates, messages).strftime("%d.%m.%y")
        note += f"\n\n📁 Папка будет создана автоматически: {folder_name}"
        await bot.send_message(chat_id, note)
        await _do_upload(chat_id, telegram_id, items, work_dir, folder_name)
        return

    await state.update_data(items=items, work_dir=work_dir)
    await state.set_state(Flow.waiting_folder_name)

    note += "\n\nУкажите имя папки на Google Drive. Если такой папки нет, она будет создана."
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить загрузку", callback_data="cancel")],
    ])
    await bot.send_message(chat_id, note, reply_markup=cancel_kb)


# ------------------------------------------------------------------ cancel ---
@dp.callback_query(F.data == "cancel")
async def on_cancel(cq: CallbackQuery, state: FSMContext):
    """Wipes downloaded/cleaned temp files and clears FSM state without
    uploading anything."""
    data = await state.get_data()
    work_dir = data.get("work_dir")
    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    await state.clear()
    await cq.message.edit_text("❌ Отменено. Ничего не загружено, временные файлы удалены.")
    await cq.answer("Отменено")


# ------------------------------------------------------------ folder + up ----
UPLOAD_CONCURRENCY = 4


async def _upload_all(account: dict, items: list[dict],
                      folder_name: str) -> tuple[list[dict], str]:
    """Upload every item to the user's Google Drive folder, concurrently
    (bounded by UPLOAD_CONCURRENCY). Continues past individual file
    failures (records them, doesn't abort).

    Unlike Yandex.Disk (which uploads with overwrite=true, making retries
    harmless), Drive's file-create call always makes a NEW file — so a
    naive whole-batch retry on token expiry would leave duplicates behind.
    Instead we refresh the access token at most once — an asyncio.Lock
    makes that safe even if several concurrent uploads hit 401 at the same
    time (only the first actually calls Google; the rest just wait for it
    and reuse the refreshed token) — and only the items that actually hit
    the 401 get retried; everything already uploaded stays untouched.
    """
    folder_name = folder_name.strip()  # Drive folder names are plain metadata,
                                        # not a path, so no slash-stripping needed
    access_token = account["access_token"]
    account_id = account["id"]
    refreshed = False
    refresh_lock = asyncio.Lock()

    async def refresh_once() -> str:
        nonlocal access_token, refreshed
        async with refresh_lock:
            if not refreshed:
                new = await refresh_access_token(account["refresh_token"])
                access_token = new["access_token"]
                update_account_tokens(account_id, new["access_token"], new["refresh_token"])
                refreshed = True
        return access_token

    sem = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    results: list[dict | None] = [None] * len(items)

    async with aiohttp.ClientSession() as session:
        try:
            folder_id = await ensure_folder(session, access_token, folder_name)
        except GoogleAuthError:
            access_token = await refresh_once()
            folder_id = await ensure_folder(session, access_token, folder_name)

        async def _upload_one(index: int, it: dict) -> None:
            async with sem:
                try:
                    await upload_file(
                        session, access_token, it["upload_path"],
                        folder_id, it["upload_name"],
                    )
                    results[index] = {**it, "success": True, "error": None}
                    return
                except GoogleAuthError:
                    pass  # fall through to a single refresh-and-retry below
                except Exception as e:
                    logging.error(f"upload failed for {it['upload_name']}: {e}")
                    results[index] = {**it, "success": False, "error": str(e)}
                    return

                try:
                    token = await refresh_once()
                    await upload_file(
                        session, token, it["upload_path"],
                        folder_id, it["upload_name"],
                    )
                    results[index] = {**it, "success": True, "error": None}
                except Exception as e:
                    logging.error(f"upload failed for {it['upload_name']} after refresh: {e}")
                    results[index] = {**it, "success": False, "error": str(e)}

        await asyncio.gather(*[_upload_one(i, it) for i, it in enumerate(items)])

        url = await publish_and_get_url(session, access_token, folder_id)
    return results, url


async def _do_upload(chat_id: int, telegram_id: int, items: list[dict],
                     work_dir: str | None, folder_name: str) -> None:
    """Shared by both the manual folder-name prompt and the auto-date-folder
    path: upload, report per-file success/failure, delete succeeded chat
    messages, clean up temp files."""
    account = get_active_account(telegram_id)
    if not account:
        await bot.send_message(chat_id, "Диск не подключён. /start чтобы подключить.")
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        return

    await bot.send_message(chat_id, f"Загружаю в папку «{folder_name}»...")

    try:
        results, link = await _upload_all(account, items, folder_name)
    except Exception as e:
        logging.exception("upload failed")
        await bot.send_message(chat_id, f"Ошибка при загрузке: {e}")
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        return

    succeeded = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    lines = []
    if succeeded:
        names = ", ".join(r["upload_name"] for r in succeeded)
        lines.append(f"✅ Загружено ({len(succeeded)}): {names}")
    if failed:
        names = ", ".join(r["orig_name"] for r in failed)
        lines.append(f"❌ Не удалось загрузить ({len(failed)}): {names}")
    if succeeded:
        lines.append(f"\nПапка (доступ на чтение всем, у кого есть ссылка):\n{link}")
    await bot.send_message(chat_id, "\n".join(lines))

    # Delete original chat messages only for files that actually uploaded.
    deleted, not_deleted = 0, 0
    for r in succeeded:
        try:
            await bot.delete_message(chat_id, r["message_id"])
            deleted += 1
        except Exception as e:
            logging.warning(f"couldn't delete message {r['message_id']}: {e}")
            not_deleted += 1

    if deleted or not_deleted:
        note = f"🗑 Удалил {deleted} исходных сообщений из чата."
        if not_deleted:
            note += (f" Не смог удалить {not_deleted} — обычно если прошло "
                     "слишком много времени, удали вручную при желании.")
        await bot.send_message(chat_id, note)

    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)


@dp.message(Flow.waiting_folder_name, F.text)
async def on_folder_name(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("items", [])
    work_dir = data.get("work_dir")
    folder_name = message.text.strip()

    if not items:
        await message.answer("Нет файлов для загрузки, начни заново.")
        await state.clear()
        return

    await state.clear()
    await _do_upload(message.chat.id, message.from_user.id, items, work_dir, folder_name)


# ------------------------------------------------------- oauth web server ----
async def oauth_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    error = request.query.get("error")
    if error:
        return web.Response(text=f"Отказано в доступе: {error}", content_type="text/plain")
    try:
        telegram_id, access_token, refresh_token = await exchange_code(state, code)
        email = await get_user_email(access_token)
        add_or_update_account(telegram_id, email, access_token, refresh_token)
        await bot.send_message(
            telegram_id,
            f"Google Drive подключён ✅ ({email})\nТеперь пришли фото или файлы."
        )
        return web.Response(
            text="Готово! Диск подключён. Можешь вернуться в Telegram.",
            content_type="text/plain",
        )
    except Exception as e:
        logging.exception("oauth callback failed")
        return web.Response(text=f"Ошибка авторизации: {e}", content_type="text/plain")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_web():
    app = web.Application()
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    logging.info(f"web server on :{config.PORT}")


BOT_COMMANDS = [
    BotCommand(command="start", description="Подключить Диск / статус"),
    BotCommand(command="accounts", description="Аккаунты: список и переключение"),
    BotCommand(command="settings", description="Настройки очистки и переименования"),
    BotCommand(command="logout", description="Отключить все аккаунты"),
]


async def main():
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    await bot.set_my_commands(BOT_COMMANDS)
    await run_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
