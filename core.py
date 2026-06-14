"""Базовый слой: конфигурация, пул соединений с БД, общие утилиты.
Не зависит от остальных модулей проекта."""
import os
import logging
import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import psycopg2
from psycopg2 import pool as pg_pool
from telegram import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
if not ALLOWED_USER_ID:
    logger.critical("ALLOWED_USER_ID не задан — бот не будет отвечать никому, пока переменная не настроена!")

DATABASE_URL = os.environ["DATABASE_URL"]
MSK = ZoneInfo("Europe/Moscow")
HISTORY_WINDOW = 20  # сколько последних сообщений держим в оперативной памяти диалога
HISTORY_KEEP = 300   # сколько строк истории храним в базе (старше — чистим)

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
# Секрет для доступа к /api/* из мини-форм. Стабильно выводится из токена бота,
# попадает только в ссылки клавиатуры, которую видит лишь разрешённый пользователь.
WEBAPP_SECRET = hashlib.sha256(("webapp-api:" + TELEGRAM_TOKEN).encode()).hexdigest()[:32]


def webapp_url(path):
    return f"{WEBAPP_URL}/{path}?k={WEBAPP_SECRET}"

STAFF = {
    "Жанель": "финансы, администрация",
    "Эдик": "исполнитель, руками",
    "Филадельфия": "всё остальное",
}

QUICK_TASK_BUTTON = "📝 Задача"
QUICK_FINANCE_BUTTON = "💰 Финансы"
QUICK_DECISION_BUTTON = "🤝 Договорённость"
VIEW_TASKS_BUTTON = "📋 Задачи"
VIEW_DECISIONS_BUTTON = "📒 Договорённости"
VIEW_FINANCE_BUTTON = "💵 Баланс"
APARTMENT_BUTTON = "🏠 Квартиры"
VIEW_APARTMENT_BALANCE_BUTTON = "🏦 Касса квартир"
MOVE_IN_BUTTON = "🔑 Заселение"
MOVE_OUT_BUTTON = "🚪 Выселение"
UTILITIES_BUTTON = "⚡ Коммуналка"

DEADLINE_KEYBOARD = ReplyKeyboardMarkup([["Сегодня", "Завтра"], ["Нет"]], resize_keyboard=True)
PRIORITY_KEYBOARD = ReplyKeyboardMarkup([["Высокий", "Средний", "Низкий"], ["Нет"]], resize_keyboard=True)
ASSIGNEE_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys()), ["Нет"]], resize_keyboard=True)
WITH_WHOM_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys())], resize_keyboard=True)
FINANCE_TYPE_KEYBOARD = ReplyKeyboardMarkup([["Расход", "Доход"]], resize_keyboard=True)
SKIP_KEYBOARD = ReplyKeyboardMarkup([["Нет"]], resize_keyboard=True)

if WEBAPP_URL:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [KeyboardButton(QUICK_TASK_BUTTON, web_app=WebAppInfo(url=webapp_url("form"))), VIEW_TASKS_BUTTON],
            [KeyboardButton(QUICK_FINANCE_BUTTON, web_app=WebAppInfo(url=webapp_url("finance"))), VIEW_FINANCE_BUTTON],
            [KeyboardButton(QUICK_DECISION_BUTTON, web_app=WebAppInfo(url=webapp_url("decisions"))), VIEW_DECISIONS_BUTTON],
            [KeyboardButton(APARTMENT_BUTTON, web_app=WebAppInfo(url=webapp_url("apartments"))), VIEW_APARTMENT_BALANCE_BUTTON],
            [KeyboardButton(MOVE_IN_BUTTON, web_app=WebAppInfo(url=webapp_url("move_in"))),
             KeyboardButton(MOVE_OUT_BUTTON, web_app=WebAppInfo(url=webapp_url("move_out")))],
            [KeyboardButton(UTILITIES_BUTTON, web_app=WebAppInfo(url=webapp_url("utilities")))],
        ],
        resize_keyboard=True,
    )
else:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [QUICK_TASK_BUTTON, VIEW_TASKS_BUTTON],
            [QUICK_FINANCE_BUTTON, VIEW_FINANCE_BUTTON],
            [QUICK_DECISION_BUTTON, VIEW_DECISIONS_BUTTON],
            [APARTMENT_BUTTON, VIEW_APARTMENT_BALANCE_BUTTON],
            [MOVE_IN_BUTTON, MOVE_OUT_BUTTON],
            [UTILITIES_BUTTON],
        ],
        resize_keyboard=True,
    )

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


def now_msk():
    return datetime.now(MSK)


def today_msk():
    return now_msk().date()


def to_decimal(value):
    """Безопасно привести число (int/float/str/Decimal) к Decimal. None -> None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


async def reply_md(message, text, **kwargs):
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest:
        await message.reply_text(text, **kwargs)


async def send_md(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
