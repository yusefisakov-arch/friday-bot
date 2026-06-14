"""Слой расписания: брифинги, напоминания, ежедневные задачи."""
import asyncio
import logging
from datetime import timedelta
from telegram import Bot

from core import *
from db import *

logger = logging.getLogger(__name__)


async def send_morning_briefing(bot: Bot):
    tasks = db_get_tasks()
    urgent = db_get_urgent_tasks()
    text = f"*Доброе утро, сэр*\n\n*Задачи на сегодня:*\n{tasks}"
    if urgent:
        text += f"\n\n*Срочное (дедлайн сегодня/завтра):*\n{urgent}"
    text += "\n\nГотов к работе."
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_evening_briefing(bot: Bot):
    tasks = db_get_tasks()
    text = f"*Вечерний разбор, сэр*\n\n*Открытые задачи:*\n{tasks}\n\nЧто закрыли сегодня? Что переносим?"
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_sop_reminders(bot: Bot):
    now = now_msk()
    current_month = now.strftime("%Y-%m")
    due = db_get_due_sop_reminders(now.day, current_month)
    if not due:
        return
    lines = "\n".join(f"- {text}" for _, text in due)
    await send_md(bot, ALLOWED_USER_ID, f"*Напоминания по SOP, сэр:*\n{lines}")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for reminder_id, text in due:
        db_mark_sop_reminder_sent(reminder_id, current_month)
        db_create_task_if_absent(text, deadline=tomorrow)


async def send_apartment_reminders(bot: Bot):
    now = now_msk()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    current_month = now.strftime("%Y-%m")
    for apt_id, address, rent_day, lease_end, end_sent, last_month in db_get_apartments_for_reminders():
        if rent_day and rent_day == tomorrow.day and last_month != current_month:
            await send_md(
                bot, ALLOWED_USER_ID,
                text=f"Сэр, завтра аренда по *{address}*. Попросите у квартиранта фото счётчиков (свет, газ, вода), сделайте просчёт и договоритесь о встрече."
            )
            db_set_collection_reminder_sent(apt_id, current_month)

        if lease_end and not end_sent and today <= lease_end <= today + timedelta(days=10):
            await send_md(
                bot, ALLOWED_USER_ID,
                text=f"Сэр, по адресу *{address}* контракт заканчивается *{lease_end.strftime('%d.%m.%Y')}*. Узнайте у квартиранта про продление или выезд."
            )
            db_set_lease_end_reminder_sent(apt_id)


async def scheduler(bot: Bot):
    while True:
        now = now_msk()
        today_str = now.strftime("%Y-%m-%d")

        # Утренний блок: один раз за день в окне 8:00–11:59 (переживает рестарты и задержки).
        if 8 <= now.hour <= 11 and db_claim_daily_job("morning", today_str):
            try:
                await send_morning_briefing(bot)
            except Exception as e:
                logger.error(f"Morning briefing error: {e}")
            try:
                await send_sop_reminders(bot)
            except Exception as e:
                logger.error(f"SOP reminders error: {e}")
            try:
                await send_apartment_reminders(bot)
            except Exception as e:
                logger.error(f"Apartment reminders error: {e}")
            try:
                db_prune_history()
            except Exception as e:
                logger.error(f"History prune error: {e}")

        # Вечерний блок: один раз за день в окне 21:00–23:59.
        if now.hour >= 21 and db_claim_daily_job("evening", today_str):
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
                await send_md(bot, ALLOWED_USER_ID, f"Напоминание, сэр: {text}")
                db_mark_reminder_sent(reminder_id)
            except Exception as e:
                logger.error(f"Reminder error: {e}")

        await asyncio.sleep(60)

