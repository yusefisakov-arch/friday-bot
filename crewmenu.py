"""Постановка задач кнопками — надстройка над /task и /fix.

Главное правило: время нигде не вводится с клавиатуры. Пользователь печатает
только суть задачи словами (шаг wait_title), всё остальное — нажатия. Диалог
многошаговый, поэтому его состояние живёт в базе (crew_draft), а не в памяти:
Railway перезапускает сервис при каждом деплое, и словарь в памяти потерялся бы.

Пространство callback-данных — new:, чтобы не пересекаться с crew: (кнопки
карточки задачи). Команды /task и /fix остаются рабочими — это запасной путь.
"""
import logging
from datetime import datetime, timedelta, date

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode

from core import is_allowed, now_local, LOCAL_TZ
import crew as C
from crewbot import send_task_card

logger = logging.getLogger(__name__)

# Единая сетка часов для всех экранов выбора времени — чтобы не расходились.
HOUR_GRID = [8, 9, 10, 11, 12, 14, 16, 18, 20, 22]  # + «Конец дня» = 23:59
WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Куда ведёт «Назад» с каждого шага.
BACK = {
    "pick_due_date": "pick_due_day",
    "pick_due_hour": "pick_due_day",
    "pick_freq": "confirm",
    "pick_weekday": "pick_freq",
    "pick_days": "pick_freq",
    "pick_start_hour": "pick_freq",
    "pick_fix_due": "pick_start_hour",
    "pick_fix_due_hour": "pick_fix_due",
    "final": "pick_fix_due",
}


# --- вспомогательное ------------------------------------------------------------

def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _hhmm(s):
    return int(s[:2]), int(s[2:])


def _hour_keyboard(prefix):
    """Сетка часов по три в ряд + «Конец дня» + «Назад». Меняется только префикс."""
    btns = [B(f"{h:02d}:00", callback_data=f"new:{prefix}:{h:02d}00") for h in HOUR_GRID]
    rows = _rows(btns, 3)
    rows.append([B("Конец дня", callback_data=f"new:{prefix}:2359")])
    rows.append([B("‹ Назад", callback_data="new:back")])
    return M(rows)


def _weekdays_label(wd):
    if wd == "1234567":
        return "каждый день"
    if wd == "12345":
        return "по будням"
    return "по " + ", ".join(WD_SHORT[int(d) - 1] for d in wd)


def _date_label(d):
    return f"{WD_SHORT[d.isoweekday() - 1]} {d:%d.%m}"


def _name(draft):
    if not draft.get("person_id"):
        return "?"
    p = C.person_by_id(draft["person_id"])
    return p["name"] if p else "?"


def _screen(draft):
    """Текст и клавиатура для текущего шага — собираются целиком из черновика."""
    step = draft["step"]
    name = _name(draft)
    title = draft.get("title") or ""

    if step == "wait_title":
        return (f"*{name}.* Что сделать?\n\n"
                "Напишите одним сообщением, без времени — срок выберете кнопками.\n"
                "Например: «свести заезды и выезды»",
                M([[B("Отмена", callback_data="new:cancel")]]))

    if step == "pick_due_day":
        return (f"*{name}*\n{title}\n\nК какому сроку?", M([
            [B("Через час", callback_data="new:in:60"),
             B("Через 2 часа", callback_data="new:in:120"),
             B("Через 4 часа", callback_data="new:in:240")],
            [B("Сегодня", callback_data="new:day:0"),
             B("Завтра", callback_data="new:day:1"),
             B("Послезавтра", callback_data="new:day:2")],
            [B("Другой день", callback_data="new:other")],
            [B("Отмена", callback_data="new:cancel")]]))

    if step == "pick_due_date":
        shift = draft.get("week_shift") or 0
        start = now_local().date() + timedelta(days=shift * 7)
        days = [start + timedelta(days=i) for i in range(7)]
        btns = [B(_date_label(d), callback_data=f"new:date:{d.isoformat()}") for d in days]
        kb = _rows(btns, 3)
        kb.append([B("Ещё неделя ›", callback_data="new:week:1")])
        kb.append([B("‹ Назад", callback_data="new:back")])
        return f"*{name}*\n{title}\n\nКакой день?", M(kb)

    if step == "pick_due_hour":
        d = draft.get("pick_date")
        when = _date_label(d) if d else ""
        return f"*{name}* · {title}\n{when}, во сколько?", _hour_keyboard("hour")

    if step == "confirm":
        due = draft.get("due_at")
        text = f"*{name}*\n{title}\n\nСрок: {C.fmt_due(due)}"
        if due and due <= now_local():
            text += "\n\n⚠️ этот срок уже прошёл"
        return text, M([
            [B("Разовая", callback_data="new:once"),
             B("Постоянная", callback_data="new:fix")],
            [B("Изменить срок", callback_data="new:due"),
             B("Отмена", callback_data="new:cancel")]])

    if step == "pick_freq":
        return (f"*{name}*\n{title}\n\nКак часто?", M([
            [B("Каждый день", callback_data="new:freq:daily"),
             B("По будням", callback_data="new:freq:work")],
            [B("Раз в неделю", callback_data="new:freq:weekly"),
             B("Выбрать дни", callback_data="new:freq:pick")],
            [B("‹ Назад", callback_data="new:back")]]))

    if step == "pick_weekday":
        btns = [B(WD_SHORT[i], callback_data=f"new:wd:{i + 1}") for i in range(7)]
        kb = _rows(btns, 4)
        kb.append([B("‹ Назад", callback_data="new:back")])
        return f"*{name}*\n{title}\n\nВ какой день?", M(kb)

    if step == "pick_days":
        chosen = set(draft.get("weekdays") or "")
        btns = []
        for i in range(7):
            d = str(i + 1)
            mark = "✓ " if d in chosen else ""
            btns.append(B(f"{mark}{WD_SHORT[i]}", callback_data=f"new:wd:{i + 1}"))
        kb = _rows(btns, 4)
        kb.append([B("Готово", callback_data="new:wd_ok"),
                   B("‹ Назад", callback_data="new:back")])
        return f"*{name}*\n{title}\n\nПо каким дням?", M(kb)

    if step == "pick_start_hour":
        return (f"*{name}* · {title}\nВо сколько ставить задачу?",
                _hour_keyboard("start"))

    if step == "pick_fix_due":
        return (f"*{name}* · {title}\nДо скольки сделать?", M([
            [B("+1 час", callback_data="new:fixdue:60"),
             B("+2 часа", callback_data="new:fixdue:120"),
             B("+4 часа", callback_data="new:fixdue:240")],
            [B("Конец дня", callback_data="new:fixdue:eod"),
             B("Выбрать час", callback_data="new:fixdue:hour")],
            [B("‹ Назад", callback_data="new:back")]]))

    if step == "pick_fix_due_hour":
        return f"*{name}* · {title}\nДо скольки сделать?", _hour_keyboard("fixhour")

    if step == "final":
        wd = draft.get("weekdays") or ""
        h, mi = draft.get("hour") or 0, draft.get("minute") or 0
        dh, dm = draft.get("due_hour"), draft.get("due_minute")
        due_txt = f", сделать до {dh:02d}:{dm or 0:02d}" if dh is not None else ""
        return (f"*Постоянное задание*\n\n*{name}*\n{title}\n"
                f"{_weekdays_label(wd)}, ставить в {h:02d}:{mi:02d}{due_txt}", M([
                    [B("Поставить", callback_data="new:create"),
                     B("Отмена", callback_data="new:cancel")]]))

    return "…", M([[B("Отмена", callback_data="new:cancel")]])


async def _rerender(query, draft):
    text, kb = _screen(draft)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=kb)
    except Exception as e:
        logger.debug("Диалог не перерисовался: %s", e)
    await query.answer()


# --- команда /menu --------------------------------------------------------------

async def menu_cmd(update, context):
    """/menu — постоянное меню с кнопкой на каждого человека."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    people = C.people_all()
    if not people:
        await update.message.reply_text(
            "Пока некому ставить задачи. Заведите человека: создайте с ним "
            "группу, добавьте меня и напишите там /crew Имя.")
        return
    btns = [B(p["name"], callback_data=f"new:who:{p['id']}") for p in people]
    msg = await update.message.reply_text(
        "*Кому ставим задачу?*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=M(_rows(btns, 2)))
    try:
        await context.bot.pin_chat_message(chat_id=msg.chat_id,
                                           message_id=msg.message_id,
                                           disable_notification=True)
    except Exception as e:
        logger.debug("Меню не закрепилось: %s", e)


# --- все нажатия new: -----------------------------------------------------------

async def menu_button(update, context):
    query = update.callback_query
    user_id = query.from_user.id
    if not is_allowed(user_id):
        await query.answer("Не для вас")
        return

    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""
    C.crew_init_db()

    # Выбор человека начинает новый диалог отдельным сообщением; постоянное
    # меню при этом остаётся на месте и годится для следующей задачи.
    if action == "who":
        if not arg.isdigit():
            await query.answer()
            return
        person = C.person_by_id(int(arg))
        if not person:
            await query.answer("Человек не найден")
            return
        C.draft_reset(user_id, query.message.chat_id, "wait_title")
        C.draft_set(user_id, person_id=person["id"])
        text, kb = _screen(C.draft_get(user_id))
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        C.draft_set(user_id, message_id=sent.message_id)
        await query.answer()
        return

    draft = C.draft_get(user_id)
    if not draft:
        await query.answer("Диалог устарел — начните с /menu")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if action == "cancel":
        C.draft_clear(user_id)
        await query.answer("Отменено")
        try:
            await query.edit_message_text("Отменено.")
        except Exception:
            pass
        return

    if action == "back":
        prev = BACK.get(draft["step"])
        if prev:
            C.draft_set(user_id, step=prev)
        await _rerender(query, C.draft_get(user_id))
        return

    # --- выбор срока (разовая) ---
    if action == "in":
        mins = int(arg) if arg.isdigit() else 60
        C.draft_set(user_id, due_at=now_local() + timedelta(minutes=mins),
                    step="confirm")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "day":
        n = int(arg) if arg.isdigit() else 0
        C.draft_set(user_id, pick_date=now_local().date() + timedelta(days=n),
                    step="pick_due_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "other":
        C.draft_set(user_id, step="pick_due_date", week_shift=0)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "week":
        delta = int(arg) if arg.lstrip("-").isdigit() else 1
        shift = max(0, (draft.get("week_shift") or 0) + delta)
        C.draft_set(user_id, week_shift=shift, step="pick_due_date")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "date":
        try:
            d = date.fromisoformat(arg)
        except ValueError:
            await query.answer()
            return
        C.draft_set(user_id, pick_date=d, step="pick_due_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "hour":
        hh, mm = _hhmm(arg)
        d = draft.get("pick_date") or now_local().date()
        due = datetime(d.year, d.month, d.day, hh, mm, tzinfo=LOCAL_TZ)
        C.draft_set(user_id, due_at=due, step="confirm")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "due":
        C.draft_set(user_id, step="pick_due_day")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "once":
        await _create_once(query, context, draft, user_id)
        return

    # --- постоянная ---
    if action == "fix":
        C.draft_set(user_id, kind="fix", step="pick_freq")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "freq":
        mapping = {"daily": ("1234567", "pick_start_hour"),
                   "work": ("12345", "pick_start_hour"),
                   "weekly": ("", "pick_weekday"),
                   "pick": ("", "pick_days")}
        wd, step = mapping.get(arg, ("", "pick_freq"))
        C.draft_set(user_id, weekdays=wd, step=step)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "wd":
        if not arg.isdigit():
            await query.answer()
            return
        if draft["step"] == "pick_weekday":
            C.draft_set(user_id, weekdays=arg, step="pick_start_hour")
        elif draft["step"] == "pick_days":
            chosen = set(draft.get("weekdays") or "")
            chosen.discard(arg) if arg in chosen else chosen.add(arg)
            C.draft_set(user_id, weekdays="".join(sorted(chosen)))
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "wd_ok":
        if not (draft.get("weekdays") or ""):
            await query.answer("Выберите хотя бы один день")
            return
        C.draft_set(user_id, step="pick_start_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "start":
        hh, mm = _hhmm(arg)
        C.draft_set(user_id, hour=hh, minute=mm, step="pick_fix_due")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "fixdue":
        if arg in ("60", "120", "240"):
            base = (draft.get("hour") or 0) * 60 + (draft.get("minute") or 0)
            total = base + int(arg)
            C.draft_set(user_id, due_hour=(total // 60) % 24, due_minute=total % 60,
                        step="final")
        elif arg == "eod":
            C.draft_set(user_id, due_hour=23, due_minute=59, step="final")
        elif arg == "hour":
            C.draft_set(user_id, step="pick_fix_due_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "fixhour":
        hh, mm = _hhmm(arg)
        C.draft_set(user_id, due_hour=hh, due_minute=mm, step="final")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "create":
        await _create_fix(query, draft, user_id)
        return

    await query.answer()


# --- создание -------------------------------------------------------------------

async def _create_once(query, context, draft, user_id):
    person = C.person_by_id(draft["person_id"]) if draft.get("person_id") else None
    title = (draft.get("title") or "").strip()
    due = draft.get("due_at")
    if not person or not title:
        C.draft_clear(user_id)
        await query.answer("Диалог сбился")
        await query.edit_message_text("Диалог сбился. Начните заново: /menu")
        return
    tid = C.task_create(person["id"], title, due)
    C.draft_clear(user_id)
    await query.answer("Готово")
    try:
        await send_task_card(context.bot, C.task_get(tid), person)
    except Exception as e:
        logger.error("Карточка задачи #%s не ушла: %s", tid, e)
        await query.edit_message_text(
            f"Задачу записал (#{tid}), но карточка в группу «{person['name']}» "
            "не ушла. Проверьте, что бот в группе и может писать.")
        return
    await query.edit_message_text(
        f"✅ Поставил *{person['name']}*: {title}\n"
        f"Срок: {C.fmt_due(due)}  `#{tid}`", parse_mode=ParseMode.MARKDOWN)


async def _create_fix(query, draft, user_id):
    person = C.person_by_id(draft["person_id"]) if draft.get("person_id") else None
    title = (draft.get("title") or "").strip()
    wd = draft.get("weekdays") or ""
    if not person or not title or not wd:
        C.draft_clear(user_id)
        await query.answer("Диалог сбился")
        await query.edit_message_text("Диалог сбился. Начните заново: /menu")
        return
    h, mi = draft.get("hour") or 9, draft.get("minute") or 0
    dh, dm = draft.get("due_hour"), draft.get("due_minute")
    fid = C.fix_create(person["id"], title, h, mi, wd, due_hour=dh, due_minute=dm)
    C.draft_clear(user_id)
    await query.answer("Готово")
    due_txt = f", до {dh:02d}:{dm or 0:02d}" if dh is not None else ""
    await query.edit_message_text(
        f"♻️ Постоянное задание для *{person['name']}*: {title}\n"
        f"{_weekdays_label(wd)} в {h:02d}:{mi:02d}{due_txt}  `#f{fid}`",
        parse_mode=ParseMode.MARKDOWN)


# --- ввод названия задачи (единственный текстовый шаг) --------------------------

async def catch_draft_input(update, context):
    """Ловит название задачи, когда пользователь в шаге wait_title.
    Возвращает True, если сообщение обработано (дальше его трогать не надо)."""
    msg = update.message
    if not msg or not msg.text:
        return False
    user_id = msg.from_user.id
    if not is_allowed(user_id):
        return False
    draft = C.draft_get(user_id)
    if not draft or draft["step"] != "wait_title":
        return False
    if draft.get("chat_id") and msg.chat_id != draft["chat_id"]:
        return False

    title = msg.text.strip()
    if not title:
        await msg.reply_text("Не понял, что делать. Напишите задачу словами.")
        return True

    C.draft_set(user_id, title=title[:500], step="pick_due_day")
    draft = C.draft_get(user_id)
    text, kb = _screen(draft)
    if draft.get("message_id"):
        try:
            await context.bot.edit_message_text(
                chat_id=draft["chat_id"], message_id=draft["message_id"],
                text=text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return True
        except Exception as e:
            logger.debug("Не смог обновить диалог, шлю новым: %s", e)
    sent = await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    C.draft_set(user_id, message_id=sent.message_id)
    return True
