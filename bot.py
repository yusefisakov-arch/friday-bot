"""Точка входа FRIDAY: телеграм-обработчики, диалоговая память, запуск сервисов."""
import asyncio
import base64
import logging
from telegram import Update, Bot, ReplyKeyboardRemove
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from core import *
from db import *
from ai import build_system, process_message, generate_mentor_briefing
from webapp import (
    run_webapp_server, handle_webapp_data, normalize_deadline,
    create_quick_task, create_quick_finance, create_quick_decision,
)
from scheduler import scheduler

logger = logging.getLogger(__name__)

conversation_history = []


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
    await reply_md(update.message, db_get_tasks())


async def decisions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await reply_md(update.message, db_get_decisions())


async def finance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await reply_md(update.message, db_get_finance())


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    prefs = db_get_preferences()
    if prefs:
        await update.message.reply_text(f"Что я о вас знаю, сэр:\n{prefs}")
    else:
        await update.message.reply_text("Пока ничего не запомнено, сэр.")


async def mentor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        text = await asyncio.to_thread(generate_mentor_briefing)
    except Exception as e:
        logger.error(f"Mentor on-demand error: {e}")
        text = "Не удалось собрать разбор, сэр. Попробуйте чуть позже."
    await reply_md(update.message, text, reply_markup=MAIN_KEYBOARD)


async def selfdestruct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    context.user_data["awaiting_selfdestruct"] = True
    await update.message.reply_text(
        "⚠️ ВНИМАНИЕ, сэр.\n\n"
        "Это БЕЗВОЗВРАТНО сотрёт ВСЕ данные: задачи, цели, финансы, договорённости, "
        "кассу квартир, справочник квартир, счётчики, историю — всё до нуля. "
        "Восстановить будет нельзя.\n\n"
        f"Если действительно уверены — отправьте СЛЕДУЮЩИМ сообщением ровно эту фразу:\n\n{SELF_DESTRUCT_PHRASE}\n\n"
        "Любое другое сообщение отменит операцию."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    user_message = update.message.text

    # --- Подтверждение самоуничтожения (двойная защита: команда + точная фраза) ---
    if context.user_data.get("awaiting_selfdestruct"):
        context.user_data["awaiting_selfdestruct"] = False
        global conversation_history
        if (user_message or "").strip() == SELF_DESTRUCT_PHRASE:
            db_self_destruct()
            conversation_history = []
            await update.message.reply_text(
                "Готово, сэр. Все данные стёрты безвозвратно. Чистый лист.",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text("Отменено, сэр. Ничего не тронуто.", reply_markup=MAIN_KEYBOARD)
        return

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
        deadline = normalize_deadline(user_message)
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
        deadline = normalize_deadline(user_message)
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

    # --- Квартиры без мини-формы (если WEBAPP_URL не настроен) ---
    if user_message == APARTMENT_BUTTON:
        await update.message.reply_text(
            "Опишите операцию текстом, сэр, например: \"запиши приход 700 лей аренды по Лев Толстой от квартиранта\". "
            "Я разберусь сам."
        )
        return

    # --- Кнопки мониторинга ---
    if user_message == VIEW_TASKS_BUTTON:
        await reply_md(update.message, db_get_tasks())
        return

    if user_message == VIEW_DECISIONS_BUTTON:
        await reply_md(update.message, db_get_decisions())
        return

    if user_message == VIEW_FINANCE_BUTTON:
        await reply_md(update.message, db_get_finance())
        return

    if user_message == VIEW_APARTMENT_BALANCE_BUTTON:
        await reply_md(update.message, db_get_apartment_balance())
        return

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > HISTORY_WINDOW:
        conversation_history = conversation_history[-HISTORY_WINDOW:]

    context_str = f"\n\n{get_db_context()}" if needs_context(user_message) else ""
    system = build_system(context_str)

    try:
        db_save_message("user", user_message)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        # Тяжёлый синхронный вызов ИИ — в отдельном потоке, чтобы не блокировать
        # напоминания, веб-формы и другие сообщения.
        assistant_message, chart_path = await asyncio.to_thread(
            process_message, list(conversation_history), system
        )
        conversation_history.append({"role": "assistant", "content": assistant_message})
        db_save_message("assistant", assistant_message)
        await reply_md(update.message, assistant_message)
        if chart_path:
            try:
                with open(chart_path, "rb") as f:
                    await update.message.reply_photo(photo=f)
            finally:
                os.remove(chart_path)
    except Exception as e:
        logger.error(f"Error: {e}")
        if conversation_history and conversation_history[-1] == {"role": "user", "content": user_message}:
            conversation_history.pop()
        await update.message.reply_text("Произошла ошибка, сэр. Попробуйте ещё раз.")


async def post_init(application: Application):
    asyncio.create_task(scheduler(application.bot))
    asyncio.create_task(run_webapp_server())


def main():
    init_db()
    try:
        db_prune_history()
    except Exception as e:
        logger.warning(f"Чистка истории при старте пропущена: {e}")
    global conversation_history
    conversation_history = db_get_recent_history()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("decisions", decisions_cmd))
    app.add_handler(CommandHandler("finance", finance_cmd))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("mentor", mentor))
    app.add_handler(CommandHandler("selfdestruct", selfdestruct))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
