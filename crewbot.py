"""Команды, кнопки и фоновый контроль задач.

Отделено от crew.py сознательно: там данные и правила, здесь разговор с
Telegram. Правила можно проверить тестами, не поднимая ни бота, ни группы.
"""
import asyncio
import calendar
import logging
from datetime import timedelta

from telegram import (ForceReply, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto)
from telegram.constants import ParseMode
from telegram.error import BadRequest

# Слова, которыми исполнитель завершает или пропускает шаг с фото.
PHOTO_STOP = {"нет", "no", "-", "пропустить", "skip", "не надо", "готово",
              "всё", "все", "хватит", "дальше", "да", "ок", "ok"}


async def _ask(msg, flow, text, **kw):
    """Задаёт вопрос в диалоге и запоминает сообщение как временное (на удаление)."""
    sent = await msg.reply_text(text, **kw)
    flow.setdefault("trash", []).append(sent.message_id)
    return sent


async def _cleanup(bot, chat_id, flow):
    """Убирает все промежуточные сообщения диалога — чтобы не засорять группу.
    Карточка задачи не в списке, она остаётся и обновляется на месте."""
    for mid in flow.get("trash", []):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass


async def _send_photos(bot, chat_id, file_ids, caption=None):
    """Шлёт фото: одно — обычным сообщением, несколько — альбомом (до 10)."""
    ids = list(file_ids or [])[:10]
    if not ids:
        return
    if len(ids) == 1:
        await bot.send_photo(chat_id=chat_id, photo=ids[0], caption=caption)
        return
    media = [InputMediaPhoto(media=f, caption=caption if i == 0 else None)
             for i, f in enumerate(ids)]
    await bot.send_media_group(chat_id=chat_id, media=media)

from core import db_conn, is_allowed, now_local, send_md, LOCAL_TZ, ALLOWED_USER_ID
import crew as C

logger = logging.getLogger(__name__)

HQ_KEY = "hq_chat_id"
CHECK_EVERY_SECONDS = 180

# Активные разборы «Проблемы», по одному на человека в чате:
# (chat_id, user_id) -> {"tid", "step", "desc", "photo", "solution"}
# step: desc -> photo -> solution. По завершении уходит отчёт в Штаб.
PROBLEM_FLOW = {}

# Владелец пишет инструкцию после «Запретить»: (chat_id, user_id) -> tid
AWAIT_INSTR = {}

# Исполнитель заполняет отчёт после «Отчёт»:
# (chat_id, user_id) -> {"tid", "step", "done", "photo", "left"}
REPORT_FLOW = {}

# Исполнитель закрывает задачу по «Готово»: что сделал → фото → в Штаб.
# (chat_id, user_id) -> {"tid", "step", "what", "photo"}
DONE_FLOW = {}


# --- вспомогательное ------------------------------------------------------------

def task_buttons(task):
    if task["status"] in (C.STATUS_DONE, C.STATUS_CANCELLED):
        return None
    row = [
        InlineKeyboardButton("✅ Готово", callback_data=f"crew:done:{task['id']}"),
        InlineKeyboardButton("⚠️ Проблема", callback_data=f"crew:problem:{task['id']}")]
    second = [InlineKeyboardButton("⏳ Не успеваю", callback_data=f"crew:more:{task['id']}")]
    return InlineKeyboardMarkup([row, second])


async def send_task_card(bot, task, person):
    msg = await bot.send_message(
        chat_id=person["chat_id"], text=C.task_card(task, person),
        parse_mode=ParseMode.MARKDOWN, reply_markup=task_buttons(task))
    C.task_update(task["id"], chat_id=person["chat_id"], message_id=msg.message_id)
    return msg


async def refresh_card(bot, task_id):
    """Перерисовывает карточку на месте — история в группе не засоряется."""
    task = C.task_get(task_id)
    if not task or not task.get("message_id"):
        return
    person = C.person_by_id(task["person_id"])
    if not person:
        return
    try:
        await bot.edit_message_text(
            chat_id=task["chat_id"], message_id=task["message_id"],
            text=C.task_card(task, person), parse_mode=ParseMode.MARKDOWN,
            reply_markup=task_buttons(task))
    except Exception as e:
        logger.debug(f"Карточку #{task_id} не обновить: {e}")


def hq_chat_id():
    """Куда писать вам. Штаб, если заведён, иначе личка."""
    raw = C.state_get(HQ_KEY)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return ALLOWED_USER_ID or None


async def tell_boss(bot, text):
    chat = hq_chat_id()
    if not chat:
        logger.warning("Некому сообщить: ни Штаба, ни ALLOWED_USER_ID")
        return
    try:
        await bot.send_message(chat_id=chat, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Не смог написать в Штаб: {e}")


def mention(person):
    if person.get("username"):
        return f"@{person['username']}"
    return person["name"]


def _short(title, n=50):
    """Короткая ссылка на задачу для напоминаний: первая строка, обрезанная.
    Полный текст и так есть в карточке, на которую напоминание отвечает."""
    line = (title or "").splitlines()[0].strip()
    return line if len(line) <= n else line[:n].rstrip() + "…"


# --- команды --------------------------------------------------------------------

async def crew_here_cmd(update, context):
    """/crew Имя — сказать боту, что эта группа принадлежит этому человеку."""
    if not is_allowed(update.effective_user.id):
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Эту команду нужно отправить в группе того человека, "
            "которого заводим. Например: /crew Эдик")
        return

    name = " ".join(context.args).strip() if context.args else ""
    if not name:
        await update.message.reply_text("Напишите имя: /crew Эдик")
        return

    C.crew_init_db()
    pid = C.person_save(name, chat.id)
    await update.message.reply_text(
        f"Готово. Эта группа — *{name}*.\n\n"
        f"Теперь задачи ему ставятся так: `/task {name} починить бойлер в 204, до 18:00`\n"
        f"Или прямо здесь, без имени: `/task починить бойлер в 204, до 18:00`",
        parse_mode=ParseMode.MARKDOWN)
    logger.info("Команда: группа %s закреплена за человеком #%s", chat.id, pid)


async def hq_here_cmd(update, context):
    """/hq — сделать эту группу Штабом: сюда идут сводки и эскалации."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    C.state_set(HQ_KEY, update.effective_chat.id)
    await update.message.reply_text(
        "Это Штаб. Сюда буду присылать сводки и сообщать, если кто-то тормозит.")


async def crew_list_cmd(update, context):
    """/crew_list — кто заведён."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    people = C.people_all()
    if not people:
        await update.message.reply_text(
            "Никто не заведён. Создайте группу с человеком, добавьте меня "
            "и напишите там: /crew Имя")
        return
    lines = ["*Команда*", ""]
    for p in people:
        st = C.person_stats(p["id"])
        lines.append(
            f"• *{p['name']}* — открыто {st['open']}, "
            f"вовремя {st['on_time']}, с опозданием {st['late']}, "
            f"провалено {st['failed']} _(за 30 дней)_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def task_cmd(update, context):
    """/task [Имя] что сделать, до когда."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()

    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.message.reply_text(
            "Как ставить задачу:\n"
            "`/task Эдик починить бойлер в 204, до 18:00`\n"
            "`/task Жанеля свести кассу, завтра до 12:00`\n"
            "`/task Эдик вывезти мусор, через 2 часа`\n\n"
            "В группе самого человека имя можно не писать.",
            parse_mode=ParseMode.MARKDOWN)
        return

    here = C.person_by_chat(update.effective_chat.id)
    person, title, due = C.parse_assignment(raw, default_person=here)

    if not person:
        names = ", ".join(p["name"] for p in C.people_all()) or "пока никого"
        await update.message.reply_text(
            f"Не понял, кому. Заведены: {names}.\n"
            "Начните с имени: /task Эдик ...")
        return
    if not title:
        await update.message.reply_text("Не понял, что делать. Напишите задачу словами.")
        return

    tid = C.task_create(person["id"], title, due)
    task = C.task_get(tid)
    try:
        await send_task_card(context.bot, task, person)
    except Exception as e:
        logger.error("Задача #%s не отправилась в группу %s: %s",
                     tid, person["chat_id"], e)
        await update.message.reply_text(
            f"Задачу записал (#{tid}), но в группу «{person['name']}» не отправилось. "
            f"Проверьте, что бот в группе и может писать.")
        return

    if update.effective_chat.id != person["chat_id"]:
        await update.message.reply_text(
            f"Поставил *{person['name']}*: {title}\nСрок: {C.fmt_due(due)}  `#{tid}`",
            parse_mode=ParseMode.MARKDOWN)


async def fix_cmd(update, context):
    """/fix Имя что делать, каждый день 9:00."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()

    raw = " ".join(context.args).strip() if context.args else ""
    if not raw:
        await update.message.reply_text(
            "Постоянные задания — те, что бот ставит сам каждый раз:\n"
            "`/fix Эдик обход котельной, каждый день 9:00`\n"
            "`/fix Жанеля сверка кассы, каждый будни в 10`\n"
            "`/fix Елена отчёт, каждую пятницу 16:00`\n\n"
            "Список и удаление: /fix\\_list, /fix\\_del 3",
            parse_mode=ParseMode.MARKDOWN)
        return

    here = C.person_by_chat(update.effective_chat.id)
    first = raw.split()[0].strip(",:").lstrip("@")
    person = C.person_by_name(first) or here
    if person and C.person_by_name(first):
        raw = raw[len(raw.split()[0]):].strip(" ,:")
    if not person:
        await update.message.reply_text("Не понял, кому. Начните с имени.")
        return

    parsed = C.parse_fix(raw)
    if not parsed:
        await update.message.reply_text(
            "Не понял расписание. Нужно «каждый день», «каждый будни» "
            "или «каждую пятницу», и время: `каждый день 9:00`",
            parse_mode=ParseMode.MARKDOWN)
        return

    hour, minute, weekdays, due_hour, due_minute, title = parsed
    if not title:
        await update.message.reply_text("Не понял, что делать.")
        return

    fid = C.fix_create(person["id"], title, hour, minute, weekdays,
                       due_hour=due_hour, due_minute=due_minute)
    days = {"1234567": "каждый день", "12345": "по будням"}.get(
        weekdays, "по " + ", ".join(
            ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][int(d) - 1] for d in weekdays))
    due_txt = (f", сделать до {due_hour:02d}:{due_minute:02d}"
               if due_hour is not None else "")
    await update.message.reply_text(
        f"Постоянное задание для *{person['name']}*: {title}\n"
        f"{days} в {hour:02d}:{minute:02d}{due_txt}  `#f{fid}`",
        parse_mode=ParseMode.MARKDOWN)


async def fix_list_cmd(update, context):
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    rows = C.fix_all()
    if not rows:
        await update.message.reply_text("Постоянных заданий нет. Завести: /fix")
        return
    lines = ["*Постоянные задания*", ""]
    for f in rows:
        p = C.person_by_id(f["person_id"])
        md = f.get("monthday")
        if md:
            days = "в последний день месяца" if md >= 99 else f"{md} числа каждый месяц"
        else:
            days = {"1234567": "каждый день", "12345": "по будням"}.get(
                f["weekdays"], "по дням " + f["weekdays"])
        due_txt = (f" → до {f['due_hour']:02d}:{f['due_minute'] or 0:02d}"
                   if f["due_hour"] is not None else "")
        lines.append(f"`#f{f['id']}` *{p['name'] if p else '?'}* — {f['title']}\n"
                     f"       {days} в {f['hour']:02d}:{f['minute']:02d}{due_txt}")
    lines.append("\nУдалить: /fix\\_del 3")
    # Кнопки правки времени — по одной на задание.
    btns = [InlineKeyboardButton(f"✏️ f{f['id']}",
                                 callback_data=f"new:fedit:{f['id']}") for f in rows]
    kb = InlineKeyboardMarkup([btns[i:i + 3] for i in range(0, len(btns), 3)])
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=kb)


async def fix_del_cmd(update, context):
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Какое удалить? /fix_del 3")
        return
    C.crew_init_db()
    try:
        fid = int(context.args[0].lstrip("#f"))
    except ValueError:
        await update.message.reply_text("Нужен номер: /fix_del 3")
        return
    title = C.fix_delete(fid)
    await update.message.reply_text(
        f"Убрал: {title}" if title else "Такого задания нет.")


async def today_cmd(update, context):
    """/today — что у всех на сегодня."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    await update.message.reply_text(
        render_tasks(C.tasks_for_day(), "📅 Сегодня"),
        parse_mode=ParseMode.MARKDOWN)


async def debts_cmd(update, context):
    """/debts — всё просроченное."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    await update.message.reply_text(
        render_tasks(C.tasks_overdue(), "⏰ Просрочено"),
        parse_mode=ParseMode.MARKDOWN)


STATE_WORD = {
    C.STATUS_NEW: "🆕 ждёт",
    C.STATUS_TAKEN: "🔧 в работе",
    C.STATUS_PROBLEM: "⚠️ проблема",
    C.STATUS_DONE: "✅ сделано",
    C.STATUS_FAILED: "❌ провалено",
    C.STATUS_CANCELLED: "🚫 снята",
}


def _task_line(t, now):
    """Одна строка задачи: статус — суть — когда. Формат одинаков везде."""
    prio = t.get("priority") or 1
    tag = f"{C.PRIORITY_ICON.get(prio, '')} " if prio > 1 else ""
    if not t.get("message_id") and t.get("send_at"):
        return f"⏳ отложена · {tag}{_short(t['title'], 45)} — уйдёт {C.fmt_due(t['send_at'])}"
    when = C.fmt_due(t["due_at"])
    if t["due_at"] and t["status"] in C.OPEN_STATUSES \
            and t["due_at"].astimezone(now.tzinfo) < now:
        when = f"просрочка {C.fmt_overdue(t['due_at'])}"
    return f"{STATE_WORD.get(t['status'], '•')} · {tag}{_short(t['title'], 45)} — {when}"


def render_tasks(tasks, title):
    """Единый формат сводки: по людям, статус явно, каждая задача один раз."""
    seen, uniq = set(), []
    for t in tasks:
        if t["id"] in seen or t["status"] == C.STATUS_CANCELLED:
            continue  # снятые в сводке не показываем
        seen.add(t["id"])
        uniq.append(t)
    if not uniq:
        return f"*{title}*\n\nЗадач нет."
    now = now_local()
    by_person = {}
    for t in uniq:
        by_person.setdefault(t["person_id"], []).append(t)
    lines = [f"*{title}*"]
    for pid, items in by_person.items():
        p = C.person_by_id(pid)
        lines.append(f"\n👤 *{p['name'] if p else '?'}*")
        for t in items:
            lines.append("  " + _task_line(t, now))
    counts = {}
    for t in uniq:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    summary = " · ".join(
        f"{STATE_WORD.get(s, s).split()[0]} {counts[s]}"
        for s in (C.STATUS_NEW, C.STATUS_TAKEN, C.STATUS_PROBLEM,
                  C.STATUS_DONE, C.STATUS_FAILED) if counts.get(s))
    if summary:
        lines.append(f"\n_{summary}_")
    return "\n".join(lines)


def render_board():
    """Доска по статусам: просрочено / проблема / в работе / не взято / сделано.
    Полный текст задачи, секции разделены."""
    now = now_local()
    openz = C.tasks_open()
    done = [t for t in C.tasks_for_day() if t["status"] == C.STATUS_DONE]

    overdue, problem, working, waiting, scheduled = [], [], [], [], []
    for t in openz:
        if not t.get("message_id") and t.get("send_at"):
            scheduled.append(t)
        elif t["due_at"] and t["due_at"].astimezone(now.tzinfo) < now:
            overdue.append(t)
        elif t["status"] == C.STATUS_PROBLEM:
            problem.append(t)
        elif t["status"] == C.STATUS_TAKEN:
            working.append(t)
        else:
            waiting.append(t)

    sections = [("⏰ Просрочено", overdue), ("⚠️ Проблема", problem),
                ("🔧 В работе", working), ("🆕 Не взято", waiting),
                ("⏳ Отложенные", scheduled), ("✅ Сделано сегодня", done)]
    if not any(items for _, items in sections):
        return "📋 *Доска задач*\n\nЗадач нет."

    blocks = ["📋 *Доска задач*"]
    for label, items in sections:
        if not items:
            continue
        lines = [f"*{label}* ({len(items)})"]
        for t in items:
            p = C.person_by_id(t["person_id"])
            who = p["name"] if p else "?"
            if t in scheduled:
                when = f"уйдёт {C.fmt_due(t['send_at'])}"
            elif t in overdue:
                when = f"просрочка {C.fmt_overdue(t['due_at'])}"
            else:
                when = C.fmt_due(t["due_at"])
            prio = t.get("priority") or 1
            tag = f"{C.PRIORITY_ICON.get(prio, '')} " if prio > 1 else ""
            lines.append(f"👤 *{who}* · {when}  `#{t['id']}`\n{tag}{t['title']}")
        blocks.append("\n".join(lines))
    return "\n\n➖➖➖➖➖\n\n".join(blocks)


def render_weekly():
    """Итоги недели по людям (за 7 дней)."""
    people = C.people_all()
    if not people:
        return "📆 *Итоги недели*\n\nНикто не заведён."
    rows, t_ok, t_late, t_fail, t_pen = [], 0, 0, 0, 0
    for p in people:
        st = C.person_stats(p["id"], days=7)
        t_ok += st["on_time"]
        t_late += st["late"]
        t_fail += st["failed"]
        t_pen += st["penalty"]
        pen = f" · 🚫 штраф {st['penalty']}" if st["penalty"] else ""
        rows.append(f"👤 *{p['name']}*\n"
                    f"  ✅ вовремя {st['on_time']} · ⏰ с опозданием {st['late']} · "
                    f"❌ провалено {st['failed']}{pen}")
    head = ["📆 *Итоги недели* (7 дней)", "",
            f"Всего: ✅ {t_ok} · ⏰ {t_late} · ❌ {t_fail} · 🚫 штрафов {t_pen}", ""]
    return "\n".join(head + rows)


def _board_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Обновить", callback_data="crew:board:0")]])


async def board_cmd(update, context):
    """/board — доска задач по всем группам (в Штабе или в личке)."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    await update.message.reply_text(render_board(), parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=_board_kb())


async def done_cmd(update, context):
    """/done 12 — закрыть задачу руками."""
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Какую? /done 12")
        return
    C.crew_init_db()
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Нужен номер: /done 12")
        return
    task = C.task_get(tid)
    if not task:
        await update.message.reply_text("Такой задачи нет.")
        return
    C.task_update(tid, status=C.STATUS_DONE, done_at=now_local())
    await refresh_card(context.bot, tid)
    await update.message.reply_text(f"Закрыл #{tid}: {task['title']}")


async def cancel_cmd(update, context):
    """/cancel 12 — снять задачу."""
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Какую? /cancel 12")
        return
    C.crew_init_db()
    try:
        tid = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Нужен номер: /cancel 12")
        return
    task = C.task_get(tid)
    if not task:
        await update.message.reply_text("Такой задачи нет.")
        return
    C.task_update(tid, status=C.STATUS_CANCELLED)
    await refresh_card(context.bot, tid)
    await update.message.reply_text(f"Снял #{tid}: {task['title']}")


async def clear_cmd(update, context):
    """/clear [N] — удалить последние N сообщений в этом чате (по умолчанию 100).

    Читать историю боты не могут, поэтому идём по номерам сообщений от текущего
    вниз и удаляем. Свои сообщения бот удаляет всегда; чужие — только если он
    админ с правом удаления. Чего удалить нельзя — молча пропускаем."""
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    last_id = update.message.message_id
    try:
        n = int(context.args[0]) if context.args else 100
    except ValueError:
        n = 100
    n = max(1, min(n, 300))

    deleted = 0
    for mid in range(last_id, max(0, last_id - n), -1):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)

    try:
        note = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🧹 Удалил {deleted} сообщений. Меню вернуть: /menu")
        # само подтверждение убираем через несколько секунд, чтобы не мусорить
        await asyncio.sleep(4)
        await context.bot.delete_message(chat_id=chat_id, message_id=note.message_id)
    except Exception:
        pass


# --- кнопки ---------------------------------------------------------------------

async def crew_button(update, context):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "crew" or not parts[2].isdigit():
        return
    action, tid = parts[1], int(parts[2])

    C.crew_init_db()

    # Доска задач — не привязана к конкретной задаче (tid=0), обрабатываем раньше.
    if action == "board":
        if not is_allowed(query.from_user.id):
            await query.answer("Не для вас")
            return
        try:
            await query.edit_message_text(render_board(), parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=_board_kb())
        except Exception:
            pass  # «сообщение не изменилось» — не страшно
        await query.answer("Обновлено")
        return

    task = C.task_get(tid)
    if not task:
        await query.answer("Задача не найдена")
        return

    # Одобрение/запрет решения и запрос отчёта — до проверки чата: кнопки
    # «Одобрить/Запретить» живут в Штабе, а не в группе задачи.
    if action in ("approve", "reject", "report"):
        await _handle_report_actions(update, context, action, task)
        return
    # Одобрение/отклонение переноса срока — тоже из Штаба.
    if action in ("extok", "extno"):
        await _handle_extension(update, context, action, task)
        return

    # Кнопку можно нажать только в том чате, куда ушла карточка задачи —
    # защита от нажатий по чужой задаче из другого чата.
    if task["chat_id"] and query.message and query.message.chat_id != task["chat_id"]:
        await query.answer()
        return

    # Просьба о переносе срока — жмёт исполнитель в своей группе.
    if action in ("more", "mh1", "mh2", "mh4", "meod", "mtom", "mcancel"):
        await _handle_extension(update, context, action, task)
        return

    person = C.person_by_id(task["person_id"])
    user = query.from_user

    # Кто нажал — тот и работник этой группы: запоминаем, чтобы упоминать по @
    if person and not person.get("username") and user.username:
        C.person_save(person["name"], person["chat_id"],
                      tg_user_id=user.id, username=user.username)
        person = C.person_by_id(task["person_id"])

    if action == "done":
        # «Готово» ведёт короткий отчёт: что сделал → фото → закрыть и в Штаб.
        flow = {"tid": tid, "step": "what", "what": "", "photos": [], "trash": []}
        DONE_FLOW[(query.message.chat_id, user.id)] = flow
        await query.answer("Короткий отчёт")
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            text=f"{mention(person) if person else ''} что сделали по задаче "
                 f"«{_short(task['title'])}»? Опишите одним сообщением.",
            reply_markup=ForceReply(selective=bool(person and person.get("username"))))
        flow["trash"].append(sent.message_id)
        return
    elif action == "problem":
        C.task_update(tid, status=C.STATUS_PROBLEM)
        flow = {"tid": tid, "step": "desc", "desc": "", "photos": [],
                "solution": "", "trash": []}
        PROBLEM_FLOW[(query.message.chat_id, user.id)] = flow
        await query.answer("Опишите проблему")
        sent = await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            text=f"{mention(person) if person else ''} что случилось по задаче "
                 f"«{_short(task['title'])}»? Опишите одним сообщением.",
            reply_markup=ForceReply(selective=bool(person and person.get("username"))))
        flow["trash"].append(sent.message_id)
    else:
        await query.answer()
        return

    await refresh_card(context.bot, tid)


async def catch_problem_note(update, context):
    """Свободные сообщения по задачам. Порядок разбора:
    1) владелец пишет инструкцию после «Запретить» (может быть и в личке-Штабе);
    2) исполнитель заполняет отчёт после «Отчёт»;
    3) исполнитель закрывает задачу по «Готово»;
    4) исполнитель поясняет «Проблему».
    Вне этих состояний свободного разговора с ботом нет."""
    msg = update.message
    if not msg:
        return
    key = (msg.chat_id, msg.from_user.id)
    text = (msg.text or msg.caption or "").strip()

    # 1) инструкция владельца — до ограничения «только группы»: Штаб бывает личкой
    if key in AWAIT_INSTR:
        await _handle_instruction(context.bot, msg, key, text)
        return
    # 2) отчёт исполнителя
    if key in REPORT_FLOW:
        await _handle_report_step(context.bot, msg, key, text)
        return
    # 3) закрытие задачи по «Готово»
    if key in DONE_FLOW:
        await _handle_done_step(context.bot, msg, key, text)
        return

    # 4) разбор «Проблемы» — только в группах
    if msg.chat.type not in ("group", "supergroup"):
        return
    flow = PROBLEM_FLOW.get(key)
    if not flow:
        return
    flow.setdefault("trash", []).append(msg.message_id)

    step = flow["step"]

    if step == "desc":
        if not text:
            await _ask(msg, flow, "Опишите словами, в чём проблема.")
            return
        flow["desc"] = text[:1000]
        flow["step"] = "photo"
        await _ask(msg, flow,
                   "Принял. Нужны фото — пришлите (можно несколько). Если нет — «нет».",
                   reply_markup=ForceReply(selective=True))
        return

    if step == "photo":
        if msg.photo:
            flow.setdefault("photos", []).append(msg.photo[-1].file_id)
            await _ask(msg, flow,
                       f"Принял фото ({len(flow['photos'])}). Ещё? Или «готово».",
                       reply_markup=ForceReply(selective=True))
            return
        if text.lower() not in PHOTO_STOP:
            await _ask(msg, flow, "Пришлите фото или напишите «готово» / «нет».")
            return
        flow["step"] = "solution"
        await _ask(msg, flow, "Хорошо. Как, по-вашему, это решить?",
                   reply_markup=ForceReply(selective=True))
        return

    if step == "solution":
        if not text:
            await _ask(msg, flow, "Напишите, как предлагаете решить.")
            return
        flow["solution"] = text[:1000]
        PROBLEM_FLOW.pop(key, None)
        await _cleanup(context.bot, msg.chat_id, flow)
        await _finish_problem(context.bot, msg, flow)


async def _finish_problem(bot, msg, flow):
    """Сохраняет разбор в задачу и шлёт полноценный отчёт в Штаб (+фото)."""
    C.crew_init_db()
    tid = flow["tid"]
    task = C.task_get(tid)
    if not task:
        return
    person = C.person_by_id(task["person_id"])

    C.task_update(tid, note=f"{flow['desc']}\n\nРешение: {flow['solution']}"[:500])
    await refresh_card(bot, tid)

    who = person["name"] if person else "?"
    report = (
        f"⚠️ *{who}* сообщает о проблеме\n"
        f"Задача: {task['title']}  `#{tid}`\n"
        f"Срок: {C.fmt_due(task['due_at'])}\n\n"
        f"*Что случилось:*\n{flow['desc']}\n\n"
        f"*Как предлагает решить:*\n{flow['solution']}")

    chat = hq_chat_id()
    if not chat:
        logger.warning("Отчёт по проблеме #%s некому отправить (нет Штаба)", tid)
        return
    try:
        await _send_photos(bot, chat, flow.get("photos"),
                           caption=f"Фото к проблеме по задаче #{tid}")
    except Exception as e:
        logger.error("Фото к проблеме #%s не ушло: %s", tid, e)
    # Отчёт с решением идёт последним, на нём — кнопки одобрения.
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"crew:approve:{tid}"),
        InlineKeyboardButton("✍️ Запретить", callback_data=f"crew:reject:{tid}")]])
    await send_md(bot, chat, report, reply_markup=kb)


# --- одобрение решения и отчёт исполнителя --------------------------------------

def _report_deadline(task):
    """Когда ждём отчёт: к сроку задачи, а если срока нет или он прошёл —
    к концу текущего дня."""
    now = now_local()
    due = task.get("due_at")
    if due and due.astimezone(LOCAL_TZ) > now:
        return due
    return now.replace(hour=23, minute=59, second=0, microsecond=0)


async def _send_report_button(bot, task, person, text):
    """Шлёт исполнителю сообщение с кнопкой «Отчёт» и включает ожидание отчёта.
    Обычные напоминания по сроку глушим — теперь следим за отчётом."""
    C.task_update(task["id"], report_due=_report_deadline(task),
                  report_done=False, report_nagged=False,
                  status=C.STATUS_TAKEN, warned_due=True, asked_due=True,
                  told_boss=True)
    chat = task["chat_id"] or (person["chat_id"] if person else None)
    if not chat:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(
        "📋 Отчёт", callback_data=f"crew:report:{task['id']}")]])
    try:
        await bot.send_message(chat_id=chat, text=text,
                               parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except BadRequest:
        await bot.send_message(chat_id=chat, text=text, reply_markup=kb)
    except Exception as e:
        logger.error("Кнопка «Отчёт» по #%s не ушла: %s", task["id"], e)


async def _mark_boss_msg(query, note):
    """Дописывает строку к сообщению в Штабе и убирает кнопки."""
    try:
        await query.edit_message_text((query.message.text or "") + "\n\n" + note)
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def _handle_report_actions(update, context, action, task):
    query = update.callback_query
    tid = task["id"]
    person = C.person_by_id(task["person_id"])

    if action in ("approve", "reject") and not is_allowed(query.from_user.id):
        await query.answer("Не для вас")
        return

    if action == "approve":
        await query.answer("Одобрено")
        await _send_report_button(
            context.bot, C.task_get(tid), person,
            "✅ Руководитель одобрил ваше решение. Действуйте, как предложили.\n"
            "Как закончите — нажмите «Отчёт».")
        await _mark_boss_msg(
            query, f"✅ Одобрено. Жду отчёт до {C.fmt_due(_report_deadline(task))}.")
        return

    if action == "reject":
        AWAIT_INSTR[(query.message.chat_id, query.from_user.id)] = tid
        await query.answer("Напишите инструкцию")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Что делать по «{task['title']}»? Напишите инструкцию одним "
                 "сообщением — передам исполнителю.",
            reply_markup=ForceReply(selective=False))
        return

    if action == "report":
        # «Отчёт» жмёт исполнитель в своей группе
        REPORT_FLOW[(query.message.chat_id, query.from_user.id)] = {
            "tid": tid, "step": "done", "done": "", "photos": [], "left": ""}
        await query.answer("Отчёт")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            text=f"{mention(person) if person else ''} отчёт по «{task['title']}».\n"
                 "Что сделали? Опишите одним сообщением.",
            reply_markup=ForceReply(selective=bool(person and person.get("username"))))
        return


async def _handle_instruction(bot, msg, key, text):
    """Владелец написал инструкцию после «Запретить» — шлём её исполнителю."""
    tid = AWAIT_INSTR.get(key)
    if not text:
        await msg.reply_text("Напишите инструкцию словами.")
        return
    AWAIT_INSTR.pop(key, None)
    C.crew_init_db()
    task = C.task_get(tid)
    if not task:
        return
    person = C.person_by_id(task["person_id"])
    C.task_update(tid, boss_note=text[:800])
    await msg.reply_text("Передал исполнителю.")
    await _send_report_button(
        bot, C.task_get(tid), person,
        f"❌ Решение не одобрили.\n*Что делать:* {text}\n\n"
        "Как сделаете — нажмите «Отчёт».")


async def _handle_report_step(bot, msg, key, text):
    """Пошаговый отчёт исполнителя: что сделал → фото → итог → в Штаб."""
    flow = REPORT_FLOW.get(key)
    if not flow:
        return
    flow.setdefault("trash", []).append(msg.message_id)
    step = flow["step"]

    if step == "done":
        if not text:
            await _ask(msg, flow, "Опишите, что сделали.")
            return
        flow["done"] = text[:1000]
        flow["step"] = "photo"
        await _ask(msg, flow,
                   "Есть фото результата? Пришлите (можно несколько) или «нет».",
                   reply_markup=ForceReply(selective=True))
        return

    if step == "photo":
        if msg.photo:
            flow.setdefault("photos", []).append(msg.photo[-1].file_id)
            await _ask(msg, flow,
                       f"Принял фото ({len(flow['photos'])}). Ещё? Или «готово».",
                       reply_markup=ForceReply(selective=True))
            return
        if text.lower() not in PHOTO_STOP:
            await _ask(msg, flow, "Пришлите фото или напишите «готово» / «нет».")
            return
        flow["step"] = "left"
        await _ask(msg, flow, "Всё решено или что-то осталось? Напишите коротко.",
                   reply_markup=ForceReply(selective=True))
        return

    if step == "left":
        if not text:
            await _ask(msg, flow, "Напишите: всё решено или что осталось.")
            return
        flow["left"] = text[:1000]
        REPORT_FLOW.pop(key, None)
        await _cleanup(bot, msg.chat_id, flow)
        await _finish_report(bot, msg, flow)


async def _finish_report(bot, msg, flow):
    C.crew_init_db()
    tid = flow["tid"]
    task = C.task_get(tid)
    if not task:
        return
    person = C.person_by_id(task["person_id"])
    stored = f"Сделано: {flow['done']}"
    if flow.get("left"):
        stored += f"\nИтог: {flow['left']}"
    C.task_update(tid, report_text=stored[:800], report_done=True,
                  status=C.STATUS_DONE, done_at=now_local())
    await refresh_card(bot, tid)

    who = person["name"] if person else "?"
    report = (f"📋 *{who}* отчитался по задаче\n"
              f"{task['title']}  `#{tid}`\n\n"
              f"*Сделано:*\n{flow['done']}")
    if flow.get("left"):
        report += f"\n\n*Итог:*\n{flow['left']}"
    chat = hq_chat_id()
    if not chat:
        return
    try:
        await _send_photos(bot, chat, flow.get("photos"),
                           caption=f"Фото к отчёту по задаче #{tid}")
    except Exception as e:
        logger.error("Фото отчёта #%s не ушло: %s", tid, e)
    await send_md(bot, chat, report)


# --- «Готово» с отчётом о выполнении --------------------------------------------

async def _handle_done_step(bot, msg, key, text):
    """Короткий отчёт при закрытии задачи: что сделал → фото → закрыть."""
    flow = DONE_FLOW.get(key)
    if not flow:
        return
    flow.setdefault("trash", []).append(msg.message_id)
    step = flow["step"]

    if step == "what":
        if not text:
            await _ask(msg, flow, "Опишите, что сделали.")
            return
        flow["what"] = text[:1000]
        flow["step"] = "photo"
        await _ask(msg, flow,
                   "Есть фото результата? Пришлите (можно несколько) или «нет».",
                   reply_markup=ForceReply(selective=True))
        return

    if step == "photo":
        if msg.photo:
            flow.setdefault("photos", []).append(msg.photo[-1].file_id)
            await _ask(msg, flow,
                       f"Принял фото ({len(flow['photos'])}). Ещё? Или «готово».",
                       reply_markup=ForceReply(selective=True))
            return
        if text.lower() not in PHOTO_STOP:
            await _ask(msg, flow, "Пришлите фото или напишите «готово» / «нет».")
            return
        DONE_FLOW.pop(key, None)
        await _cleanup(bot, msg.chat_id, flow)
        await _finish_done(bot, msg, flow)


async def _finish_done(bot, msg, flow):
    C.crew_init_db()
    tid = flow["tid"]
    task = C.task_get(tid)
    if not task:
        return
    person = C.person_by_id(task["person_id"])
    late = task["due_at"] and now_local() > task["due_at"]
    C.task_update(tid, status=C.STATUS_DONE, done_at=now_local(),
                  report_text=f"Сделано: {flow['what']}"[:800], report_done=True)
    # снять открытые дубли этой же задачи, чтобы не висели как «ждёт»
    C.cancel_open_siblings(task["person_id"], task["title"], tid)
    await refresh_card(bot, tid)

    who = person["name"] if person else "?"
    mark = "✅ (с опозданием)" if late else "✅"
    report = (f"{mark} *{who}* закрыл(а) задачу\n"
              f"{task['title']}  `#{tid}`\n\n"
              f"*Сделано:*\n{flow['what']}")
    if late:
        report += f"\n\nСрок был {C.fmt_due(task['due_at'])}"
    chat = hq_chat_id()
    if not chat:
        return
    try:
        await _send_photos(bot, chat, flow.get("photos"),
                           caption=f"Фото к задаче #{tid}")
    except Exception as e:
        logger.error("Фото к закрытию #%s не ушло: %s", tid, e)
    await send_md(bot, chat, report)


# --- перенос срока по просьбе исполнителя ---------------------------------------

def _extend_base(task):
    """От чего считать перенос: от срока задачи, а если он прошёл — от сейчас."""
    now = now_local()
    due = task.get("due_at")
    if due and due.astimezone(LOCAL_TZ) > now:
        return due.astimezone(LOCAL_TZ)
    return now


async def _handle_extension(update, context, action, task):
    query = update.callback_query
    tid = task["id"]
    person = C.person_by_id(task["person_id"])
    emp_chat = task["chat_id"] or (person["chat_id"] if person else None)

    # --- решение владельца в Штабе ---
    if action in ("extok", "extno"):
        if not is_allowed(query.from_user.id):
            await query.answer("Не для вас")
            return
        new_due = task.get("ext_due")
        if action == "extok" and new_due:
            fields = {"due_at": new_due, "ext_due": None,
                      "warned_due": False, "asked_due": False}
            if task["status"] != C.STATUS_NEW:
                fields["told_boss"] = False
            if task["status"] in (C.STATUS_FAILED, C.STATUS_PROBLEM):
                fields["status"] = C.STATUS_TAKEN
            C.task_update(tid, **fields)
            await refresh_card(context.bot, tid)
            await _mark_boss_msg(query, f"✅ Перенос одобрен: {C.fmt_due(new_due)}")
            if emp_chat:
                await context.bot.send_message(
                    chat_id=emp_chat,
                    text=f"✅ Срок перенесён: {C.fmt_due(new_due)}.")
        else:
            C.task_update(tid, ext_due=None)
            await _mark_boss_msg(query, "❌ Перенос отклонён")
            if emp_chat:
                await context.bot.send_message(
                    chat_id=emp_chat,
                    text=f"❌ Перенос отклонён. Срок прежний: {C.fmt_due(task['due_at'])}.")
        return

    # --- сотрудник открыл выбор нового срока ---
    if action == "more":
        await query.answer()
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("+1 час", callback_data=f"crew:mh1:{tid}"),
             InlineKeyboardButton("+2 часа", callback_data=f"crew:mh2:{tid}"),
             InlineKeyboardButton("+4 часа", callback_data=f"crew:mh4:{tid}")],
            [InlineKeyboardButton("Конец дня", callback_data=f"crew:meod:{tid}"),
             InlineKeyboardButton("Завтра", callback_data=f"crew:mtom:{tid}")],
            [InlineKeyboardButton("Отмена", callback_data=f"crew:mcancel:{tid}")]])
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            text=f"{mention(person) if person else ''} на когда перенести "
                 f"«{_short(task['title'])}»?",
            reply_markup=kb)
        return

    if action == "mcancel":
        await query.answer("Отменено")
        try:
            await query.edit_message_text("Перенос отменён.")
        except Exception:
            pass
        return

    # --- сотрудник выбрал новый срок ---
    if action in ("mh1", "mh2", "mh4"):
        new_due = _extend_base(task) + timedelta(hours={"mh1": 1, "mh2": 2, "mh4": 4}[action])
    elif action == "meod":
        now = now_local()
        eod = now.replace(hour=23, minute=59, second=0, microsecond=0)
        new_due = eod if eod > now else eod + timedelta(days=1)
    elif action == "mtom":
        new_due = _extend_base(task) + timedelta(days=1)
    else:
        await query.answer()
        return

    C.task_update(tid, ext_due=new_due)
    await query.answer("Запрос отправлен")
    try:
        await query.edit_message_text(
            f"Запросил перенос на {C.fmt_due(new_due)}. Жду ответа руководителя.")
    except Exception:
        pass
    chat = hq_chat_id()
    if chat:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"crew:extok:{tid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"crew:extno:{tid}")]])
        who = person["name"] if person else "?"
        await context.bot.send_message(
            chat_id=chat,
            text=f"⏳ *{who}* просит перенести «{task['title']}»\n"
                 f"Было: {C.fmt_due(task['due_at'])}\n"
                 f"Просит: {C.fmt_due(new_due)}  `#{tid}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# --- фоновый контроль -----------------------------------------------------------

async def deliver_scheduled(bot):
    """Доставляет отложенные разовые задачи, когда подошло время отправки.
    Отсчёт контроля ведём от доставки, поэтому обновляем created_at."""
    for task in C.tasks_to_deliver():
        person = C.person_by_id(task["person_id"])
        if not person or not person["active"]:
            continue
        C.task_update(task["id"], created_at=now_local())
        try:
            await send_task_card(bot, C.task_get(task["id"]), person)
            logger.info("Отложенная задача #%s доставлена", task["id"])
        except Exception as e:
            logger.error("Отложенная #%s не доставилась: %s", task["id"], e)
        await asyncio.sleep(0.3)


async def spawn_fixed(bot):
    """Ставит постоянные задания, когда подошло их время."""
    now = now_local()
    today = now.date()
    weekday = str(now.isoweekday())

    for fx in C.fix_all():
        md = fx.get("monthday")
        if md:
            # ежемесячное: 99 = последний день, иначе N-е число (в коротких
            # месяцах 31-е сдвигается на последний день).
            last = calendar.monthrange(now.year, now.month)[1]
            target = last if md >= 99 else min(md, last)
            if now.day != target:
                continue
        elif weekday not in fx["weekdays"]:
            continue
        if fx["last_spawn"] == today:
            continue
        if (now.hour, now.minute) < (fx["hour"], fx["minute"]):
            continue

        person = C.person_by_id(fx["person_id"])
        if not person or not person["active"]:
            continue

        due_h = fx["due_hour"] if fx["due_hour"] is not None else C.DEFAULT_DUE_HOUR
        due_m = fx["due_minute"] or 0
        due = now.replace(hour=due_h, minute=due_m, second=0, microsecond=0)
        if due <= now:
            # срок указан раньше времени постановки — значит, имели в виду
            # следующие сутки: «ставить в 23:00, сделать до 02:00»
            due += timedelta(days=1)

        # прошлый незакрытый экземпляр этого задания снимаем — не копим дубли
        C.cancel_open_for_fix(fx["id"])
        tid = C.task_create(person["id"], fx["title"], due, fix_id=fx["id"],
                            priority=fx.get("priority") or 1)
        C.fix_mark_spawned(fx["id"], today)
        try:
            await send_task_card(bot, C.task_get(tid), person)
            logger.info(f"Постоянное задание #{fx['id']} поставлено как #{tid}")
        except Exception as e:
            logger.error(f"Постоянное задание #{fx['id']} не отправилось: {e}")
        await asyncio.sleep(0.5)


async def chase(bot):
    """Сторож без напоминаний: молча ждёт срок. Не закрыл и не заявил
    проблему — провал: штраф по приоритету и сообщение в Штаб. Решение
    принимает crew.decide, здесь только исполнение."""
    now = now_local()

    for task in C.tasks_open():
        # Отложенная задача ещё не доставлена (нет карточки) — не трогаем.
        if not task.get("message_id"):
            continue
        if C.decide(task, now) != C.ACT_FAIL:
            continue

        person = C.person_by_id(task["person_id"])
        if not person:
            continue
        prio = task.get("priority") or 1
        C.task_update(task["id"], told_boss=True, status=C.STATUS_FAILED,
                      penalty=prio)
        await refresh_card(bot, task["id"])
        await tell_boss(
            bot,
            f"❌ Провалено — *{person['name']}* +{prio} штрафной(ых)\n"
            f"{task['title']}\n"
            f"Срок был {C.fmt_due(task['due_at'])}, ответа нет.  "
            f"`#{task['id']}`")
        await asyncio.sleep(0.3)

    # Ждём отчёт, а срок отчёта прошёл — сообщаем владельцу (один раз).
    for task in C.tasks_awaiting_report():
        C.task_update(task["id"], report_nagged=True)
        person = C.person_by_id(task["person_id"])
        await tell_boss(
            bot,
            f"⏰ Нет отчёта по задаче\n"
            f"{task['title']}  `#{task['id']}`\n"
            f"Исполнитель: {person['name'] if person else '?'}\n"
            f"Ждали до {C.fmt_due(task['report_due'])}.")
        await asyncio.sleep(0.3)


async def evening_report(bot):
    """Вечерняя сводка в Штаб — раз в день."""
    now = now_local()
    if now.hour != C.EVENING_REPORT_HOUR:
        return
    today = str(now.date())
    if C.state_get("last_report") == today:
        return
    C.state_set("last_report", today)

    await tell_boss(bot, render_tasks(C.tasks_for_day(), "🌙 Итоги дня"))


async def weekly_report(bot):
    """Недельная сводка в Штаб по всем людям — утром в понедельник, раз в неделю."""
    now = now_local()
    if now.isoweekday() != 1 or now.hour < C.WEEKLY_REPORT_HOUR:
        return
    iso = now.isocalendar()
    tag = f"{iso[0]}-{iso[1]}"          # год-номер недели
    if C.state_get("last_weekly") == tag:
        return
    C.state_set("last_weekly", tag)
    if C.people_all():
        await tell_boss(bot, render_weekly())


async def morning_report(bot):
    """Утренний план на день в Штаб по всем группам — раз в день."""
    now = now_local()
    if now.hour != C.MORNING_REPORT_HOUR:
        return
    today = str(now.date())
    if C.state_get("last_morning") == today:
        return
    C.state_set("last_morning", today)

    await tell_boss(bot, render_tasks(C.tasks_for_day(), "🌅 План на день"))


async def _migrate_cards_once(bot):
    """Один раз перерисовывает все открытые карточки в новый вид — чтобы
    кнопка «Не успеваю» появилась и на задачах, поставленных до её добавления."""
    C.crew_init_db()
    if C.state_get("cards_btn_v2"):
        return
    for task in C.tasks_open():
        await refresh_card(bot, task["id"])
        await asyncio.sleep(0.2)
    C.state_set("cards_btn_v2", "1")
    logger.info("Старые карточки обновлены под новый набор кнопок")


async def _dedup_once():
    """Одноразово схлопывает накопленные дубли заданий/задач."""
    C.crew_init_db()
    if C.state_get("dedup_v1"):
        return
    C.cleanup_duplicates()
    C.state_set("dedup_v1", "1")
    logger.info("Дубли постоянных заданий и задач схлопнуты")


async def crew_loop(bot):
    """Фоновый цикл контроля."""
    await asyncio.sleep(45)
    try:
        await _migrate_cards_once(bot)
        await _dedup_once()
    except Exception:
        logger.exception("Разовая уборка при старте не удалась")
    while True:
        try:
            C.crew_init_db()
            await deliver_scheduled(bot)
            await spawn_fixed(bot)
            await morning_report(bot)
            await chase(bot)
            await evening_report(bot)
            await weekly_report(bot)
        except Exception:
            logger.exception("Контроль задач: сбой цикла")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
