"""Постановка задач кнопками — надстройка над /task и /fix.

Главное правило: время нигде не вводится с клавиатуры. Пользователь печатает
только суть задачи словами (шаг wait_title), всё остальное — нажатия. Диалог
многошаговый, поэтому его состояние живёт в базе (crew_draft), а не в памяти:
Railway перезапускает сервис при каждом деплое, и словарь в памяти потерялся бы.

Пространство callback-данных — new:, чтобы не пересекаться с crew: (кнопки
карточки задачи). Команды /task и /fix остаются рабочими — это запасной путь.
"""
import logging
from datetime import timedelta, date

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M
from telegram.constants import ParseMode

from core import is_allowed, now_local
import crew as C
from crewbot import send_task_card

logger = logging.getLogger(__name__)

WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Куда ведёт «Назад» с каждого шага.
BACK = {
    "pick_due_date": "pick_due",
    "pick_freq": "confirm",
    "pick_weekday": "pick_freq",
    "pick_days": "pick_freq",
    "pick_monthday": "pick_freq",
    "pick_start": "pick_freq",
    "pick_fix_due": "pick_start",
    "pick_fixdue_time": "pick_fix_due",
    "final": "pick_fix_due",
}


def _default_due():
    """Точка отсчёта для стрелок: ближайший «круглый» час от текущего."""
    now = now_local()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _adj_time(h, m, dh=0, dm=0):
    """Сдвиг времени суток стрелками, с переходом через полночь."""
    total = ((h or 0) * 60 + (m or 0) + dh * 60 + dm) % (24 * 60)
    return total // 60, total % 60


# --- вспомогательное ------------------------------------------------------------

def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _time_stepper(prefix):
    """Стрелки для времени суток: ±час, ±15 мин, Готово, Назад.
    prefix «s» — время постановки, «f» — срок постоянного задания."""
    return M([
        [B("− час", callback_data=f"new:{prefix}h:-1"),
         B("+ час", callback_data=f"new:{prefix}h:1")],
        [B("−15 мин", callback_data=f"new:{prefix}m:-15"),
         B("+15 мин", callback_data=f"new:{prefix}m:15")],
        [B("Готово", callback_data=f"new:{prefix}ok"),
         B("‹ Назад", callback_data="new:back")]])


def _weekdays_label(wd):
    if wd == "1234567":
        return "каждый день"
    if wd == "12345":
        return "по будням"
    return "по " + ", ".join(WD_SHORT[int(d) - 1] for d in wd)


def _sched_label(draft):
    """Как описать расписание постоянного задания: по дням недели или по числу."""
    md = draft.get("monthday")
    if md:
        return "в последний день месяца" if md >= 99 else f"{md} числа каждый месяц"
    return _weekdays_label(draft.get("weekdays") or "")


def _date_label(d):
    return f"{WD_SHORT[d.isoweekday() - 1]} {d:%d.%m}"


def _short(title, n=60):
    """Короткое имя задачи для экранов диалога: первая строка, обрезанная.
    Полное название хранится в черновике и уходит в задачу целиком."""
    line = " ".join((title or "").split())
    return line if len(line) <= n else line[:n].rstrip() + "…"


def _name(draft):
    if not draft.get("person_id"):
        return "?"
    p = C.person_by_id(draft["person_id"])
    return p["name"] if p else "?"


def _screen(draft):
    """Текст и клавиатура для текущего шага — собираются целиком из черновика."""
    step = draft["step"]
    name = _name(draft)
    title = _short(draft.get("title") or "")

    if step == "wait_title":
        return (f"*{name}.* Что сделать?\n\n"
                "Напишите одним сообщением. Можно сразу со сроком — "
                "«...к 17:00», «...через 2 часа», «...завтра». "
                "Если срок не указать — выберете кнопками.",
                M([[B("Отмена", callback_data="new:cancel")]]))

    if step == "pick_due":
        due = draft.get("due_at")
        text = f"*{name}*\n{title}\n\nСрок: {C.fmt_due(due)}"
        if due and due <= now_local():
            text += "  ⚠️ уже прошёл"
        return text, M([
            [B("◀ день", callback_data="new:d:-1"),
             B("день ▶", callback_data="new:d:1")],
            [B("− час", callback_data="new:h:-1"),
             B("+ час", callback_data="new:h:1")],
            [B("−15 мин", callback_data="new:m:-15"),
             B("+15 мин", callback_data="new:m:15")],
            [B("Другой день", callback_data="new:other")],
            [B("Готово", callback_data="new:okdue"),
             B("Отмена", callback_data="new:cancel")]])

    if step == "pick_due_date":
        shift = draft.get("week_shift") or 0
        start = now_local().date() + timedelta(days=shift * 7)
        days = [start + timedelta(days=i) for i in range(7)]
        btns = [B(_date_label(d), callback_data=f"new:date:{d.isoformat()}") for d in days]
        kb = _rows(btns, 3)
        kb.append([B("Ещё неделя ›", callback_data="new:week:1")])
        kb.append([B("‹ Назад", callback_data="new:back")])
        return f"*{name}*\n{title}\n\nКакой день?", M(kb)

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
            [B("Раз в месяц", callback_data="new:freq:month")],
            [B("‹ Назад", callback_data="new:back")]]))

    if step == "pick_monthday":
        md = draft.get("monthday") or 1
        shown = "последний день" if md >= 99 else f"{md} число"
        return (f"*{name}* · {title}\nКакого числа: *{shown}*", M([
            [B("− день", callback_data="new:md:-1"),
             B("+ день", callback_data="new:md:1")],
            [B("Последний день месяца", callback_data="new:mdlast")],
            [B("Готово", callback_data="new:mdok"),
             B("‹ Назад", callback_data="new:back")]]))

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

    if step == "pick_start":
        h, m = draft.get("hour") or 0, draft.get("minute") or 0
        return (f"*{name}* · {title}\nВо сколько ставить: *{h:02d}:{m:02d}*",
                _time_stepper("s"))

    if step == "pick_fix_due":
        return (f"*{name}* · {title}\nДо скольки сделать?", M([
            [B("+1 час", callback_data="new:fixdue:60"),
             B("+2 часа", callback_data="new:fixdue:120"),
             B("+4 часа", callback_data="new:fixdue:240")],
            [B("Конец дня", callback_data="new:fixdue:eod"),
             B("Выбрать точно", callback_data="new:fixdue:hour")],
            [B("‹ Назад", callback_data="new:back")]]))

    if step == "pick_fixdue_time":
        h, m = draft.get("due_hour") or 0, draft.get("due_minute") or 0
        return (f"*{name}* · {title}\nСделать до: *{h:02d}:{m:02d}*",
                _time_stepper("f"))

    if step == "final":
        h, mi = draft.get("hour") or 0, draft.get("minute") or 0
        dh, dm = draft.get("due_hour"), draft.get("due_minute")
        due_txt = f", сделать до {dh:02d}:{dm or 0:02d}" if dh is not None else ""
        head = "Изменить задание" if draft.get("edit_fid") else "Постоянное задание"
        save = "Сохранить" if draft.get("edit_fid") else "Поставить"
        return (f"*{head}*\n\n*{name}*\n{title}\n"
                f"{_sched_label(draft)}, ставить в {h:02d}:{mi:02d}{due_txt}", M([
                    [B(save, callback_data="new:create"),
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
    kb = _rows(btns, 2)
    # Доска задач обрабатывается в crew_button (namespace crew:), не в new:.
    kb.append([B("📋 Доска задач", callback_data="crew:board:0")])
    msg = await update.message.reply_text(
        "*Кому ставим задачу?*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=M(kb))
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

    # Правка времени постоянного задания: грузим его в черновик и идём
    # по тем же стрелкам (постановка → срок → сохранить).
    if action == "fedit":
        if not arg.isdigit():
            await query.answer()
            return
        fx = C.fix_get(int(arg))
        if not fx:
            await query.answer("Задание не найдено")
            return
        C.draft_reset(user_id, query.message.chat_id, "pick_start")
        C.draft_set(user_id, person_id=fx["person_id"], title=fx["title"],
                    weekdays=fx["weekdays"], monthday=fx.get("monthday"),
                    hour=fx["hour"], minute=fx["minute"],
                    due_hour=fx["due_hour"], due_minute=fx["due_minute"],
                    kind="fixedit", edit_fid=fx["id"])
        text, kb = _screen(C.draft_get(user_id))
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id, text=text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        C.draft_set(user_id, message_id=sent.message_id)
        await query.answer("Меняем время")
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

    # --- выбор срока стрелками (разовая) ---
    if action in ("d", "h", "m"):
        try:
            delta = int(arg)
        except ValueError:
            await query.answer()
            return
        due = draft.get("due_at") or _default_due()
        if action == "d":
            due = due + timedelta(days=delta)
        elif action == "h":
            due = due + timedelta(hours=delta)
        else:
            due = due + timedelta(minutes=delta)
        C.draft_set(user_id, due_at=due, step="pick_due")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "other":
        if not draft.get("due_at"):
            C.draft_set(user_id, due_at=_default_due())
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
        due = (draft.get("due_at") or _default_due()).replace(
            year=d.year, month=d.month, day=d.day)
        C.draft_set(user_id, due_at=due, step="pick_due")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "okdue":
        if not draft.get("due_at"):
            C.draft_set(user_id, due_at=_default_due())
        C.draft_set(user_id, step="confirm")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "due":
        if not draft.get("due_at"):
            C.draft_set(user_id, due_at=_default_due())
        C.draft_set(user_id, step="pick_due")
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
        if arg in ("daily", "work"):
            wd = "1234567" if arg == "daily" else "12345"
            C.draft_set(user_id, weekdays=wd, monthday=None, hour=9, minute=0,
                        step="pick_start")
        elif arg == "weekly":
            C.draft_set(user_id, weekdays="", monthday=None, step="pick_weekday")
        elif arg == "pick":
            C.draft_set(user_id, weekdays="", monthday=None, step="pick_days")
        elif arg == "month":
            C.draft_set(user_id, weekdays="", monthday=1, step="pick_monthday")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "md":
        try:
            delta = int(arg)
        except ValueError:
            await query.answer()
            return
        cur = draft.get("monthday") or 1
        if cur >= 99:
            cur = 1  # со «последнего дня» стрелка возвращает к числам
        nd = ((cur - 1 + delta) % 28) + 1
        C.draft_set(user_id, monthday=nd, step="pick_monthday")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "mdlast":
        C.draft_set(user_id, monthday=99, step="pick_monthday")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "mdok":
        fields = {"step": "pick_start"}
        if not draft.get("monthday"):
            fields["monthday"] = 1
        if draft.get("hour") is None:
            fields["hour"], fields["minute"] = 9, 0
        C.draft_set(user_id, **fields)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "wd":
        if not arg.isdigit():
            await query.answer()
            return
        if draft["step"] == "pick_weekday":
            C.draft_set(user_id, weekdays=arg, hour=9, minute=0, step="pick_start")
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
        fields = {"step": "pick_start"}
        if draft.get("hour") is None:
            fields["hour"], fields["minute"] = 9, 0
        C.draft_set(user_id, **fields)
        await _rerender(query, C.draft_get(user_id))
        return

    # стрелки времени: постановка (s) и срок (f)
    if action in ("sh", "sm", "fh", "fm"):
        try:
            delta = int(arg)
        except ValueError:
            await query.answer()
            return
        if action in ("sh", "sm"):
            h, m = _adj_time(draft.get("hour"), draft.get("minute"),
                             dh=delta if action == "sh" else 0,
                             dm=delta if action == "sm" else 0)
            C.draft_set(user_id, hour=h, minute=m, step="pick_start")
        else:
            h, m = _adj_time(draft.get("due_hour"), draft.get("due_minute"),
                             dh=delta if action == "fh" else 0,
                             dm=delta if action == "fm" else 0)
            C.draft_set(user_id, due_hour=h, due_minute=m, step="pick_fixdue_time")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "sok":
        # при создании — экран пресетов срока; при правке — сразу стрелки срока
        nxt = "pick_fixdue_time" if draft.get("edit_fid") else "pick_fix_due"
        fields = {"step": nxt}
        if nxt == "pick_fixdue_time" and draft.get("due_hour") is None:
            h, m = _adj_time(draft.get("hour"), draft.get("minute"), dh=1)
            fields["due_hour"], fields["due_minute"] = h, m
        C.draft_set(user_id, **fields)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "fixdue":
        if arg in ("60", "120", "240"):
            h, m = _adj_time(draft.get("hour"), draft.get("minute"), dh=int(arg) // 60)
            C.draft_set(user_id, due_hour=h, due_minute=m, step="final")
        elif arg == "eod":
            C.draft_set(user_id, due_hour=23, due_minute=59, step="final")
        elif arg == "hour":
            fields = {"step": "pick_fixdue_time"}
            if draft.get("due_hour") is None:
                h, m = _adj_time(draft.get("hour"), draft.get("minute"), dh=1)
                fields["due_hour"], fields["due_minute"] = h, m
            C.draft_set(user_id, **fields)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "fok":
        C.draft_set(user_id, step="final")
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
    md = draft.get("monthday")
    if not person or not title or (not wd and not md):
        C.draft_clear(user_id)
        await query.answer("Диалог сбился")
        await query.edit_message_text("Диалог сбился. Начните заново: /menu")
        return
    h, mi = draft.get("hour") or 9, draft.get("minute") or 0
    dh, dm = draft.get("due_hour"), draft.get("due_minute")
    due_txt = f", до {dh:02d}:{dm or 0:02d}" if dh is not None else ""
    edit_fid = draft.get("edit_fid")
    if edit_fid:
        # правка времени: last_spawn не трогаем, но если сегодня уже ставили
        # раньше нового времени — разрешим поставить сегодня заново.
        C.fix_update(edit_fid, hour=h, minute=mi, due_hour=dh, due_minute=dm)
        C.draft_clear(user_id)
        await query.answer("Сохранено")
        await query.edit_message_text(
            f"✏️ Обновил постоянное задание для *{person['name']}*: {title}\n"
            f"{_sched_label(draft)} в {h:02d}:{mi:02d}{due_txt}  `#f{edit_fid}`",
            parse_mode=ParseMode.MARKDOWN)
        return
    fid = C.fix_create(person["id"], title, h, mi, wd, due_hour=dh,
                       due_minute=dm, monthday=md)
    C.draft_clear(user_id)
    await query.answer("Готово")
    await query.edit_message_text(
        f"♻️ Постоянное задание для *{person['name']}*: {title}\n"
        f"{_sched_label(draft)} в {h:02d}:{mi:02d}{due_txt}  `#f{fid}`",
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

    # Срок можно написать прямо в тексте («...к 17:00», «...через 2 часа»).
    # Нашли — сразу к подтверждению; не нашли — стрелки.
    due, clean = C.parse_due_explicit(title)
    if due is not None:
        C.draft_set(user_id, title=(clean or title)[:500], due_at=due, step="confirm")
    else:
        C.draft_set(user_id, title=title[:500], due_at=_default_due(), step="pick_due")
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
