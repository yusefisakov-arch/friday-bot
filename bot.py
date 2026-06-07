import os
import logging
import asyncio
from datetime import datetime, timedelta
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import httpx
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

NOTION_DBS = {
    "tasks": "377f8d3fa9dd80bbbc5dcb517b428339",
    "diary": "377f8d3fa9dd800b90facd4534bf7242",
    "finance": "377f8d3fa9dd80078babc5294417b3d4",
    "decisions": "377f8d3fa9dd807a9ff7f91cc2c10bba"
}

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

conversation_history = []

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

РАБОТА С NOTION:
У тебя есть доступ к 4 базам данных в Notion. Используй инструменты для:
- Создания задач когда Юсеф говорит "добавь задачу", "напомни", "нужно сделать"
- Создания решений когда говорит "договорились", "решили", "обещал"
- Записи финансов когда говорит "потратил", "заплатил", "расход"
- Чтения задач для брифинга и планирования

СТИЛЬ ОТВЕТОВ:
Коротко. Без заголовков и markdown символов. Простой текст. На русском если Юсеф пишет по-русски. Максимум 3-4 предложения если не просят подробнее.

ЧЕГО НЕ ДЕЛАЕШЬ:
Не даёшь советов вне своей зоны. Не объясняешь зачем что-то нужно. Не добавляешь воду. Не используешь символы # ** --- в тексте.

Текущая дата и время: {datetime}

{notion_context}"""


async def notion_get_tasks():
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"https://api.notion.com/v1/databases/{NOTION_DBS['tasks']}/query",
                headers=NOTION_HEADERS,
                json={"filter": {"property": "Стату", "status": {"does_not_equal": "Готово"}}}
            )
            data = r.json()
            tasks = []
            for page in data.get("results", []):
                props = page.get("properties", {})
                name = ""
                for key in ["Задачи", "Name", "Название"]:
                    if key in props and props[key].get("title"):
                        name = props[key]["title"][0]["plain_text"] if props[key]["title"] else ""
                        break
                deadline = ""
                for key in ["Дедлайн", "Deadline", "Дата"]:
                    if key in props and props[key].get("date"):
                        deadline = props[key]["date"].get("start", "")
                        break
                priority = ""
                for key in ["Приоритет", "Priority"]:
                    if key in props and props[key].get("select"):
                        priority = props[key]["select"].get("name", "")
                        break
                if name:
                    tasks.append(f"- {name}" + (f" (дедлайн: {deadline})" if deadline else "") + (f" [{priority}]" if priority else ""))
            return "\n".join(tasks) if tasks else "Открытых задач нет"
        except Exception as e:
            logger.error(f"Notion tasks error: {e}")
            return "Не удалось загрузить задачи"


async def notion_create_task(name, deadline=None, priority=None):
    props = {
        "Задачи": {"title": [{"text": {"content": name}}]}
    }
    if deadline:
        props["Дедлайн"] = {"date": {"start": deadline}}
    if priority:
        props["Приоритет"] = {"select": {"name": priority}}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={"parent": {"database_id": NOTION_DBS["tasks"]}, "properties": props}
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Notion create task error: {e}")
            return False


async def notion_create_decision(with_whom, what_decided, deadline=None, next_step=None):
    props = {
        "Решения": {"title": [{"text": {"content": f"{with_whom}: {what_decided}"}}]}
    }
    if deadline:
        props["Дедлайн"] = {"date": {"start": deadline}}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={"parent": {"database_id": NOTION_DBS["decisions"]}, "properties": props}
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Notion create decision error: {e}")
            return False


async def notion_create_finance(amount, category, comment=None):
    props = {
        "Финансы": {"title": [{"text": {"content": f"{category}: {amount}"}}]},
        "Дата": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}}
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                "https://api.notion.com/v1/pages",
                headers=NOTION_HEADERS,
                json={"parent": {"database_id": NOTION_DBS["finance"]}, "properties": props}
            )
            return r.status_code == 200
        except Exception as e:
            logger.error(f"Notion create finance error: {e}")
            return False


async def get_notion_context():
    tasks = await notion_get_tasks()
    return f"Текущие открытые задачи в Notion:\n{tasks}"


async def process_message_with_tools(user_message, system):
    tools = [
        {
            "name": "create_task",
            "description": "Создать задачу в Notion когда пользователь просит добавить задачу, напомнить или сделать что-то",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Название задачи"},
                    "deadline": {"type": "string", "description": "Дедлайн в формате YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Приоритет: Высокий, Средний, Низкий"}
                },
                "required": ["name"]
            }
        },
        {
            "name": "create_decision",
            "description": "Записать договорённость или решение когда пользователь говорит договорились, решили, обещал",
            "input_schema": {
                "type": "object",
                "properties": {
                    "with_whom": {"type": "string", "description": "С кем договорились"},
                    "what_decided": {"type": "string", "description": "Что решили"},
                    "deadline": {"type": "string", "description": "Дедлайн в формате YYYY-MM-DD"},
                    "next_step": {"type": "string", "description": "Следующий шаг"}
                },
                "required": ["with_whom", "what_decided"]
            }
        },
        {
            "name": "create_finance",
            "description": "Записать финансовую операцию когда пользователь говорит потратил, заплатил, расход",
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "string", "description": "Сумма"},
                    "category": {"type": "string", "description": "Категория расхода"},
                    "comment": {"type": "string", "description": "Комментарий"}
                },
                "required": ["amount", "category"]
            }
        },
        {
            "name": "get_tasks",
            "description": "Получить список открытых задач из Notion",
            "input_schema": {
                "type": "object",
                "properties": {}
            }
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
            tool_input = block.input
            result = ""

            if tool_name == "create_task":
                success = await notion_create_task(
                    tool_input["name"],
                    tool_input.get("deadline"),
                    tool_input.get("priority")
                )
                result = "Задача создана" if success else "Ошибка создания задачи"

            elif tool_name == "create_decision":
                success = await notion_create_decision(
                    tool_input["with_whom"],
                    tool_input["what_decided"],
                    tool_input.get("deadline"),
                    tool_input.get("next_step")
                )
                result = "Решение записано" if success else "Ошибка записи решения"

            elif tool_name == "create_finance":
                success = await notion_create_finance(
                    tool_input["amount"],
                    tool_input["category"],
                    tool_input.get("comment")
                )
                result = "Финансовая запись создана" if success else "Ошибка записи финансов"

            elif tool_name == "get_tasks":
                result = await notion_get_tasks()

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result
            })

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
    task_list = await notion_get_tasks()
    await update.message.reply_text(task_list)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return

    user_message = update.message.text
    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})

    if len(conversation_history) > 20:
        conversation_history = conversation_history[-20:]

    current_datetime = datetime.now().strftime("%A, %d %B %Y, %H:%M")
    notion_context = await get_notion_context()
    system = SYSTEM_PROMPT.format(datetime=current_datetime, notion_context=notion_context)

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        assistant_message = await process_message_with_tools(user_message, system)
        conversation_history.append({"role": "assistant", "content": assistant_message})
        await update.message.reply_text(assistant_message)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка. Попробуй ещё раз.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY с Notion запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
