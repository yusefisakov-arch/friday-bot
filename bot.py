"""Точка входа: радар недвижимости 999.md в Telegram.

Бот следит за объявлениями о продаже квартир и домов в Кишинёве и присылает
в группу всё, что не дороже заданной цены за квадратный метр. Каждый район —
своя тема; повторно объявление приходит только если цена на него упала.

Всё остальное (задачи, финансы, договорённости, почта, наставник) убрано —
осталась одна функция, которая делает одну вещь.
"""
import asyncio
import logging
import os

from telegram import Update, BotCommand
from telegram.ext import (Application, ApplicationHandlerStop, CommandHandler,
                          ContextTypes, CallbackQueryHandler, MessageHandler,
                          TypeHandler, filters)

from core import TELEGRAM_TOKEN, is_allowed
from radar999 import (
    radar_init_db, radar_loop, radar_here, radar_status,
    radar_check_cmd, radar_toggle_cmd, radar_top_cmd, radar_sync_filters,
    radar_topics_cmd, radar_dump_cmd, radar_redump_cmd,
    radar_bind_cmd, radar_unbind_cmd,
    ch_add_cmd, ch_list_cmd, ch_del_cmd, ch_check_cmd, ch_dump_cmd,
)
from crewbot import (
    crew_loop, crew_here_cmd, hq_here_cmd, crew_list_cmd, task_cmd,
    fix_cmd, fix_list_cmd, fix_del_cmd, today_cmd, debts_cmd, done_cmd,
    cancel_cmd, crew_button, catch_problem_note,
)
from crew import crew_init_db

logger = logging.getLogger(__name__)

RADAR_INTERVAL_MINUTES = int(os.environ.get("RADAR_INTERVAL_MINUTES", "15"))

HELP = (
    "*Радар 999.md*\n\n"
    "Слежу за продажей квартир и домов в Кишинёве и присылаю всё, что не "
    "дороже заданной цены за квадратный метр. Каждый район — своя тема.\n\n"
    "*Как запустить*\n"
    "1. Создайте группу, включите в ней темы\n"
    "2. Добавьте меня админом с правом управления темами\n"
    "3. В группе: /radar\\_here, затем /radar\\_topics и /radar\\_dump\n\n"
    "*Команды*\n"
    "/radar — что настроено и когда проверял\n"
    "/radar\\_top — лучшее из того, что висит на 999.md сейчас\n"
    "/radar\\_topics — завести темы по районам\n"
    "/bind Район — привязать тему, которую вы создали сами\n"
    "/radar\\_dump — разложить по темам все текущие варианты\n"
    "/radar\\_check — проверить прямо сейчас\n\n"
    "*Телеграм-каналы*\n"
    "/ch\\_add @канал — подключить публичный канал\n"
    "/ch\\_list — подключённые каналы\n"
    "/ch\\_dump — выгрузить из каналов всё подходящее\n\n"
    "*Задачи сотрудникам*\n"
    "/crew Имя — в группе человека: закрепить её за ним\n"
    "/hq — сделать эту группу Штабом\n"
    "/task Имя что сделать, до 18:00 — поставить задачу\n"
    "/fix Имя что делать, каждый день 9:00 — постоянное задание\n"
    "/today — что у всех на сегодня\n"
    "/debts — всё просроченное\n"
    "/crew\\_list — команда и статистика\n"
    "/radar\\_on N, /radar\\_off N — включить или приостановить\n"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP, parse_mode="Markdown")


async def post_init(application: Application):
    try:
        await application.bot.set_my_commands([
            BotCommand("radar", "🎯 Что настроено и когда проверял"),
            BotCommand("radar_top", "🔥 Лучшее из того, что висит сейчас"),
            BotCommand("radar_topics", "🗂 Завести темы по районам"),
            BotCommand("bind", "📌 Привязать эту тему к району"),
            BotCommand("radar_dump", "📦 Разложить всё текущее по темам"),
            BotCommand("radar_redump", "♻️ Забыть отправленное и разложить заново"),
            BotCommand("radar_check", "Проверить 999.md прямо сейчас"),
            BotCommand("ch_add", "📡 Подключить телеграм-канал"),
            BotCommand("ch_list", "📡 Подключённые каналы"),
            BotCommand("ch_dump", "📡 Выгрузить всё из каналов"),
            BotCommand("radar_here", "Настроить радар в этой группе"),
            BotCommand("task", "📋 Поставить задачу"),
            BotCommand("today", "📋 Что у всех на сегодня"),
            BotCommand("debts", "📋 Всё просроченное"),
            BotCommand("crew_list", "📋 Команда и статистика"),
            BotCommand("start", "Справка"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands пропущено: {e}")
    asyncio.create_task(radar_loop(application.bot, RADAR_INTERVAL_MINUTES))
    asyncio.create_task(crew_loop(application.bot))


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Единый вход. В личке бот отвечает только тем, кому дан доступ —
    остальным полная тишина (и никаких трат, когда появится AI). В группах
    пропускаем дальше: команды сами проверяют доступ, а сотрудники могут
    только жать кнопки задач и отвечать по «Проблеме» — ничего больше."""
    user = update.effective_user
    chat = update.effective_chat
    if chat and chat.type == "private" and not (user and is_allowed(user.id)):
        logger.info("Личка от постороннего %s — игнор",
                    user.id if user else "?")
        raise ApplicationHandlerStop


def main():
    radar_init_db()
    try:
        crew_init_db()
    except Exception as e:
        logger.warning(f"Задачи: таблицы не созданы — {e}")
    try:
        radar_sync_filters()
    except Exception as e:
        logger.warning(f"Радар: фильтры не синхронизированы — {e}")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    # Замок: срабатывает раньше всех и отсекает чужую личку.
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("radar", radar_status))
    app.add_handler(CommandHandler("radar_here", radar_here))
    app.add_handler(CommandHandler("radar_check", radar_check_cmd))
    app.add_handler(CommandHandler("radar_top", radar_top_cmd))
    app.add_handler(CommandHandler("radar_topics", radar_topics_cmd))
    app.add_handler(CommandHandler("bind", radar_bind_cmd))
    app.add_handler(CommandHandler("unbind", radar_unbind_cmd))
    app.add_handler(CommandHandler("radar_dump", radar_dump_cmd))
    app.add_handler(CommandHandler("radar_redump", radar_redump_cmd))
    app.add_handler(CommandHandler("ch_add", ch_add_cmd))
    app.add_handler(CommandHandler("ch_list", ch_list_cmd))
    app.add_handler(CommandHandler("ch_del", ch_del_cmd))
    app.add_handler(CommandHandler("ch_check", ch_check_cmd))
    app.add_handler(CommandHandler("ch_dump", ch_dump_cmd))
    app.add_handler(CommandHandler("radar_on", radar_toggle_cmd))
    app.add_handler(CommandHandler("radar_off", radar_toggle_cmd))

    # задачи сотрудникам
    app.add_handler(CommandHandler("crew", crew_here_cmd))
    app.add_handler(CommandHandler("hq", hq_here_cmd))
    app.add_handler(CommandHandler("crew_list", crew_list_cmd))
    app.add_handler(CommandHandler("task", task_cmd))
    app.add_handler(CommandHandler("fix", fix_cmd))
    app.add_handler(CommandHandler("fix_list", fix_list_cmd))
    app.add_handler(CommandHandler("fix_del", fix_del_cmd))
    app.add_handler(CommandHandler(["today", "сегодня"], today_cmd))
    app.add_handler(CommandHandler(["debts", "долги"], debts_cmd))
    app.add_handler(CommandHandler("done", done_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(crew_button, pattern=r"^crew:"))
    # последним: ловит пояснение к «Проблеме», всё остальное пропускает мимо
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,
                                   catch_problem_note))
    logger.info(f"Радар запущен, интервал проверки {RADAR_INTERVAL_MINUTES} мин")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
