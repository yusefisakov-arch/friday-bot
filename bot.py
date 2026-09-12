"""Точка входа: радар недвижимости 999.md в Telegram.

Бот следит за объявлениями о продаже квартир и домов в Кишинёве, считает
медиану €/м² по сопоставимому жилью и присылает в темы группы только то,
что заметно дешевле своей группы.

Всё остальное (задачи, финансы, договорённости, почта, наставник) убрано —
осталась одна функция, которая делает одну вещь.
"""
import asyncio
import logging
import os

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

from core import TELEGRAM_TOKEN, is_allowed
from radar999 import (
    radar_init_db, radar_loop, radar_here, radar_status,
    radar_check_cmd, radar_toggle_cmd, radar_top_cmd, radar_sync_filters,
)

logger = logging.getLogger(__name__)

RADAR_INTERVAL_MINUTES = int(os.environ.get("RADAR_INTERVAL_MINUTES", "15"))

HELP = (
    "*Радар 999.md*\n\n"
    "Слежу за продажей квартир и домов в Кишинёве и присылаю то, что дешевле "
    "сопоставимых предложений в том же районе.\n\n"
    "*Как запустить*\n"
    "1. Создайте группу, включите в ней темы\n"
    "2. Добавьте меня админом с правом управления темами\n"
    "3. Отправьте в группе /radar\\_here — заведу темы и начну следить\n\n"
    "*Команды*\n"
    "/radar — что настроено и когда проверял\n"
    "/radar\\_top — лучшее из того, что висит на 999.md сейчас\n"
    "/radar\\_check — проверить прямо сейчас\n"
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
            BotCommand("radar_check", "Проверить 999.md прямо сейчас"),
            BotCommand("radar_here", "Настроить радар в этой группе"),
            BotCommand("start", "Справка"),
        ])
    except Exception as e:
        logger.warning(f"set_my_commands пропущено: {e}")
    asyncio.create_task(radar_loop(application.bot, RADAR_INTERVAL_MINUTES))


def main():
    radar_init_db()
    try:
        radar_sync_filters()
    except Exception as e:
        logger.warning(f"Радар: фильтры не синхронизированы — {e}")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("radar", radar_status))
    app.add_handler(CommandHandler("radar_here", radar_here))
    app.add_handler(CommandHandler("radar_check", radar_check_cmd))
    app.add_handler(CommandHandler("radar_top", radar_top_cmd))
    app.add_handler(CommandHandler("radar_on", radar_toggle_cmd))
    app.add_handler(CommandHandler("radar_off", radar_toggle_cmd))
    logger.info(f"Радар запущен, интервал проверки {RADAR_INTERVAL_MINUTES} мин")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
