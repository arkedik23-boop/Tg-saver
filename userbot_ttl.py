"""
Исчезающие (одноразовые / с таймером) фото и видео с НЕСКОЛЬКИХ аккаунтов -> в один админ-чат.

Business API такие медиа ботам не отдаёт, поэтому тут юзерботы на Telethon
(по одной сессии на аккаунт). Файл скачивается и уходит админу через бота,
так что аккаунтам не нужно знать админа или писать ему.

Первый вход (один раз на каждый аккаунт):
    python userbot_ttl.py login acc1
    python userbot_ttl.py login acc2
Дальше работа всех сразу:
    python userbot_ttl.py run

Env: BOT_TOKEN, ADMIN_ID (админ должен хоть раз нажать /start у бота), API_ID, API_HASH
     (API_ID/API_HASH с my.telegram.org; можно одни на все аккаунты),
     SESSIONS=acc1,acc2,acc3  — список имён сессий.

Риски: серая зона правил Telegram (возможен бан аккаунтов), отправитель рассчитывает,
что файл не сохранится. Не тестировалось на живых аккаунтах.
"""
import asyncio
import html
import os
import sys
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
from telethon import TelegramClient, events

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
SESSIONS = [s.strip() for s in os.getenv("SESSIONS", "acc1").split(",") if s.strip()]

DATA = Path(os.getenv("TTL_DIR", "data/ttl"))
DATA.mkdir(parents=True, exist_ok=True)
Path("data").mkdir(exist_ok=True)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def make_client(name: str) -> TelegramClient:
    return TelegramClient(f"data/{name}", API_ID, API_HASH)


def attach(client: TelegramClient, session_name: str):
    @client.on(events.NewMessage(incoming=True))
    async def handler(ev):
        if not ev.is_private:
            return
        msg = ev.message
        ttl = getattr(msg.media, "ttl_seconds", None)
        if not ttl:
            return
        me = await client.get_me()
        acc = f"{me.first_name or ''} {me.last_name or ''}".strip()
        if me.username:
            acc += f" (@{me.username})"
        sender = await ev.get_sender()
        sname = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip()
        if getattr(sender, "username", None):
            sname += f" (@{sender.username})"

        path = await msg.download_media(file=str(DATA / f"{session_name}_{ev.chat_id}_{msg.id}"))
        if not path:
            await bot.send_message(
                ADMIN_ID,
                f"👤 <b>Аккаунт:</b> {html.escape(acc)}\n⏳ Исчезающее медиа от "
                f"{html.escape(sname)}, но скачать не вышло.",
            )
            return
        caption = (
            f"👤 <b>Аккаунт:</b> {html.escape(acc)}\n"
            f"⏳ Исчезающее медиа от <b>{html.escape(sname)}</b>, таймер {ttl}с"
        )
        f = FSInputFile(path)
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            await bot.send_photo(ADMIN_ID, f, caption=caption)
        elif ext in (".mp4", ".mov", ".mkv"):
            await bot.send_video(ADMIN_ID, f, caption=caption)
        else:
            await bot.send_document(ADMIN_ID, f, caption=caption)
        # копия самому аккаунту — в его «Избранное»
        await client.send_file(
            "me", path, caption=f"⏳ Исчезающее медиа от {sname}, таймер {ttl}с"
        )


async def run_all():
    clients = []
    for name in SESSIONS:
        c = make_client(name)
        await c.connect()
        if not await c.is_user_authorized():
            print(f"[{name}] не авторизован, сначала: python userbot_ttl.py login {name}")
            continue
        attach(c, name)
        clients.append(c)
        print(f"[{name}] запущен")
    if not clients:
        sys.exit("Нет авторизованных сессий.")
    await asyncio.gather(*(c.run_until_disconnected() for c in clients))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "login":
        make_client(sys.argv[2]).start()  # спросит телефон и код
        print("OK, сессия сохранена")
    else:
        asyncio.run(run_all())
