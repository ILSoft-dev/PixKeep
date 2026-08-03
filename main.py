"""
main.py
v3.3 - CleanDrive Bot (multi-user, Yandex.Disk backend)

Changelog:
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

Flow: files -> metadata report (+GPS) -> clean? -> anonymize names? ->
folder name -> upload to user's Yandex.Disk (per-file, continue on error) ->
report + public link -> delete succeeded messages from chat.

Runs an aiohttp server (OAuth callback + Render port) alongside aiogram polling.
"""
import asyncio
import logging
import os
import shutil
import uuid

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from db import save_tokens, get_tokens, delete_user
from exif_utils import inspect_metadata, strip_exif
from disk_utils import (
    ensure_folder,
    upload_file,
    publish_and_get_url,
    YandexAuthError,
)
from oauth import build_auth_url, exchange_code, refresh_access_token

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

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
    waiting_rename_choice = State()
    waiting_folder_name = State()


# ---------------------------------------------------------------- commands ---
@dp.message(CommandStart())
async def start(message: Message):
    uid = message.from_user.id
    if get_tokens(uid):
        await message.answer(
            "Ты уже подключил Яндекс.Диск ✅\n\n"
            "Пришли фото или файлы (можно альбомом) — я спрошу, чистить ли "
            "метаданные, и загружу результат в папку на твоём Диске. После "
            "успешной загрузки я удалю исходные сообщения из чата, чтобы они "
            "не занимали место.\n\n"
            "⚠️ Присылай как файл (📎 → Файл), а не как обычное фото — иначе "
            "Telegram сам пережмёт изображение.\n\n"
            "/logout — отключить Диск."
        )
        return

    auth_url = build_auth_url(uid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подключить Яндекс.Диск", url=auth_url)]
    ])
    await message.answer(
        "Привет! Я CleanDrive — чищу метаданные (EXIF/GPS/IPTC/XMP) из фото и файлов "
        "без потери качества и складываю результат на твой Яндекс.Диск.\n\n"
        "Сначала подключи свой Диск — откроется страница Яндекса, войди и "
        "разреши доступ. Пароль вводится только у Яндекса, я его не вижу. "
        "Доступ ограничен: я работаю только со своей папкой «Приложения/CleanDrive», "
        "остальной Диск мне не виден.",
        reply_markup=kb,
    )


@dp.message(Command("logout"))
async def logout(message: Message):
    delete_user(message.from_user.id)
    await message.answer(
        "Отключил твой Яндекс.Диск. Чтобы подключить снова — /start.\n"
        "Токен удалён из базы. Доступ приложения можно также отозвать вручную "
        "на странице yandex.ru/id (Мои приложения)."
    )


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
    if not get_tokens(message.from_user.id):
        await message.answer("Сначала подключи Яндекс.Диск командой /start.")
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


async def _process(messages: list[Message], state: FSMContext, chat_id: int):
    """Download each message's media and build a per-file tracking list.
    Each item carries its own message_id so we know which chat message to
    delete later, and its own status once uploaded."""
    work_dir = os.path.join(config.TEMP_DIR, str(chat_id), uuid.uuid4().hex)
    os.makedirs(work_dir, exist_ok=True)

    items, gps_count = [], 0
    for msg in messages:
        path = await _download(msg, work_dir)
        if not path:
            continue
        items.append({
            "message_id": msg.message_id,
            "path": path,
            "orig_name": os.path.basename(path),
        })
        if inspect_metadata(path)["has_gps"]:
            gps_count += 1

    if not items:
        await bot.send_message(chat_id, "Не нашёл файлов для обработки.")
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    await state.update_data(items=items, work_dir=work_dir)

    note = f"Получил {len(items)} файл(ов)."
    if gps_count:
        note += f"\n⚠️ В {gps_count} из них есть GPS-координаты съёмки."
    note += "\n\nЧистить метаданные перед загрузкой?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Очистить метаданные", callback_data="clean:yes")],
        [InlineKeyboardButton(text="📤 Загрузить как есть", callback_data="clean:no")],
        [InlineKeyboardButton(text="❌ Отменить загрузку", callback_data="cancel")],
    ])
    await bot.send_message(chat_id, note, reply_markup=kb)


# ------------------------------------------------------------ clean choice ---
@dp.callback_query(F.data.startswith("clean:"))
async def on_clean_choice(cq: CallbackQuery, state: FSMContext):
    do_clean = cq.data.split(":")[1] == "yes"
    data = await state.get_data()
    items = data.get("items", [])
    work_dir = data.get("work_dir")

    if not items:
        await cq.message.edit_text("Файлы не найдены, начни заново.")
        await cq.answer()
        return

    if do_clean:
        clean_dir = os.path.join(work_dir, "clean")
        os.makedirs(clean_dir, exist_ok=True)
        for it in items:
            out = os.path.join(clean_dir, it["orig_name"])
            try:
                strip_exif(it["path"], out)
            except Exception as e:
                logging.error(f"strip failed {it['path']}: {e}")
                shutil.copy(it["path"], out)
            it["upload_path"] = out
    else:
        for it in items:
            it["upload_path"] = it["path"]

    await state.update_data(items=items, cleaned=do_clean)
    await state.set_state(Flow.waiting_rename_choice)

    verb = "Почистил" if do_clean else "Оставил как есть"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Да, переименовать в 001, 002…",
                              callback_data="rename:yes")],
        [InlineKeyboardButton(text="📄 Нет, оставить имена",
                              callback_data="rename:no")],
        [InlineKeyboardButton(text="❌ Отменить загрузку", callback_data="cancel")],
    ])
    await cq.message.edit_text(
        f"{verb} {len(items)} файл(ов).\n\n"
        "Обезличить имена файлов? Имя файла тоже может выдавать инфу.",
        reply_markup=kb,
    )
    await cq.answer()


# ------------------------------------------------------------------ cancel ---
@dp.callback_query(F.data == "cancel")
async def on_cancel(cq: CallbackQuery, state: FSMContext):
    """Works from any step of the flow (clean choice, rename choice, or
    folder-name prompt) — wipes downloaded/cleaned temp files and clears
    FSM state without uploading anything."""
    data = await state.get_data()
    work_dir = data.get("work_dir")
    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    await state.clear()
    await cq.message.edit_text("❌ Отменено. Ничего не загружено, временные файлы удалены.")
    await cq.answer("Отменено")


# ------------------------------------------------------------ rename choice --
@dp.callback_query(Flow.waiting_rename_choice, F.data.startswith("rename:"))
async def on_rename_choice(cq: CallbackQuery, state: FSMContext):
    do_rename = cq.data.split(":")[1] == "yes"
    data = await state.get_data()
    items = data.get("items", [])

    if not items:
        await cq.message.edit_text("Файлы не найдены, начни заново.")
        await state.clear()
        await cq.answer()
        return

    width = max(3, len(str(len(items))))
    for i, it in enumerate(items, start=1):
        if do_rename:
            ext = os.path.splitext(it["upload_path"])[1] or ".jpg"
            it["upload_name"] = f"{i:0{width}d}{ext}"
        else:
            it["upload_name"] = it["orig_name"]

    await state.update_data(items=items, rename=do_rename)
    await state.set_state(Flow.waiting_folder_name)

    tail = ("Имена будут 001, 002, 003…" if do_rename
            else "Оригинальные имена сохранены.")
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить загрузку", callback_data="cancel")],
    ])
    await cq.message.edit_text(
        f"Ок. {tail}\n\n"
        "Укажите имя папки на Яндекс.Диске. Если такой папки нет, она будет создана.",
        reply_markup=cancel_kb,
    )
    await cq.answer()


# ------------------------------------------------------------ folder + up ----
async def _upload_all(tokens: dict, telegram_id: int, items: list[dict],
                      folder_name: str) -> tuple[list[dict], str]:
    """Upload every item to the user's Yandex.Disk app folder.
    Continues past individual file failures (records them, doesn't abort).
    Refreshes the access token once on 401 and retries the whole batch
    (re-uploads are safe: overwrite=true, so already-uploaded files are
    just overwritten with the same bytes)."""
    safe_name = folder_name.replace("/", "-").replace("\\", "-").strip().strip(".")
    remote_dir = f"{config.APP_ROOT}{safe_name}"

    async def run(access_token: str) -> tuple[list[dict], str]:
        results = []
        async with aiohttp.ClientSession() as session:
            await ensure_folder(session, access_token, remote_dir)
            for it in items:
                try:
                    await upload_file(
                        session, access_token, it["upload_path"],
                        f"{remote_dir}/{it['upload_name']}"
                    )
                    results.append({**it, "success": True, "error": None})
                except YandexAuthError:
                    raise  # propagate: refresh token and retry the batch
                except Exception as e:
                    logging.error(f"upload failed for {it['upload_name']}: {e}")
                    results.append({**it, "success": False, "error": str(e)})
            url = await publish_and_get_url(session, access_token, remote_dir)
        return results, url

    try:
        return await run(tokens["access_token"])
    except YandexAuthError:
        new = await refresh_access_token(tokens["refresh_token"])
        save_tokens(telegram_id, new["access_token"], new["refresh_token"])
        return await run(new["access_token"])


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

    tokens = get_tokens(message.from_user.id)
    if not tokens:
        await message.answer("Диск не подключён. /start чтобы подключить.")
        await state.clear()
        return

    await message.answer(f"Загружаю в папку «{folder_name}»...")

    try:
        results, link = await _upload_all(
            tokens, message.from_user.id, items, folder_name
        )
    except Exception as e:
        logging.exception("upload failed")
        await message.answer(f"Ошибка при загрузке: {e}")
        if work_dir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)
        await state.clear()
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
    await message.answer("\n".join(lines))

    # Delete original chat messages only for files that actually uploaded.
    deleted, not_deleted = 0, 0
    for r in succeeded:
        try:
            await bot.delete_message(message.chat.id, r["message_id"])
            deleted += 1
        except Exception as e:
            logging.warning(f"couldn't delete message {r['message_id']}: {e}")
            not_deleted += 1

    if deleted or not_deleted:
        note = f"🗑 Удалил {deleted} исходных сообщений из чата."
        if not_deleted:
            note += (f" Не смог удалить {not_deleted} — обычно если прошло "
                     "слишком много времени, удали вручную при желании.")
        await message.answer(note)

    if work_dir and os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    await state.clear()


# ------------------------------------------------------- oauth web server ----
async def oauth_callback(request: web.Request) -> web.Response:
    state = request.query.get("state", "")
    code = request.query.get("code", "")
    error = request.query.get("error")
    if error:
        return web.Response(text=f"Отказано в доступе: {error}", content_type="text/plain")
    try:
        telegram_id, access_token, refresh_token = await exchange_code(state, code)
        save_tokens(telegram_id, access_token, refresh_token)
        await bot.send_message(
            telegram_id,
            "Яндекс.Диск подключён ✅ Теперь пришли фото или файлы."
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


async def main():
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    await run_web()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
