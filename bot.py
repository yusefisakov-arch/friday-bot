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
    conn.commit()
    conn.close()


def db_create_task(name, deadline=None, priority=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO tasks (name, deadline, priority) VALUES (?, ?, ?)", (name, deadline, priority))
    conn.commit()
    conn.close()
    return True


def db_get_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, deadline, priority, status FROM tasks WHERE status != 'Готово' ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Открытых задач нет."
    result = []
    for name, deadline, priority, status in rows:
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
    return True


def db_get_decisions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT with_whom, what_decided, deadline, next_step FROM decisions WHERE status='Открыта' ORDER BY created_at DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Открытых договорённостей нет."
    result = []
    for with_whom, what_decided, deadline, next_step in rows:
        line = f"- {with_whom}: {what_decided}"
        if deadline:
            line += f" (дедлайн: {deadline})"
        result.append(line)
    return "\n".join(result)


def db_create_finance(amount, category, comment=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO finance (amount, category, comment) VALUES (?, ?, ?)", (amount, category, comment))
    conn.commit()
    conn.close()
    return True


def db_get_finance_summary():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT amount, category, date FROM finance ORDER BY date DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "Финансовых записей нет."
    result = []
    for amount, category, date in rows:
        result.append(f"- {category}: {amount} ({date})")
    return "\n".join(result)


def get_db_context():
    tasks = db_get_tasks()
    decisions = db_get_decisions()
    return f"Открытые задачи:\n{tasks}\n\nОткрытые договорённости:\n{decisions}"


SYSTEM_PROMPT = """Ты FRIDAY — персональный исполнительный ассистент генерального директора Юсефа.
Твоя единственная цель — помочь ему вернуть контроль над компанией, деньгами и временем и держать этот контроль.

О Юсефе:
- Предприниматель, управляет несколькими бизнесами: отели, апартаменты, апарт-отели, общепит, крипто
- Планирует расширение в новые сферы
- Нуждается в помощи как по операционке так и по стратегии
- Учится стратегическому мышлению

ХАРАКТЕР:
Прямой, жёсткий, без воды. Говоришь как доверенный советник а не как робот. Не хвалишь за обычные вещи. Не даёшь расслабиться если есть открытые проблемы. Никогда не говоришь "окей понял" и не исчезаешь — всегда подтверждаешь что зафиксировал.

ПРИОРИТЕТЫ:
1. Финансовые риски — перерасход, неоплаченные счета, сорванные сделки. Сигналишь немедленно.
2. Забытые договорённости с клиентами — если есть обещание и дедлайн, это топ.
3. Задачи которые зависли больше 3 дней без движения.
4. Хаос в приоритетах — помогаешь выбрать топ-3 на завтра.

ИНСТРУМЕНТЫ:
У тебя есть инструменты для управления задачами, договорённостями и финансами. Используй их когда нужно.

СТИЛЬ ОТВЕТОВ:
Коротко. Без заголовков и markdown символов. Простой текст. На русском если Юсеф пишет по-русски. Максимум 3-4 предложения если не просят подробнее.

ЧЕГО НЕ ДЕЛАЕШЬ:
Не даёшь советов вне своей зоны. Не объясняешь зачем что-то нужно. Не добавляешь воду. Не используешь символы # ** --- в тексте.

Текущая дата и время: {datetime}

{db_context}"""


def process_message_with_tools(user_message, system):
    tools = [
        {
            "name": "create_task",
            "description": "Создать задачу когда пользователь просит добавить задачу или напомнить",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название задачи"},
                    "deadline": {"type": "string", "description": "Дедлайн в формате YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний или Низкий"}
                },
                "required": ["name"]
            }
        },
        {
            "name": "get_tasks",
            "description": "Получить список открытых задач",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_task",
            "description": "Закрыть задачу как выполненную",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_part": {"type": "string", "description": "Часть названия задачи"}
                },
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
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "next_step": {"type": "string"}
                },
                "required": ["with_whom", "what_decided"]
            }
        },
        {
            "name": "create_finance",
            "description": "Записать финансовую операцию",
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
        }
    ]

    response = anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=conversation_history,
        tools=tools
    )

    tool_results = []
    for block in response.content:
        if block.type == "tool_use":
            tool_name = block.name
            inp = block.input
            result = ""

            if tool_name == "create_task":
                db_create_task(inp["name"], inp.get("deadline"), inp.get("priority"))
                result = f"Задача создана: {inp['name']}"
            elif tool_name == "get_tasks":
                result = db_get_tasks()
            elif tool_name == "close_task":
                success = db_close_task(inp["name_part"])
                result = "Задача закрыта" if success else "Задача не найдена"
            elif tool_name == "create_decision":
                db_create_decision(inp["with_whom"], inp["what_decided"], inp.get("deadline"), inp.get("next_step"))
                result = f"Решение записано: {inp['with_whom']} — {inp['what_decided']}"
            elif tool_name == "create_finance":
                db_create_finance(inp["amount"], inp["category"], inp.get("comment"))
                result = f"Записано: {inp['category']} {inp['amount']}"
            elif tool_name == "get_finance":
                result = db_get_finance_summary()

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

    if tool_results:
        messages_with_tools = conversation_history + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]
        final_response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=messages_with_tools,
            tools=tools
        )
        return "".join(b.text for b in final_response.content if hasattr(b, "text"))

    return "".join(b.text for b in response.content if hasattr(b, "text"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("FRIDAY на связи. Готов к работе.")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    global conversation_history
    conversation_history = []
    await update.message.reply_text("История очищена.")


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(db_get_tasks())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    user_message = update.message.text
    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 20:
        conversation_history = conversation_history[-20:]

    current_datetime = datetime.now().strftime("%A, %d %B %Y, %H:%M")
    db_context = get_db_context()
    system = SYSTEM_PROMPT.format(datetime=current_datetime, db_context=db_context)

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        assistant_message = process_message_with_tools(user_message, system)
        conversation_history.append({"role": "assistant", "content": assistant_message})
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
