import os
import logging
import psycopg2
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
from anthropic import Anthropic
from telegram import Update, Bot, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

DATABASE_URL = os.environ["DATABASE_URL"]
MSK = ZoneInfo("Europe/Moscow")
conversation_history = []

QUICK_TASK_BUTTON = "📝 Задача"
MAIN_KEYBOARD = ReplyKeyboardMarkup([[QUICK_TASK_BUTTON]], resize_keyboard=True)
DEADLINE_KEYBOARD = ReplyKeyboardMarkup([["Сегодня", "Завтра"], ["Без срока"]], resize_keyboard=True)
PRIORITY_KEYBOARD = ReplyKeyboardMarkup([["Высокий", "Средний", "Низкий"], ["Без приоритета"]], resize_keyboard=True)


def now_msk():
    return datetime.now(MSK)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
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
        amount TEXT NOT NULL,
        category TEXT NOT NULL,
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
    conn.close()


def db_create_task(name, deadline=None, priority=None):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (name, deadline, priority) VALUES (%s, %s, %s)", (name, deadline, priority))
    conn.commit()
    conn.close()


def db_get_tasks():
    conn = psycopg2.connect(DATABASE_URL)
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


def db_get_urgent_tasks():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    today = now_msk().strftime("%Y-%m-%d")
    c.execute("""SELECT name, deadline, priority FROM tasks
                 WHERE status != 'Готово' AND deadline IS NOT NULL
                 AND deadline <= %s ORDER BY deadline ASC LIMIT 5""", (tomorrow,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return ""
    result = []
    for name, deadline, priority in rows:
        result.append(f"- {name} (дедлайн: {deadline})")
    return "\n".join(result)


def db_close_task(name_part):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("UPDATE tasks SET status='Готово' WHERE name LIKE %s AND status != 'Готово'", (f"%{name_part}%",))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return affected > 0


def db_find_task(name_part):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("""SELECT name, deadline, priority, status, created_at FROM tasks
                 WHERE name LIKE %s ORDER BY created_at DESC LIMIT 5""", (f"%{name_part}%",))
    rows = c.fetchall()
    conn.close()
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
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO decisions (with_whom, what_decided, deadline, next_step) VALUES (%s, %s, %s, %s)",
              (with_whom, what_decided, deadline, next_step))
    conn.commit()
    conn.close()


def db_get_decisions():
    conn = psycopg2.connect(DATABASE_URL)
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
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO finance (amount, category, comment) VALUES (%s, %s, %s)", (amount, category, comment))
    conn.commit()
    conn.close()


def db_get_finance():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("SELECT amount, category, date FROM finance ORDER BY date DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Финансовых записей нет."
    return "\n".join(f"- {cat}: {amt} ({date})" for amt, cat, date in rows)


def db_save_preference(key, value):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO preferences (key, value) VALUES (%s, %s)", (key, value))
    conn.commit()
    conn.close()


def db_get_preferences():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("SELECT key, value FROM preferences ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return ""
    return "\n".join(f"- {key}: {value}" for key, value in rows)


def db_create_reminder(text, remind_at):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO reminders (text, remind_at) VALUES (%s, %s)", (text, remind_at))
    conn.commit()
    conn.close()


def db_get_pending_reminders():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    now = now_msk().strftime("%Y-%m-%d %H:%M")
    c.execute("SELECT id, text FROM reminders WHERE sent=0 AND remind_at <= %s", (now,))
    rows = c.fetchall()
    conn.close()
    return rows


def db_mark_reminder_sent(reminder_id):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("UPDATE reminders SET sent=1 WHERE id=%s", (reminder_id,))
    conn.commit()
    conn.close()


def db_save_message(role, content):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("INSERT INTO conversation_history (role, content) VALUES (%s, %s)", (role, content))
    conn.commit()
    conn.close()


def db_get_recent_history(limit=10):
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT %s", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def db_clear_history():
    conn = psycopg2.connect(DATABASE_URL)
    c = conn.cursor()
    c.execute("DELETE FROM conversation_history")
    conn.commit()
    conn.close()


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
    return f"""Ты FRIDAY — исполнительный ассистент Юсефа, предпринимателя (отели, апартаменты, общепит, крипто).
Сотрудники: Жанель (финансы, администрация), Эдик (исполнитель, руками), Филадельфия (всё остальное).
При делегировании — создавай задачу с пометкой исполнителя и задачу контроля для Юсефа.

Обращайся к нему "сэр". Говори как доверенный советник — прямо, коротко, без воды. Всегда подтверждай что зафиксировал.
Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.
Когда создаёшь задачу с дедлайном — всегда спрашивай: "Напомнить вам за день до дедлайна, сэр?" Если говорит да — ставь напоминание автоматически на 08:00 за день до дедлайна.

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
                success = db_close_task(inp["name_part"])
                result = "Задача закрыта" if success else "Задача не найдена"
            elif block.name == "find_task":
                result = db_find_task(inp["name_part"])
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

        reminders = db_get_pending_reminders()
        for reminder_id, text in reminders:
            try:
                await bot.send_message(chat_id=ALLOWED_USER_ID, text=f"Напоминание, сэр: {text}")
                db_mark_reminder_sent(reminder_id)
            except Exception as e:
                logger.error(f"Reminder error: {e}")

        await asyncio.sleep(60)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("FRIDAY на связи, сэр. Готов к работе.", reply_markup=MAIN_KEYBOARD)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    global conversation_history
    conversation_history = []
    db_clear_history()
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

    if user_message == QUICK_TASK_BUTTON:
        context.user_data["quick_task"] = {}
        context.user_data["quick_task_step"] = "name"
        await update.message.reply_text("Что за задача, сэр? Напишите одной строкой.")
        return

    quick_task_step = context.user_data.get("quick_task_step")

    if quick_task_step == "name":
        context.user_data["quick_task"]["name"] = user_message
        context.user_data["quick_task_step"] = "deadline"
        await update.message.reply_text("Дедлайн? (выберите или напишите дату)", reply_markup=DEADLINE_KEYBOARD)
        return

    if quick_task_step == "deadline":
        if user_message == "Сегодня":
            deadline = now_msk().strftime("%Y-%m-%d")
        elif user_message == "Завтра":
            deadline = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif user_message == "Без срока":
            deadline = None
        else:
            deadline = user_message
        context.user_data["quick_task"]["deadline"] = deadline
        context.user_data["quick_task_step"] = "priority"
        await update.message.reply_text("Приоритет?", reply_markup=PRIORITY_KEYBOARD)
        return

    if quick_task_step == "priority":
        priority = None if user_message == "Без приоритета" else user_message
        task = context.user_data["quick_task"]
        db_create_task(task["name"], task.get("deadline"), priority)
        context.user_data["quick_task_step"] = None
        context.user_data["quick_task"] = {}
        summary = f"Записал, сэр: {task['name']}"
        if task.get("deadline"):
            summary += f" (дедлайн: {task['deadline']})"
        if priority:
            summary += f" [{priority}]"
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    db_save_message("user", user_message)
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    context_str = f"\n\n{get_db_context()}" if needs_context(user_message) else ""
    system = build_system(context_str)

    try:
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


def main():
    init_db()
    global conversation_history
    conversation_history = db_get_recent_history()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
