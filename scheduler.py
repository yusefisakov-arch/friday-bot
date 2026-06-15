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
    goals = db_get_goals()
    text = f"*Доброе утро, сэр*\n\n*Цели сейчас:*\n{goals}\n\n*Задачи на сегодня:*\n{tasks}"
    if urgent:
        text += f"\n\n*Срочное (дедлайн сегодня/завтра):*\n{urgent}"
    text += "\n\nКакие 1-3 главные цели на сегодня? С чего начнём, сэр?"
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_evening_briefing(bot: Bot):
    tasks = db_get_tasks()
    goals = db_get_goals()
    text = (f"*Вечерний разбор, сэр*\n\n*Цели:*\n{goals}\n\n*Открытые задачи:*\n{tasks}\n\n"
            "Как продвинулись по целям сегодня? Что закрыли, что переносим?")
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_weekly_planning(bot: Bot):
    goals = db_get_goals()
    text = (f"*Планирование недели, сэр* 📅\n\n*Текущие цели:*\n{goals}\n\n"
            "Подведём итоги прошлой недели и наметим эту. Какие 3 главные цели на неделю? "
            "Большие — разобью на конкретные шаги.")
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_monthly_planning(bot: Bot):
    goals = db_get_goals()
    text = (f"*Планирование месяца, сэр* 🗓\n\n*Текущие цели:*\n{goals}\n\n"
            "Новый месяц. Что главное хотим достичь? Назовите цели на месяц — "
            "я свяжу их с неделями и задачами и буду отслеживать прогресс.")
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_sop_reminders(bot: Bot):
    now = now_msk()
    current_month = now.strftime("%Y-%m")
    due = db_get_due_sop_reminders(now.day, current_month)
    if not due:
        return
    lines = "\n".join(f"- {text}" for _, text in due)
    await send_md(bot, ALLOWED_USER_ID, f"*Напоминания по SOP, сэр:*\n{lines}")
    # Раньше тут ещё создавалась задача-копия на каждое напоминание — это засоряло
    # список задач дублями. Теперь SOP-напоминание приходит только сообщением.
    for reminder_id, _text in due:
        db_mark_sop_reminder_sent(reminder_id, current_month)


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
            # Планирование недели — по понедельникам, один раз за неделю
            if now.weekday() == 0 and db_claim_daily_job("weekly_plan", goal_period_key("week", now.date())):
                try:
                    await send_weekly_planning(bot)
                except Exception as e:
                    logger.error(f"Weekly planning error: {e}")
            # Планирование месяца — 1-го числа, один раз за месяц
            if now.day == 1 and db_claim_daily_job("monthly_plan", goal_period_key("month", now.date())):
                try:
                    await send_monthly_planning(bot)
                except Exception as e:
                    logger.error(f"Monthly planning error: {e}")

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

