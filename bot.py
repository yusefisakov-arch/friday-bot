import os
import logging
import sqlite3
from datetime import datetime
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

DB_PATH = "/app/friday.db"
conversation_history = []


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        deadline TEXT,
        priority TEXT,
        status TEXT DEFAULT 'Открыта',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        with_whom TEXT NOT NULL,
        what_decided TEXT NOT NULL,
        deadline TEXT,
        next_step TEXT,
        status TEXT DEFAULT 'Открыта',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS finance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount TEXT NOT NULL,
        category TEXT NOT NULL,
        comment TEXT,
        date TEXT DEFAULT CURRENT_DATE
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS preferences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()


def db_create_task(name, deadline=None, priority=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (name, deadline, priority) VALUES (?, ?, ?)", (name, deadline, priority))
    conn.commit()
    conn.close()


def db_get_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, deadline, priority FROM tasks WHERE status != 'Готово' ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
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


def db_close_task(name_part):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status='Готово' WHERE name LIKE ? AND status != 'Готово'", (f"%{name_part}%",))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def db_create_decision(with_whom, what_decided, deadline=None, next_step=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO decisions (with_whom, what_decided, deadline, next_step) VALUES (?, ?, ?, ?)",
              (with_whom, what_decided, deadline, next_step))
    conn.commit()
    conn.close()


def db_get_decisions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT with_whom, what_decided, deadline FROM decisions WHERE status='Открыта' ORDER BY created_at DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Открытых договорённостей нет."
    result = []
    for with_whom, what_decided, deadline in rows:
        line = f"- {with_whom}: {what_decided}"
        if deadline:
            line += f" (до {deadline})"
        result.append(line)
    return "\n".join(result)


def db_create_finance(amount, category, comment=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO finance (amount, category, comment) VALUES (?, ?, ?)", (amount, category, comment))
    conn.commit()
    conn.close()


def db_get_finance():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT amount, category, date FROM finance ORDER BY date DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Финансовых записей нет."
    return "\n".join(f"- {cat}: {amt} ({date})" for amt, cat, date in rows)


def db_save_preference(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO preferences (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def db_get_preferences():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT key, value FROM preferences ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return ""
    return "\n".join(f"- {key}: {value}" for key, value in rows)


def db_clear_preference(key_part):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM preferences WHERE key LIKE ? OR value LIKE ?", (f"%{key_part}%", f"%{key_part}%"))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


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
    current_datetime = datetime.now().strftime("%d.%m.%Y %H:%M")
    return f"""Ты FRIDAY — исполнительный ассистент Юсефа, предпринимателя (отели, апартаменты, общепит, крипто).

Обращайся к нему "сэр". Говори как доверенный советник — прямо, коротко, без воды. Всегда подтверждай что зафиксировал.

Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.

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
            "name": "create_decision",
            "description": "Записать договорённость или решение",
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
            "name": "create_finance",
            "description": "Записать расход или доход",
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "string"},
                    "category": {"type": "string"},
                    "comment": {"type": "string"}
                },
                "required": ["amount", "category"]
            }
        },
        {
            "name": "get_finance",
            "description": "Получить финансовые записи",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "save_preference",
            "description": "Запомнить предпочтение или важную информацию о пользователе",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Краткое название предпочтения"},
                    "value": {"type": "string", "description": "Значение или описание"}
                },
                "required": ["key", "value"]
            }
        }
    ]

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=conversation_history,
        tools=tools
    )

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            inp = block.input
            result = ""
            if block.name == "create_task":
                db_create_task(inp["name"], inp.get("deadline"), inp.get("priority"))
                result = f"Задача создана: {inp['name']}"
            elif block.name == "get_tasks":
                result = db_get_tasks()
            elif block.name == "close_task":
                success = db_close_task(inp["name_part"])
                result = "Задача закрыта" if success else "Задача не найдена"
            elif block.name == "create_decision":
                db_create_decision(inp["with_whom"], inp["what_decided"], inp.get("deadline"), inp.get("next_step"))
                result = f"Записано: {inp['with_whom']} — {inp['what_decided']}"
            elif block.name == "create_finance":
                db_create_finance(inp["amount"], inp["category"], inp.get("comment"))
                result = f"Записано: {inp['category']} {inp['amount']}"
            elif block.name == "get_finance":
                result = db_get_finance()
            elif block.name == "save_preference":
                db_save_preference(inp["key"], inp["value"])
                result = f"Запомнено: {inp['key']}"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    if tool_results:
        msgs = conversation_history + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]
        final = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=msgs,
            tools=tools
        )
        return "".join(b.text for b in final.content if hasattr(b, "text"))

    return "".join(b.text for b in response.content if hasattr(b, "text"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("FRIDAY на связи, сэр. Готов к работе.")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    global conversation_history
    conversation_history = []
    await update.message.reply_text("История очищена, сэр.")


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(db_get_tasks())


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    prefs = db_get_preferences()
    if prefs:
        await update.message.reply_text(f"Что я о вас знаю, сэр:\n{prefs}")
    else:
        await update.message.reply_text("Пока ничего не запомнено, сэр.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    user_message = update.message.text
    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    context_str = f"\n\n{get_db_context()}" if needs_context(user_message) else ""
    system = build_system(context_str)

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        assistant_message = process_message(user_message, system)
        conversation_history.append({"role": "assistant", "content": assistant_message})
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка, сэр. Попробуйте ещё раз.")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
