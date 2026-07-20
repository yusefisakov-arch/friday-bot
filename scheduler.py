"""Слой расписания: брифинги, напоминания, ежедневные задачи."""
import os
import asyncio
import logging
from datetime import timedelta
from telegram import Bot

from core import *
from db import *
from ai import generate_mentor_briefing

logger = logging.getLogger(__name__)


async def send_morning_briefing(bot: Bot):
    # Всё утреннее — в ОДНОМ сообщении, чтобы не было пачки отдельных уведомлений.
    goals = db_get_goals()
    tasks = db_get_tasks()
    urgent = db_get_urgent_tasks()

    streak = db_get_streak()
    streak_line = f" 🔥 {streak} дней подряд" if streak >= 2 else ""
    parts = [f"*Доброе утро, сэр* ☀️{streak_line}", f"\n*Цели:*\n{goals}", f"\n*Задачи:*\n{tasks}"]
    if urgent:
        parts.append(f"\n*Срочное (дедлайн сегодня/завтра):*\n{urgent}")
    parts.append("\nКакие 1-3 главные цели на сегодня, сэр? С чего начнём?")
    await send_md(bot, ALLOWED_USER_ID, "\n".join(parts))


async def send_evening_briefing(bot: Bot):
    # Наставнический разбор дня — генерируется моделью по реальным данным.
    try:
        text = await asyncio.to_thread(generate_mentor_briefing)
        text = f"*Вечерний разбор, сэр* 🌙\n\n{text}"
    except Exception as e:
        logger.error(f"Mentor briefing error, fallback: {e}")
        tasks = db_get_tasks()
        goals = db_get_goals()
        text = (f"*Вечерний разбор, сэр*\n\n*Цели:*\n{goals}\n\n*Открытые задачи:*\n{tasks}\n\n"
                "Как продвинулись по целям сегодня? Что закрыли, что переносим?")
    await send_md(bot, ALLOWED_USER_ID, text)


async def send_checkin(bot: Bot, slot):
    if slot == "mid":
        text = "Чек-ин, сэр: чем сейчас занят и на чём застрял? Двигаемся по главному на сегодня?"
    else:
        text = "Чек-ин, сэр: что уже закрыл из главного? Что осталось до вечера?"
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




async def scheduler(bot: Bot):
    # Пауза всех авто-рассылок (утренний бриф, чек-ины, вечерний разбор, планирование,
    # напоминания) на время перестройки бота. Включить обратно: убрать/сменить
    # переменную SCHEDULER_ENABLED на Railway. Ответы на сообщения и формы работают как обычно.
    if os.getenv("SCHEDULER_ENABLED", "true").strip().lower() in ("false", "0", "off", "no"):
        logger.info("Планировщик отключён (SCHEDULER_ENABLED=false) — авто-рассылки на паузе.")
        return
    while True:
        now = now_msk()
        today_str = now.strftime("%Y-%m-%d")

        # Утренний блок: один раз за день в окне 8:00–11:59 (переживает рестарты и задержки).
        # SOP и напоминания по квартирам теперь вшиты в брифинг — одно сообщение вместо пачки.
        if 8 <= now.hour <= 11 and db_claim_daily_job("morning", today_str):
            try:
                await send_morning_briefing(bot)
            except Exception as e:
                logger.error(f"Morning briefing error: {e}")
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

        # Дневные чек-ины (лёгкие): ~13:00 и ~17:00, по разу за день.
        if now.hour == 13 and db_claim_daily_job("checkin_mid", today_str):
            try:
                await send_checkin(bot, "mid")
            except Exception as e:
                logger.error(f"Checkin mid error: {e}")
        if now.hour == 17 and db_claim_daily_job("checkin_eve", today_str):
            try:
                await send_checkin(bot, "eve")
            except Exception as e:
                logger.error(f"Checkin eve error: {e}")

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

