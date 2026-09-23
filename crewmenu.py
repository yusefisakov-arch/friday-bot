"""Постановка задач кнопками — надстройка над /task и /fix.

Время нигде не набирается с клавиатуры: выбор тапом. Даты — сеткой с
листанием недель; часы — полная сетка 0–23; минуты — :00/:15/:30/:45.
Диалог многошаговый, состояние живёт в базе (crew_draft), чтобы переживать
передеплой. Callback-пространство new:, чтобы не пересекаться с crew:.

Разовая задача: можно выбрать, КОГДА отправить её в группу (сейчас или
позже) и СРОК сдачи. Отложенные доставляет фоновый цикл (crewbot).
"""
import logging
from datetime import datetime, timedelta, date

from telegram import (InlineKeyboardButton as B, InlineKeyboardMarkup as M,
                      KeyboardButton, ReplyKeyboardMarkup)
from telegram.constants import ParseMode

from core import is_allowed, now_local, LOCAL_TZ
import crew as C
from crewbot import send_task_card, refresh_card, remove_card, render_board, \
    _board_kb, render_tasks, render_weekly

# Метки нижней клавиатуры-панели (кнопки шлют эти тексты).
PANEL_BOARD = "📋 Доска"
PANEL_EDIT = "✏️ Изменить задачу"
PANEL_DAY = "📅 План на день"
PANEL_WEEK = "📆 Неделя"
PANEL_LABELS = {PANEL_BOARD, PANEL_EDIT, PANEL_DAY, PANEL_WEEK}

logger = logging.getLogger(__name__)

WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# «Назад» для нешаговых экранов (пикеры навигируются своими pback/tback).
BACK = {
    "pick_freq": "confirm",
    "pick_weekday": "pick_freq",
    "pick_days": "pick_freq",
    "pick_monthday": "pick_freq",
}


def _default_due():
    now = now_local()
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


# --- клавиатуры ------------------------------------------------------------------

def _rows(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _panel_kb():
    """Постоянная нижняя клавиатура Штаба: доска, люди, изменить, отчёты."""
    people = C.people_all()
    rows = [[KeyboardButton(PANEL_BOARD), KeyboardButton(PANEL_EDIT)]]
    rows += _rows([KeyboardButton(p["name"]) for p in people], 2)
    rows.append([KeyboardButton(PANEL_DAY), KeyboardButton(PANEL_WEEK)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _edit_groups_kb():
    people = C.people_all()
    kb = _rows([B(p["name"], callback_data=f"new:egrp:{p['id']}") for p in people], 2)
    kb.append([B("Закрыть", callback_data="new:eclose")])
    return M(kb)


def _hour_grid(prefix, back):
    """Полная сетка часов 0–23 по 6 в ряд + Назад."""
    btns = [B(f"{h:02d}", callback_data=f"new:{prefix}:{h}") for h in range(24)]
    kb = _rows(btns, 6)
    kb.append([B("‹ Назад", callback_data=back)])
    return M(kb)


def _min_grid(prefix, back):
    btns = [B(f":{m:02d}", callback_data=f"new:{prefix}:{m}") for m in (0, 15, 30, 45)]
    return M([btns, [B("‹ Назад", callback_data=back)]])


def _date_grid(draft, editing):
    shift = draft.get("week_shift") or 0
    base = now_local().date()
    start = base + timedelta(days=shift * 7)
    days = [start + timedelta(days=i) for i in range(7)]
    rows = []
    if shift == 0:
        rows.append([
            B("Сегодня", callback_data=f"new:pdate:{base.isoformat()}"),
            B("Завтра", callback_data=f"new:pdate:{(base + timedelta(days=1)).isoformat()}"),
            B("Послезавтра", callback_data=f"new:pdate:{(base + timedelta(days=2)).isoformat()}")])
    daybtns = [B(_date_label(d), callback_data=f"new:pdate:{d.isoformat()}") for d in days]
    rows += _rows(daybtns, 3)
    nav = []
    if shift > 0:
        nav.append(B("‹ Неделя", callback_data="new:pweek:-1"))
    nav.append(B("Неделя ›", callback_data="new:pweek:1"))
    rows.append(nav)
    if editing == "send":
        rows.append([B("📤 Отправить сейчас", callback_data="new:pnow")])
    rows.append([B("‹ Назад", callback_data="new:pback")])
    return M(rows)


# --- подписи ---------------------------------------------------------------------

def _weekdays_label(wd):
    if wd == "1234567":
        return "каждый день"
    if wd == "12345":
        return "по будням"
    return "по " + ", ".join(WD_SHORT[int(d) - 1] for d in wd)


def _sched_label(draft):
    md = draft.get("monthday")
    if md:
        return "в последний день месяца" if md >= 99 else f"{md} числа каждый месяц"
    return _weekdays_label(draft.get("weekdays") or "")


def _date_label(d):
    return f"{WD_SHORT[d.isoweekday() - 1]} {d:%d.%m}"


def _short(title, n=60):
    line = " ".join((title or "").split())
    return line if len(line) <= n else line[:n].rstrip() + "…"


def _name(draft):
    if not draft.get("person_id"):
        return "?"
    p = C.person_by_id(draft["person_id"])
    return p["name"] if p else "?"


def _field(draft):
    """Куда пишет пикер даты/времени сейчас."""
    return "send_at" if draft.get("editing") == "send" else "due_at"


# --- экраны ----------------------------------------------------------------------

def _screen(draft):
    step = draft["step"]
    name = _name(draft)
    title = _short(draft.get("title") or "")

    if step == "wait_title":
        return (f"*{name}.* Что сделать?\n\n"
                "Напишите одним сообщением. Можно сразу со сроком — "
                "«...к 17:00», «...завтра». Если нет — выберете кнопками.",
                M([[B("Отмена", callback_data="new:cancel")]]))

    if step == "confirm":
        send = draft.get("send_at")
        due = draft.get("due_at")
        prio = draft.get("priority") or 1
        send_txt = C.fmt_due(send) if send else "сейчас"
        text = (f"*{name}*\n{title}\n\n"
                f"📤 Отправить: {send_txt}\n🎯 Срок: {C.fmt_due(due)}\n"
                f"⚑ Важность: {C.PRIORITY_LABEL[prio]} (провал −{prio})")
        if due and due <= now_local():
            text += "\n\n⚠️ срок уже прошёл"
        return text, M([
            [B("🕐 Когда отправить", callback_data="new:setsend"),
             B("🎯 Срок", callback_data="new:setdue")],
            [B(f"⚑ Важность: {C.PRIORITY_LABEL[prio]}", callback_data="new:prio")],
            [B("✅ Разовая", callback_data="new:once"),
             B("♻️ Постоянная", callback_data="new:fix")],
            [B("Отмена", callback_data="new:cancel")]])

    if step == "pd_date":
        head = "Когда отправить" if draft.get("editing") == "send" else "Срок"
        return f"*{name}*\n{title}\n\n{head} — какой день?", _date_grid(
            draft, draft.get("editing"))

    if step == "pd_hour":
        head = "Когда отправить" if draft.get("editing") == "send" else "Срок"
        return f"*{name}*\n{title}\n\n{head} — час:", _hour_grid("phour", "new:pback")

    if step == "pd_min":
        return f"*{name}*\n{title}\n\nМинуты:", _min_grid("pmin", "new:pback")

    if step == "pick_freq":
        return (f"*{name}*\n{title}\n\nКак часто?", M([
            [B("Каждый день", callback_data="new:freq:daily"),
             B("По будням", callback_data="new:freq:work")],
            [B("Раз в неделю", callback_data="new:freq:weekly"),
             B("Выбрать дни", callback_data="new:freq:pick")],
            [B("Раз в месяц", callback_data="new:freq:month")],
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

    if step == "pick_monthday":
        btns = [B(str(n), callback_data=f"new:mday:{n}") for n in range(1, 29)]
        kb = _rows(btns, 7)
        kb.append([B("Последний день месяца", callback_data="new:mday:99")])
        kb.append([B("‹ Назад", callback_data="new:back")])
        return f"*{name}*\n{title}\n\nКакого числа?", M(kb)

    if step == "tod_hour":
        head = "Во сколько ставить" if draft.get("editing") == "start" else "Сделать до"
        return f"*{name}* · {title}\n{head} — час:", _hour_grid("thour", "new:tback")

    if step == "tod_min":
        return f"*{name}* · {title}\nМинуты:", _min_grid("tmin", "new:tback")

    if step == "pick_fix_due":
        return (f"*{name}* · {title}\nДо скольки сделать?", M([
            [B("+1 час", callback_data="new:fixdue:60"),
             B("+2 часа", callback_data="new:fixdue:120"),
             B("+4 часа", callback_data="new:fixdue:240")],
            [B("Конец дня", callback_data="new:fixdue:eod"),
             B("Выбрать точно", callback_data="new:fixdue:precise")],
            [B("‹ Назад", callback_data="new:back")]]))

    if step == "final":
        h, mi = draft.get("hour") or 0, draft.get("minute") or 0
        dh, dm = draft.get("due_hour"), draft.get("due_minute")
        prio = draft.get("priority") or 1
        due_txt = f", сделать до {dh:02d}:{dm or 0:02d}" if dh is not None else ""
        head = "Изменить задание" if draft.get("edit_fid") else "Постоянное задание"
        save = "Сохранить" if draft.get("edit_fid") else "Поставить"
        return (f"*{head}*\n\n*{name}*\n{title}\n"
                f"{_sched_label(draft)}, ставить в {h:02d}:{mi:02d}{due_txt}\n"
                f"⚑ Важность: {C.PRIORITY_LABEL[prio]} (провал −{prio})", M([
                    [B(f"⚑ Важность: {C.PRIORITY_LABEL[prio]}", callback_data="new:prio")],
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
    """/menu — постоянное меню с кнопкой на каждого человека и доской."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    people = C.people_all()
    if not people:
        await update.message.reply_text(
            "Пока некому ставить задачи. Заведите человека: создайте с ним "
            "группу, добавьте меня и напишите там /crew Имя.")
        return
    await update.message.reply_text(
        "*Панель готова.*\nНажмите имя внизу — поставить задачу. "
        "Или: 📋 Доска · ✏️ Изменить · 📅 План · 📆 Неделя.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=_panel_kb())


async def handle_panel(update, context):
    """Кнопки нижней панели шлют текст-метку — обрабатываем как действия.
    Возвращает True, если сообщение обработано."""
    msg = update.message
    if not msg or not msg.text:
        return False
    uid = msg.from_user.id
    if not is_allowed(uid):
        return False
    text = msg.text.strip()

    if text == PANEL_BOARD:
        await msg.reply_text(render_board(), parse_mode=ParseMode.MARKDOWN,
                             reply_markup=_board_kb())
        return True
    if text == PANEL_DAY:
        await msg.reply_text(render_tasks(C.tasks_for_day(), "🌅 План на день"),
                             parse_mode=ParseMode.MARKDOWN)
        return True
    if text == PANEL_WEEK:
        await msg.reply_text(render_weekly(), parse_mode=ParseMode.MARKDOWN)
        return True
    if text == PANEL_EDIT:
        await msg.reply_text("✏️ *Изменить задачу*\nВыберите группу:",
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=_edit_groups_kb())
        return True

    # Имя человека с панели — начать постановку задачи (если не идёт ввод текста).
    draft = C.draft_get(uid)
    if draft and draft.get("step") == "wait_title":
        return False
    person = C.person_by_name(text)
    if person and person["name"].lower() == text.lower():
        C.draft_reset(uid, msg.chat_id, "wait_title")
        C.draft_set(uid, person_id=person["id"])
        await _open_dialog(context, msg.chat_id, uid)
        return True
    return False


async def _open_dialog(context, chat_id, user_id):
    """Показывает текущий шаг черновика новым сообщением, запоминает его id."""
    text, kb = _screen(C.draft_get(user_id))
    sent = await context.bot.send_message(chat_id=chat_id, text=text,
                                          parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=kb)
    C.draft_set(user_id, message_id=sent.message_id)


# --- нажатия new: ---------------------------------------------------------------

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
        await _open_dialog(context, query.message.chat_id, user_id)
        await query.answer()
        return

    if action == "fedit":
        if not arg.isdigit():
            await query.answer()
            return
        fx = C.fix_get(int(arg))
        if not fx:
            await query.answer("Задание не найдено")
            return
        C.draft_reset(user_id, query.message.chat_id, "tod_hour")
        C.draft_set(user_id, person_id=fx["person_id"], title=fx["title"],
                    weekdays=fx["weekdays"], monthday=fx.get("monthday"),
                    hour=fx["hour"], minute=fx["minute"],
                    due_hour=fx["due_hour"], due_minute=fx["due_minute"],
                    priority=fx.get("priority") or 1,
                    kind="fixedit", edit_fid=fx["id"], editing="start")
        await _open_dialog(context, query.message.chat_id, user_id)
        await query.answer("Меняем время")
        return

    # ---- «Изменить задачу»: группа → список → выбор → правка ----
    if action == "edit":
        if not C.people_all():
            await query.answer("Некого выбрать")
            return
        await context.bot.send_message(
            chat_id=query.message.chat_id, text="✏️ *Изменить задачу*\nВыберите группу:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=_edit_groups_kb())
        await query.answer()
        return

    if action == "eback":
        try:
            await query.edit_message_text("✏️ *Изменить задачу*\nВыберите группу:",
                                          parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=_edit_groups_kb())
        except Exception:
            pass
        await query.answer()
        return

    if action == "eclose":
        try:
            await query.edit_message_text("Закрыто.")
        except Exception:
            pass
        await query.answer()
        return

    if action == "egrp":
        if not arg.isdigit():
            await query.answer()
            return
        p = C.person_by_id(int(arg))
        tasks = C.tasks_open(int(arg))
        if not tasks:
            try:
                await query.edit_message_text(
                    f"У *{p['name'] if p else '?'}* нет открытых задач.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=M([[B("‹ Назад", callback_data="new:eback")]]))
            except Exception:
                pass
            await query.answer()
            return
        rows = [[B(f"{C.STATUS_ICON.get(t['status'], '•')} {_short(t['title'], 40)}",
                   callback_data=f"new:etask:{t['id']}")] for t in tasks]
        rows.append([B("‹ Назад", callback_data="new:eback")])
        try:
            await query.edit_message_text(
                f"✏️ *{p['name'] if p else '?'}* — выберите задачу:",
                parse_mode=ParseMode.MARKDOWN, reply_markup=M(rows))
        except Exception:
            pass
        await query.answer()
        return

    if action == "etask":
        if not arg.isdigit():
            await query.answer()
            return
        t = C.task_get(int(arg))
        if not t:
            await query.answer("Задача не найдена")
            return
        text = (f"✏️ {_short(t['title'], 80)}\n"
                f"Срок: {C.fmt_due(t['due_at'])} · {t['status']}")
        kb = M([
            [B("🎯 Изменить срок", callback_data=f"new:edue:{t['id']}")],
            [B("🚫 Снять задачу", callback_data=f"new:ecancel:{t['id']}")],
            [B("‹ Назад", callback_data=f"new:egrp:{t['person_id']}")]])
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception:
            pass
        await query.answer()
        return

    if action == "edue":
        if not arg.isdigit():
            await query.answer()
            return
        t = C.task_get(int(arg))
        if not t:
            await query.answer("Задача не найдена")
            return
        C.draft_reset(user_id, query.message.chat_id, "pd_date")
        C.draft_set(user_id, person_id=t["person_id"], title=t["title"],
                    due_at=t["due_at"] or _default_due(), editing="due",
                    edit_tid=t["id"], message_id=query.message.message_id)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "ecancel":
        if not arg.isdigit():
            await query.answer()
            return
        t = C.task_get(int(arg))
        if not t:
            await query.answer("Задача не найдена")
            return
        C.task_update(t["id"], status=C.STATUS_CANCELLED)
        try:
            await remove_card(context.bot, C.task_get(t["id"]))
        except Exception:
            pass
        await query.answer("Снял")
        try:
            await query.edit_message_text(f"🚫 Снял задачу: {_short(t['title'], 50)}")
        except Exception:
            pass
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
        if draft["step"] == "pick_fix_due":
            C.draft_set(user_id, editing="start", step="tod_hour")
        else:
            prev = BACK.get(draft["step"])
            if prev:
                C.draft_set(user_id, step=prev)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "prio":
        # цикл важности: обычная → важная → приоритетная → обычная
        cur = draft.get("priority") or 1
        C.draft_set(user_id, priority=(cur % 3) + 1)
        await _rerender(query, C.draft_get(user_id))
        return

    # ---- пикер даты/времени для разовой (send / due) ----
    if action == "setsend":
        C.draft_set(user_id, editing="send", week_shift=0, step="pd_date")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "setdue":
        C.draft_set(user_id, editing="due", week_shift=0, step="pd_date")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "pnow":
        C.draft_set(user_id, send_at=None, step="confirm")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "pweek":
        delta = int(arg) if arg.lstrip("-").isdigit() else 1
        shift = max(0, (draft.get("week_shift") or 0) + delta)
        C.draft_set(user_id, week_shift=shift, step="pd_date")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "pdate":
        try:
            d = date.fromisoformat(arg)
        except ValueError:
            await query.answer()
            return
        cur = draft.get(_field(draft)) or _default_due()
        cur = cur.replace(year=d.year, month=d.month, day=d.day)
        C.draft_set(user_id, **{_field(draft): cur, "step": "pd_hour"})
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "phour":
        cur = (draft.get(_field(draft)) or _default_due()).replace(
            hour=int(arg), minute=0)
        C.draft_set(user_id, **{_field(draft): cur, "step": "pd_min"})
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "pmin":
        cur = (draft.get(_field(draft)) or _default_due()).replace(minute=int(arg))
        if draft.get("edit_tid"):
            # правка срока существующей задачи — обновляем и обновляем карточку
            C.task_update(draft["edit_tid"], due_at=cur,
                          warned_due=False, asked_due=False)
            try:
                await refresh_card(context.bot, draft["edit_tid"])
            except Exception:
                pass
            C.draft_clear(user_id)
            await query.answer("Срок изменён")
            try:
                await query.edit_message_text(f"✅ Срок изменён: {C.fmt_due(cur)}")
            except Exception:
                pass
            return
        C.draft_set(user_id, **{_field(draft): cur, "step": "confirm"})
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "pback":
        nxt = {"pd_min": "pd_hour", "pd_hour": "pd_date",
               "pd_date": "confirm"}.get(draft["step"], "confirm")
        C.draft_set(user_id, step=nxt)
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "once":
        await _create_once(query, context, draft, user_id)
        return

    # ---- постоянная ----
    if action == "fix":
        C.draft_set(user_id, kind="fix", step="pick_freq")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "freq":
        if arg in ("daily", "work"):
            wd = "1234567" if arg == "daily" else "12345"
            C.draft_set(user_id, weekdays=wd, monthday=None, editing="start",
                        step="tod_hour")
        elif arg == "weekly":
            C.draft_set(user_id, weekdays="", monthday=None, step="pick_weekday")
        elif arg == "pick":
            C.draft_set(user_id, weekdays="", monthday=None, step="pick_days")
        elif arg == "month":
            C.draft_set(user_id, weekdays="", step="pick_monthday")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "wd":
        if not arg.isdigit():
            await query.answer()
            return
        if draft["step"] == "pick_weekday":
            C.draft_set(user_id, weekdays=arg, editing="start", step="tod_hour")
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
        C.draft_set(user_id, editing="start", step="tod_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "mday":
        if not arg.isdigit():
            await query.answer()
            return
        C.draft_set(user_id, monthday=int(arg), editing="start", step="tod_hour")
        await _rerender(query, C.draft_get(user_id))
        return

    # ---- пикер времени суток (start / fixdue) ----
    if action == "thour":
        if draft.get("editing") == "start":
            C.draft_set(user_id, hour=int(arg), minute=0, step="tod_min")
        else:
            C.draft_set(user_id, due_hour=int(arg), due_minute=0, step="tod_min")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "tmin":
        if draft.get("editing") == "start":
            C.draft_set(user_id, minute=int(arg))
            if draft.get("edit_fid"):
                C.draft_set(user_id, editing="fixdue", step="tod_hour")
            else:
                C.draft_set(user_id, step="pick_fix_due")
        else:
            C.draft_set(user_id, due_minute=int(arg), step="final")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "tback":
        editing = draft.get("editing")
        if draft["step"] == "tod_min":
            C.draft_set(user_id, step="tod_hour")
        elif editing == "fixdue":
            if draft.get("edit_fid"):
                C.draft_set(user_id, editing="start", step="tod_hour")
            else:
                C.draft_set(user_id, step="pick_fix_due")
        elif not draft.get("edit_fid"):
            C.draft_set(user_id, step="pick_freq")
        await _rerender(query, C.draft_get(user_id))
        return

    if action == "fixdue":
        if arg in ("60", "120", "240"):
            total = ((draft.get("hour") or 0) * 60 + (draft.get("minute") or 0)
                     + int(arg))
            C.draft_set(user_id, due_hour=(total // 60) % 24, due_minute=total % 60,
                        step="final")
        elif arg == "eod":
            C.draft_set(user_id, due_hour=23, due_minute=59, step="final")
        elif arg == "precise":
            C.draft_set(user_id, editing="fixdue", step="tod_hour")
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
    send = draft.get("send_at")
    if not person or not title:
        C.draft_clear(user_id)
        await query.answer("Диалог сбился")
        await query.edit_message_text("Диалог сбился. Начните заново: /menu")
        return
    scheduled = bool(send and send > now_local())
    tid = C.task_create(person["id"], title, due,
                        send_at=send if scheduled else None,
                        priority=draft.get("priority") or 1)
    C.draft_clear(user_id)
    await query.answer("Готово")

    if scheduled:
        await query.edit_message_text(
            f"📤 Запланировал *{person['name']}*: {title}\n"
            f"Отправлю {C.fmt_due(send)} · срок {C.fmt_due(due)}  `#{tid}`",
            parse_mode=ParseMode.MARKDOWN)
        return
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
        C.fix_update(edit_fid, hour=h, minute=mi, due_hour=dh, due_minute=dm)
        C.draft_clear(user_id)
        await query.answer("Сохранено")
        await query.edit_message_text(
            f"✏️ Обновил постоянное задание для *{person['name']}*: {title}\n"
            f"{_sched_label(draft)} в {h:02d}:{mi:02d}{due_txt}  `#f{edit_fid}`",
            parse_mode=ParseMode.MARKDOWN)
        return
    fid = C.fix_create(person["id"], title, h, mi, wd, due_hour=dh,
                       due_minute=dm, monthday=md, priority=draft.get("priority") or 1)
    C.draft_clear(user_id)
    await query.answer("Готово")
    await query.edit_message_text(
        f"♻️ Постоянное задание для *{person['name']}*: {title}\n"
        f"{_sched_label(draft)} в {h:02d}:{mi:02d}{due_txt}  `#f{fid}`",
        parse_mode=ParseMode.MARKDOWN)


# --- ввод названия задачи (единственный текстовый шаг) --------------------------

async def catch_draft_input(update, context):
    """Ловит название задачи в шаге wait_title. True — если обработано."""
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

    # Срок можно написать прямо в тексте. Отправка по умолчанию — сейчас;
    # изменить и срок, и время отправки можно на экране подтверждения.
    due, clean = C.parse_due_explicit(title)
    if due is not None:
        C.draft_set(user_id, title=(clean or title)[:500], due_at=due,
                    send_at=None, editing="due", step="confirm")
    else:
        C.draft_set(user_id, title=title[:500], due_at=_default_due(),
                    send_at=None, editing="due", step="confirm")

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
