"""Точка входа FRIDAY: телеграм-обработчики, диалоговая память, запуск сервисов."""
import asyncio
import base64
import logging
import aiohttp
from telegram import Update, Bot, ReplyKeyboardRemove, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from core import *
from db import *
from ai import (
    build_system, process_message, generate_mentor_briefing,
    make_apartments_heatmap, make_apartments_table,
)
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
    await reply_md(update.message, db_get_today_tasks())


async def alltasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def send_apartments_image(update, context, kind):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    builder = make_apartments_heatmap if kind == "heat" else make_apartments_table
    path = await asyncio.to_thread(builder)
    if not path:
        await update.message.reply_text("Нет квартир для отображения, сэр.")
        return
    try:
        with open(path, "rb") as f:
            await update.message.reply_photo(photo=f)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def heatmap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await send_apartments_image(update, context, "heat")


async def table_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await send_apartments_image(update, context, "table")


async def appcmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not WEBAPP_URL:
        await update.message.reply_text("WEBAPP_URL не настроен, сэр.")
        return
    url = f"{WEBAPP_URL}/app?k={WEBAPP_SECRET}"
    await update.message.reply_text(
        "Ваш командный центр FRIDAY 🧠\n\n"
        f"{url}\n\n"
        "Как установить на телефон:\n"
        "1. Откройте ссылку → «Открыть в Safari/Chrome» (не во встроенном браузере).\n"
        "2. Меню браузера → «На экран Домой» / «Установить приложение».\n"
        "3. Если после установки попросит войти — нажмите «вставить ключ» и вставьте этот код:\n\n"
        f"{WEBAPP_SECRET}",
        disable_web_page_preview=True,
    )


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


async def hardmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    new = "off" if db_get_setting("hard_mode", "off") == "on" else "on"
    db_set_setting("hard_mode", new)
    if new == "on":
        await update.message.reply_text("Жёсткий режим ВКЛЮЧЁН, сэр. Буду давить и не давать соскользнуть.")
    else:
        await update.message.reply_text("Жёсткий режим выключен, сэр. Возвращаюсь к мягкому тону.")


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
        await reply_md(update.message, db_get_today_tasks())
        return

    if user_message.strip().lower() in ("все задачи", "всё задачи", "все задания", "полный список задач"):
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

    if user_message == MAP_BUTTON:
        await send_apartments_image(update, context, "heat")
        return

    if user_message.strip().lower() in ("таблица квартир", "таблица", "квартиры таблицей"):
        await send_apartments_image(update, context, "table")
        return

    # Триаж пересланных сообщений (разгрузка от переписки)
    sender = forward_sender(update.message)
    if sender:
        user_message = (
            f"Это ПЕРЕСЛАННОЕ сообщение от: {sender}.\n"
            f"Текст сообщения:\n{user_message}\n\n"
            "Сделай триаж в структурном виде: *От кого*, *Хотят* (суть), *Срок* (если есть), "
            "*Черновик ответа* (готовый текст, который сэр может скопировать), и спроси, создать ли задачу."
        )

    await run_text_turn(update, context, user_message)


def forward_sender(message):
    """Если сообщение переслано — вернуть имя источника, иначе None."""
    fo = getattr(message, "forward_origin", None)
    if not fo:
        return None
    su = getattr(fo, "sender_user", None)
    if su:
        return su.full_name or (f"@{su.username}" if su.username else "контакт")
    sun = getattr(fo, "sender_user_name", None)
    if sun:
        return sun
    sc = getattr(fo, "sender_chat", None)
    if sc:
        return getattr(sc, "title", None) or "чат"
    ch = getattr(fo, "chat", None)
    if ch:
        return getattr(ch, "title", None) or "канал"
    return "контакт"


async def run_text_turn(update: Update, context: ContextTypes.DEFAULT_TYPE, user_message: str):
    """Общий прогон сообщения через ИИ (используется и для текста, и для расшифрованного голоса)."""
    global conversation_history
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


async def transcribe_groq(audio_bytes):
    """Распознать речь через Groq (Whisper). Возвращает текст."""
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
    form.add_field("model", "whisper-large-v3-turbo")
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            data = await resp.json()
            return (data.get("text") or "").strip()


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not GROQ_API_KEY:
        await update.message.reply_text("Распознавание голоса пока не настроено, сэр.")
        return
    voice = update.message.voice or update.message.audio
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        tg_file = await voice.get_file()
        raw = await tg_file.download_as_bytearray()
        text = await transcribe_groq(bytes(raw))
    except Exception as e:
        logger.error(f"Voice transcription error: {e}")
        await update.message.reply_text("Не смог распознать голос, сэр. Попробуйте ещё раз.")
        return
    if not text:
        await update.message.reply_text("Пустая запись, сэр.")
        return
    await update.message.reply_text(f"🎤 Расслышал: {text}")
    await run_text_turn(update, context, text)


async def post_init(application: Application):
    # Меню команд (выпадает при вводе "/")
    try:
        await application.bot.set_my_commands([
            BotCommand("app", "🧠 Командный центр (приложение)"),
            BotCommand("mentor", "Разбор дня и движение к целям (наставник)"),
            BotCommand("tasks", "Задачи на сегодня и просроченные"),
            BotCommand("alltasks", "Полный список задач"),
            BotCommand("map", "Тепловая карта аренды (картинка)"),
            BotCommand("table", "Таблица квартир (картинка)"),
            BotCommand("finance", "Финансы и итоги за месяц"),
            BotCommand("decisions", "Открытые договорённости"),
            BotCommand("memory", "Что бот о вас запомнил"),
            BotCommand("hardmode", "Жёсткий режим наставника вкл/выкл"),
            BotCommand("clear", "Очистить историю разговора"),
            BotCommand("start", "Перезапуск и клавиатура"),
            BotCommand("selfdestruct", "⚠️ Стереть все данные безвозвратно"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands пропущено: {e}")
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
    app.add_handler(CommandHandler("alltasks", alltasks))
    app.add_handler(CommandHandler("map", heatmap_cmd))
    app.add_handler(CommandHandler("table", table_cmd))
    app.add_handler(CommandHandler("decisions", decisions_cmd))
    app.add_handler(CommandHandler("finance", finance_cmd))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(CommandHandler("app", appcmd))
    app.add_handler(CommandHandler("mentor", mentor))
    app.add_handler(CommandHandler("hardmode", hardmode))
    app.add_handler(CommandHandler("selfdestruct", selfdestruct))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
