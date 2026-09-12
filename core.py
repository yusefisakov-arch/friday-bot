"""Базовый слой: конфигурация, пул соединений с БД, общие утилиты.
Не зависит от остальных модулей проекта."""
import os
import logging
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import psycopg2
from psycopg2 import pool as pg_pool
from telegram.constants import ParseMode
from telegram.error import BadRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# httpx пишет в лог полный URL запроса, а у Telegram токен стоит прямо в пути —
# в итоге он оседает в логах Railway. Оставляем от httpx только предупреждения.
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
if not ALLOWED_USER_ID:
    logger.critical("ALLOWED_USER_ID не задан — бот не будет отвечать никому, "
                    "пока переменная не настроена!")

DATABASE_URL = os.environ["DATABASE_URL"]
LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Chisinau"))

_pool = pg_pool.ThreadedConnectionPool(1, 10, DATABASE_URL)


@contextmanager
def db_conn():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def is_allowed(user_id):
    return ALLOWED_USER_ID != 0 and user_id == ALLOWED_USER_ID


def now_local():
    return datetime.now(LOCAL_TZ)


TG_LIMIT = 4000  # лимит сообщения Telegram ~4096, берём с запасом


def split_chunks(text, limit=TG_LIMIT):
    """Режет длинный текст на части ≤ limit, по возможности по переносам строк."""
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


async def reply_md(message, text, **kwargs):
    chunks = split_chunks(text)
    for i, chunk in enumerate(chunks):
        kw = kwargs if i == len(chunks) - 1 else {}
        try:
            await message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN, **kw)
        except BadRequest:
            await message.reply_text(chunk, **kw)


async def send_md(bot, chat_id, text, **kwargs):
    chunks = split_chunks(text)
    for i, chunk in enumerate(chunks):
        kw = kwargs if i == len(chunks) - 1 else {}
        try:
            await bot.send_message(chat_id=chat_id, text=chunk,
                                   parse_mode=ParseMode.MARKDOWN, **kw)
        except BadRequest:
            await bot.send_message(chat_id=chat_id, text=chunk, **kw)
