import os
import json
import logging
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool as pg_pool
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
from aiohttp import web
from anthropic import Anthropic
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
if not ALLOWED_USER_ID:
    logger.critical("ALLOWED_USER_ID не задан — бот не будет отвечать никому, пока переменная не настроена!")

DATABASE_URL = os.environ["DATABASE_URL"]
MSK = ZoneInfo("Europe/Moscow")
conversation_history = []

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

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

DEADLINE_KEYBOARD = ReplyKeyboardMarkup([["Сегодня", "Завтра"], ["Нет"]], resize_keyboard=True)
PRIORITY_KEYBOARD = ReplyKeyboardMarkup([["Высокий", "Средний", "Низкий"], ["Нет"]], resize_keyboard=True)
ASSIGNEE_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys()), ["Нет"]], resize_keyboard=True)
WITH_WHOM_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys())], resize_keyboard=True)
FINANCE_TYPE_KEYBOARD = ReplyKeyboardMarkup([["Расход", "Доход"]], resize_keyboard=True)
SKIP_KEYBOARD = ReplyKeyboardMarkup([["Нет"]], resize_keyboard=True)

if WEBAPP_URL:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [KeyboardButton(QUICK_TASK_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/form")), VIEW_TASKS_BUTTON],
            [KeyboardButton(QUICK_FINANCE_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/finance")), VIEW_FINANCE_BUTTON],
            [KeyboardButton(QUICK_DECISION_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/decisions")), VIEW_DECISIONS_BUTTON],
        ],
        resize_keyboard=True,
    )
else:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [QUICK_TASK_BUTTON, VIEW_TASKS_BUTTON],
            [QUICK_FINANCE_BUTTON, VIEW_FINANCE_BUTTON],
            [QUICK_DECISION_BUTTON, VIEW_DECISIONS_BUTTON],
        ],
        resize_keyboard=True,
    )

_pool = pg_pool.SimpleConnectionPool(1, 5, DATABASE_URL)


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


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            deadline TEXT,
            priority TEXT,
            status TEXT DEFAULT 'Открыта',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY,
            with_whom TEXT NOT NULL,
            what_decided TEXT NOT NULL,
            deadline TEXT,
            next_step TEXT,
            status TEXT DEFAULT 'Открыта',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS finance (
            id SERIAL PRIMARY KEY,
            amount NUMERIC NOT NULL,
            category TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'расход',
            comment TEXT,
            date DATE DEFAULT CURRENT_DATE
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS preferences (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS conversation_history (
            id SERIAL PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.commit()
    finally:
        conn.close()

    # Миграции для баз, созданных до перехода amount->NUMERIC и добавления type
    conn = psycopg2.connect(DATABASE_URL)
    try:
        c = conn.cursor()
        try:
            c.execute("ALTER TABLE finance ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'расход'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция finance.type пропущена: {e}")
        try:
            c.execute("ALTER TABLE finance ALTER COLUMN amount TYPE NUMERIC USING amount::numeric")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция finance.amount пропущена: {e}")
    finally:
        conn.close()


def db_create_task(name, deadline=None, priority=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO tasks (name, deadline, priority) VALUES (%s, %s, %s)", (name, deadline, priority))


def db_get_tasks():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT name, deadline, priority FROM tasks WHERE status != 'Готово' ORDER BY created_at DESC")
        rows = c.fetchall()
    if not rows:
        return "Открытых задач нет."
    result = []
    for name, deadline, priority in rows:
        line = f"- {name}"
        if deadline:
            line += f" (дедлайн: {deadline})"
        if priority:
            line += f" [{priority}]"
        result.append(line)
    return "\n".join(result)


def db_get_urgent_tasks():
    with db_conn() as conn:
        c = conn.cursor()
        tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        c.execute("""SELECT name, deadline, priority FROM tasks
                     WHERE status != 'Готово' AND deadline IS NOT NULL
                     AND deadline <= %s ORDER BY deadline ASC LIMIT 5""", (tomorrow,))
        rows = c.fetchall()
    if not rows:
        return ""
    result = []
    for name, deadline, priority in rows:
        result.append(f"- {name} (дедлайн: {deadline})")
    return "\n".join(result)


def db_close_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [name for _, name in rows]
        task_id, name = rows[0]
        c.execute("UPDATE tasks SET status='Готово' WHERE id=%s", (task_id,))
        return "closed", [name]


def db_update_task(name_part, deadline=None, priority=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [name for _, name in rows]
        task_id, name = rows[0]
        if deadline is not None:
            c.execute("UPDATE tasks SET deadline=%s WHERE id=%s", (deadline, task_id))
        if priority is not None:
            c.execute("UPDATE tasks SET priority=%s WHERE id=%s", (priority, task_id))
        return "updated", [name]


def db_find_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT name, deadline, priority, status, created_at FROM tasks
                     WHERE name ILIKE %s ORDER BY created_at DESC LIMIT 5""", (f"%{name_part}%",))
        rows = c.fetchall()
    if not rows:
        return "Ничего не найдено."
    result = []
    for name, deadline, priority, status, created_at in rows:
        line = f"- {name} [{status}]"
        if deadline:
            line += f" (дедлайн: {deadline})"
        if priority:
            line += f" [{priority}]"
        line += f", создана {created_at.strftime('%d.%m.%Y')}"
        result.append(line)
    return "\n".join(result)


def db_create_decision(with_whom, what_decided, deadline=None, next_step=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO decisions (with_whom, what_decided, deadline, next_step) VALUES (%s, %s, %s, %s)",
                  (with_whom, what_decided, deadline, next_step))


def db_get_decisions():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT with_whom, what_decided, deadline FROM decisions WHERE status='Открыта' ORDER BY created_at DESC LIMIT 10")
        rows = c.fetchall()
    if not rows:
        return "Открытых договорённостей нет."
    result = []
    for with_whom, what_decided, deadline in rows:
        line = f"- {with_whom}: {what_decided}"
        if deadline:
            line += f" (до {deadline})"
        result.append(line)
    return "\n".join(result)


def db_close_decision(text_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, with_whom, what_decided FROM decisions
                     WHERE status='Открыта' AND (with_whom ILIKE %s OR what_decided ILIKE %s)""",
                  (f"%{text_part}%", f"%{text_part}%"))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [f"{w}: {d}" for _, w, d in rows]
        dec_id, w, d = rows[0]
        c.execute("UPDATE decisions SET status='Готово' WHERE id=%s", (dec_id,))
        return "closed", [f"{w}: {d}"]


def db_create_finance(amount, category, fin_type="расход", comment=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO finance (amount, category, type, comment) VALUES (%s, %s, %s, %s)",
                  (amount, category, fin_type, comment))


def db_get_finance():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT amount, category, type, date FROM finance ORDER BY date DESC, id DESC LIMIT 10")
        rows = c.fetchall()
        c.execute("""SELECT type, COALESCE(SUM(amount), 0) FROM finance
                     WHERE date >= date_trunc('month', CURRENT_DATE) GROUP BY type""")
        totals = dict(c.fetchall())
    if not rows:
        return "Финансовых записей нет."
    result = []
    for amount, category, fin_type, date in rows:
        sign = "-" if fin_type == "расход" else "+"
        result.append(f"- {category}: {sign}{amount} ({date.strftime('%d.%m')})")
    expense = totals.get("расход", 0)
    income = totals.get("доход", 0)
    result.append(f"\nИтого в этом месяце: расходы {expense}, доходы {income}")
    return "\n".join(result)


def db_save_preference(key, value):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO preferences (key, value) VALUES (%s, %s)", (key, value))


def db_get_preferences():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT key, value FROM preferences ORDER BY created_at DESC")
        rows = c.fetchall()
    if not rows:
        return ""
    return "\n".join(f"- {key}: {value}" for key, value in rows)


def db_create_reminder(text, remind_at):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO reminders (text, remind_at) VALUES (%s, %s)", (text, remind_at))


def db_get_pending_reminders():
    with db_conn() as conn:
        c = conn.cursor()
        now = now_msk().strftime("%Y-%m-%d %H:%M")
        c.execute("SELECT id, text FROM reminders WHERE sent=0 AND remind_at <= %s", (now,))
        return c.fetchall()


def db_mark_reminder_sent(reminder_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE reminders SET sent=1 WHERE id=%s", (reminder_id,))


def db_save_message(role, content):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO conversation_history (role, content) VALUES (%s, %s)", (role, content))


def db_get_recent_history(limit=10):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT %s", (limit,))
        rows = c.fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def db_clear_history():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM conversation_history")


def needs_context(message):
    keywords = ["задач", "список", "что у нас", "покажи", "напомни", "дедлайн",
                "договор", "решени", "финанс", "потратил", "расход", "брифинг", "план"]
    return any(k in message.lower() for k in keywords)


def get_db_context():
    tasks = db_get_tasks()
    decisions = db_get_decisions()
    return f"Задачи:\n{tasks}\n\nДоговорённости:\n{decisions}"


def build_system(context_str=""):
    prefs = db_get_preferences()
    prefs_block = f"\n\nЗапомненные предпочтения сэра:\n{prefs}" if prefs else ""
    current_datetime = now_msk().strftime("%d.%m.%Y %H:%M")
    staff_lines = ", ".join(f"{name} ({role})" for name, role in STAFF.items())
    return f"""Ты FRIDAY — исполнительный ассистент Юсефа, предпринимателя (отели, апартаменты, общепит, крипто).
Сотрудники: {staff_lines}.
При делегировании — создавай задачу с пометкой исполнителя и задачу контроля для Юсефа.

Обращайся к нему "сэр". Говори как доверенный советник — прямо, коротко, без воды. Всегда подтверждай что зафиксировал.
Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.
Когда создаёшь задачу с дедлайном — всегда спрашивай: "Напомнить вам за день до дедлайна, сэр?" Если говорит да — ставь напоминание автоматически на 08:00 за день до дедлайна.
Если при закрытии или изменении задачи/договорённости находится несколько подходящих — переспроси сэра, какую именно он имеет в виду, не выбирай сам.
При записи финансов уточняй тип (расход или доход), если это не очевидно из контекста.

Стиль: простой текст, без символов # ** ---, максимум 3-4 предложения. Язык — тот на котором пишет Юсеф.

Дата: {current_datetime}{prefs_block}{context_str}"""


def process_message(user_message, system):
    tools = [
        {
            "name": "create_task",
            "description": "Создать задачу",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"}
                },
                "required": ["name"]
            }
        },
        {
            "name": "get_tasks",
            "description": "Получить список задач",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_task",
            "description": "Закрыть задачу как выполненную",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "update_task",
            "description": "Изменить дедлайн и/или приоритет существующей открытой задачи",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_part": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"}
                },
                "required": ["name_part"]
            }
        },
        {
            "name": "find_task",
            "description": "Найти задачу по части названия, включая уже закрытые. Используй чтобы проверить статус или историю задачи",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "create_decision",
            "description": "Записать договорённость",
            "input_schema": {
                "type": "object",
                "properties": {
                    "with_whom": {"type": "string"},
                    "what_decided": {"type": "string"},
                    "deadline": {"type": "string"},
                    "next_step": {"type": "string"}
                },
                "required": ["with_whom", "what_decided"]
            }
        },
        {
            "name": "get_decisions",
            "description": "Получить список открытых договорённостей",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_decision",
            "description": "Отметить договорённость выполненной",
            "input_schema": {
                "type": "object",
                "properties": {"text_part": {"type": "string", "description": "Часть текста договорённости или имени, с кем договорились"}},
                "required": ["text_part"]
            }
        },
        {
            "name": "create_finance",
            "description": "Записать расход или доход",
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Сумма, положительное число"},
                    "category": {"type": "string"},
                    "type": {"type": "string", "enum": ["расход", "доход"], "description": "расход или доход"},
                    "comment": {"type": "string"}
                },
                "required": ["amount", "category", "type"]
            }
        },
        {
            "name": "get_finance",
            "description": "Получить финансовые записи и итоги за месяц",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "save_preference",
            "description": "Запомнить предпочтение или важную информацию",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"}
                },
                "required": ["key", "value"]
            }
        },
        {
            "name": "create_reminder",
            "description": "Создать напоминание на конкретное время. Используй когда говорят 'напомни через X' или 'напомни в X'",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст напоминания"},
                    "remind_at": {"type": "string", "description": "Время в формате YYYY-MM-DD HH:MM"}
                },
                "required": ["text", "remind_at"]
            }
        }
    ]

    messages = conversation_history
    text = ""
    for _ in range(5):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=messages,
            tools=tools
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            break

        tool_results = []
        for block in tool_uses:
            inp = block.input
            result = ""
            if block.name == "create_task":
                db_create_task(inp["name"], inp.get("deadline"), inp.get("priority"))
                result = f"Задача создана: {inp['name']}"
            elif block.name == "get_tasks":
                result = db_get_tasks()
            elif block.name == "close_task":
                status, items = db_close_task(inp["name_part"])
                if status == "closed":
                    result = f"Задача закрыта: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих задач, уточни у сэра какую закрыть:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Задача не найдена"
            elif block.name == "update_task":
                status, items = db_update_task(inp["name_part"], inp.get("deadline"), inp.get("priority"))
                if status == "updated":
                    result = f"Задача обновлена: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих задач, уточни у сэра какую менять:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Задача не найдена"
            elif block.name == "find_task":
                result = db_find_task(inp["name_part"])
            elif block.name == "create_decision":
                db_create_decision(inp["with_whom"], inp["what_decided"], inp.get("deadline"), inp.get("next_step"))
                result = f"Записано: {inp['with_whom']} — {inp['what_decided']}"
            elif block.name == "get_decisions":
                result = db_get_decisions()
            elif block.name == "close_decision":
                status, items = db_close_decision(inp["text_part"])
                if status == "closed":
                    result = f"Договорённость закрыта: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих договорённостей, уточни у сэра какую закрыть:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Договорённость не найдена"
            elif block.name == "create_finance":
                db_create_finance(inp["amount"], inp["category"], inp.get("type", "расход"), inp.get("comment"))
                sign = "-" if inp.get("type", "расход") == "расход" else "+"
                result = f"Записано: {inp['category']} {sign}{inp['amount']}"
            elif block.name == "get_finance":
                result = db_get_finance()
            elif block.name == "save_preference":
                db_save_preference(inp["key"], inp["value"])
                result = f"Запомнено: {inp['key']}"
            elif block.name == "create_reminder":
                db_create_reminder(inp["text"], inp["remind_at"])
                result = f"Напоминание установлено на {inp['remind_at']}"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]

    return text or "Готово, сэр."


async def send_morning_briefing(bot: Bot):
    tasks = db_get_tasks()
    urgent = db_get_urgent_tasks()
    urgent_block = f"\nСрочные (дедлайн сегодня/завтра):\n{urgent}" if urgent else ""
    text = f"Доброе утро, сэр. Вот что на сегодня:\n\n{tasks}{urgent_block}\n\nГотов к работе."
    await bot.send_message(chat_id=ALLOWED_USER_ID, text=text)


async def send_evening_briefing(bot: Bot):
    tasks = db_get_tasks()
    text = f"Сэр, вечерний разбор. Открытые задачи:\n\n{tasks}\n\nЧто закрыли сегодня? Что переносим?"
    await bot.send_message(chat_id=ALLOWED_USER_ID, text=text)


async def scheduler(bot: Bot):
    while True:
        now = now_msk()

        if now.hour == 8 and now.minute == 0:
            try:
                await send_morning_briefing(bot)
            except Exception as e:
                logger.error(f"Morning briefing error: {e}")

        if now.hour == 21 and now.minute == 0:
            try:
                await send_evening_briefing(bot)
            except Exception as e:
                logger.error(f"Evening briefing error: {e}")

        try:
            reminders = db_get_pending_reminders()
        except Exception as e:
            logger.error(f"Reminders fetch error: {e}")
            reminders = []

        for reminder_id, text in reminders:
            try:
                await bot.send_message(chat_id=ALLOWED_USER_ID, text=f"Напоминание, сэр: {text}")
                db_mark_reminder_sent(reminder_id)
            except Exception as e:
                logger.error(f"Reminder error: {e}")

        await asyncio.sleep(60)


async def get_staff(request):
    return web.json_response(list(STAFF.keys()))


async def health(request):
    return web.json_response({"status": "ok"})


async def run_webapp_server():
    app = web.Application()
    app.router.add_get("/form", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "form.html")))
    app.router.add_get("/finance", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "finance.html")))
    app.router.add_get("/decisions", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "decisions.html")))
    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "form.html")))
    app.router.add_get("/api/staff", get_staff)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер форм запущен на порту {PORT}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("FRIDAY на связи, сэр. Готов к работе.", reply_markup=MAIN_KEYBOARD)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global conversation_history
    conversation_history = []
    db_clear_history()
    await update.message.reply_text("История очищена, сэр.")


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(db_get_tasks())


async def decisions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(db_get_decisions())


async def finance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(db_get_finance())


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    prefs = db_get_preferences()
    if prefs:
        await update.message.reply_text(f"Что я о вас знаю, сэр:\n{prefs}")
    else:
        await update.message.reply_text("Пока ничего не запомнено, сэр.")


def create_quick_task(name, deadline=None, priority=None, assignee=None):
    if assignee:
        name = f"{name} [{assignee}]"
    db_create_task(name, deadline, priority)
    summary = f"Записал, сэр: {name}"
    if deadline:
        summary += f" (дедлайн: {deadline})"
    if priority:
        summary += f" [{priority}]"
    return summary


def create_quick_finance(amount, category, fin_type="расход", comment=None):
    db_create_finance(amount, category, fin_type, comment)
    sign = "-" if fin_type == "расход" else "+"
    summary = f"Записал, сэр: {category} {sign}{amount}"
    if comment:
        summary += f" ({comment})"
    return summary


def create_quick_decision(with_whom, what_decided, deadline=None, next_step=None):
    db_create_decision(with_whom, what_decided, deadline, next_step)
    summary = f"Записал, сэр: {with_whom} — {what_decided}"
    if deadline:
        summary += f" (до {deadline})"
    if next_step:
        summary += f". Следующий шаг: {next_step}"
    return summary


def resolve_deadline(value):
    if value == "today":
        return now_msk().strftime("%Y-%m-%d")
    if value == "tomorrow":
        return (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    return value or None


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except (TypeError, ValueError, AttributeError):
        await update.message.reply_text("Не понял форму, сэр. Попробуйте ещё раз.", reply_markup=MAIN_KEYBOARD)
        return

    form = data.get("form", "task")

    if form == "finance":
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            await update.message.reply_text("Не понял сумму, сэр. Попробуйте ещё раз.", reply_markup=MAIN_KEYBOARD)
            return
        category = (data.get("category") or "").strip()
        if not category:
            await update.message.reply_text("Не указана категория, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        fin_type = "доход" if data.get("type") == "доход" else "расход"
        comment = (data.get("comment") or "").strip() or None
        summary = create_quick_finance(amount, category, fin_type, comment)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "decision":
        with_whom = (data.get("with_whom") or "").strip()
        what_decided = (data.get("what_decided") or "").strip()
        if not with_whom or not what_decided:
            await update.message.reply_text("Заполните, с кем и о чём договорились, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        deadline = resolve_deadline(data.get("deadline") or "")
        next_step = (data.get("next_step") or "").strip() or None
        summary = create_quick_decision(with_whom, what_decided, deadline, next_step)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # form == "task"
    name = (data.get("name") or "").strip()
    if not name:
        await update.message.reply_text("Пустое описание задачи, сэр.", reply_markup=MAIN_KEYBOARD)
        return

    deadline = resolve_deadline(data.get("deadline") or "")
    priority = (data.get("priority") or "").strip() or None
    assignee = (data.get("assignee") or "").strip() or None

    summary = create_quick_task(name, deadline, priority, assignee)
    await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    user_message = update.message.text

    # --- Быстрая задача (запасной пошаговый сценарий без WebApp) ---
    if user_message == QUICK_TASK_BUTTON:
        context.user_data["quick_task"] = {}
        context.user_data["quick_task_step"] = "name"
        await update.message.reply_text("Описание задачи, сэр?", reply_markup=ReplyKeyboardRemove())
        return

    quick_task_step = context.user_data.get("quick_task_step")

    if quick_task_step == "name":
        context.user_data["quick_task"]["name"] = user_message
        context.user_data["quick_task_step"] = "deadline"
        await update.message.reply_text("Дедлайн? Выберите или напишите дату вручную.", reply_markup=DEADLINE_KEYBOARD)
        return

    if quick_task_step == "deadline":
        if user_message == "Сегодня":
            deadline = now_msk().strftime("%Y-%m-%d")
        elif user_message == "Завтра":
            deadline = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif user_message == "Нет":
            deadline = None
        else:
            deadline = user_message
        context.user_data["quick_task"]["deadline"] = deadline
        context.user_data["quick_task_step"] = "priority"
        await update.message.reply_text("Приоритет? Выберите или напишите свой вариант.", reply_markup=PRIORITY_KEYBOARD)
        return

    if quick_task_step == "priority":
        priority = None if user_message == "Нет" else user_message
        context.user_data["quick_task"]["priority"] = priority
        context.user_data["quick_task_step"] = "assignee"
        await update.message.reply_text("Кто исполнитель? Выберите или напишите имя.", reply_markup=ASSIGNEE_KEYBOARD)
        return

    if quick_task_step == "assignee":
        assignee = None if user_message == "Нет" else user_message
        task = context.user_data["quick_task"]
        summary = create_quick_task(task["name"], task.get("deadline"), task.get("priority"), assignee)
        context.user_data["quick_task_step"] = None
        context.user_data["quick_task"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Быстрые финансы (запасной пошаговый сценарий) ---
    if user_message == QUICK_FINANCE_BUTTON:
        context.user_data["quick_finance"] = {}
        context.user_data["quick_finance_step"] = "type"
        await update.message.reply_text("Расход или доход, сэр?", reply_markup=FINANCE_TYPE_KEYBOARD)
        return

    quick_finance_step = context.user_data.get("quick_finance_step")

    if quick_finance_step == "type":
        fin_type = "доход" if user_message == "Доход" else "расход"
        context.user_data["quick_finance"]["type"] = fin_type
        context.user_data["quick_finance_step"] = "amount"
        await update.message.reply_text("Сумма?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_finance_step == "amount":
        try:
            amount = float(user_message.replace(",", ".").strip())
        except ValueError:
            await update.message.reply_text("Нужно число, сэр. Сколько?")
            return
        context.user_data["quick_finance"]["amount"] = amount
        context.user_data["quick_finance_step"] = "category"
        await update.message.reply_text("Категория?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_finance_step == "category":
        context.user_data["quick_finance"]["category"] = user_message
        context.user_data["quick_finance_step"] = "comment"
        await update.message.reply_text("Комментарий? Напишите или 'Нет'.", reply_markup=SKIP_KEYBOARD)
        return

    if quick_finance_step == "comment":
        comment = None if user_message == "Нет" else user_message
        fin = context.user_data["quick_finance"]
        summary = create_quick_finance(fin["amount"], fin["category"], fin["type"], comment)
        context.user_data["quick_finance_step"] = None
        context.user_data["quick_finance"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Быстрая договорённость (запасной пошаговый сценарий) ---
    if user_message == QUICK_DECISION_BUTTON:
        context.user_data["quick_decision"] = {}
        context.user_data["quick_decision_step"] = "with_whom"
        await update.message.reply_text("С кем договорились, сэр?", reply_markup=WITH_WHOM_KEYBOARD)
        return

    quick_decision_step = context.user_data.get("quick_decision_step")

    if quick_decision_step == "with_whom":
        context.user_data["quick_decision"]["with_whom"] = user_message
        context.user_data["quick_decision_step"] = "what_decided"
        await update.message.reply_text("О чём договорились?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_decision_step == "what_decided":
        context.user_data["quick_decision"]["what_decided"] = user_message
        context.user_data["quick_decision_step"] = "deadline"
        await update.message.reply_text("Дедлайн? Выберите или напишите дату вручную.", reply_markup=DEADLINE_KEYBOARD)
        return

    if quick_decision_step == "deadline":
        if user_message == "Сегодня":
            deadline = now_msk().strftime("%Y-%m-%d")
        elif user_message == "Завтра":
            deadline = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif user_message == "Нет":
            deadline = None
        else:
            deadline = user_message
        context.user_data["quick_decision"]["deadline"] = deadline
        context.user_data["quick_decision_step"] = "next_step"
        await update.message.reply_text("Следующий шаг? Напишите или 'Нет'.", reply_markup=SKIP_KEYBOARD)
        return

    if quick_decision_step == "next_step":
        next_step = None if user_message == "Нет" else user_message
        dec = context.user_data["quick_decision"]
        summary = create_quick_decision(dec["with_whom"], dec["what_decided"], dec.get("deadline"), next_step)
        context.user_data["quick_decision_step"] = None
        context.user_data["quick_decision"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Кнопки мониторинга ---
    if user_message == VIEW_TASKS_BUTTON:
        await update.message.reply_text(db_get_tasks())
        return

    if user_message == VIEW_DECISIONS_BUTTON:
        await update.message.reply_text(db_get_decisions())
        return

    if user_message == VIEW_FINANCE_BUTTON:
        await update.message.reply_text(db_get_finance())
        return

    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    context_str = f"\n\n{get_db_context()}" if needs_context(user_message) else ""
    system = build_system(context_str)

    try:
        db_save_message("user", user_message)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        assistant_message = process_message(user_message, system)
        conversation_history.append({"role": "assistant", "content": assistant_message})
        db_save_message("assistant", assistant_message)
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка, сэр. Попробуйте ещё раз.")


async def post_init(application: Application):
    asyncio.create_task(scheduler(application.bot))
    asyncio.create_task(run_webapp_server())


def main():
    init_db()
    global conversation_history
    conversation_history = db_get_recent_history()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("decisions", decisions_cmd))
    app.add_handler(CommandHandler("finance", finance_cmd))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
