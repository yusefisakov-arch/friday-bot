"""Команды, кнопки и фоновый контроль задач.

Отделено от crew.py сознательно: там данные и правила, здесь разговор с
Telegram. Правила можно проверить тестами, не поднимая ни бота, ни группы.
"""
import asyncio
import logging
from datetime import timedelta

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from core import db_conn, is_allowed, now_local, ALLOWED_USER_ID
import crew as C

logger = logging.getLogger(__name__)

HQ_KEY = "hq_chat_id"
CHECK_EVERY_SECONDS = 180

# Кого бот ждёт с пояснением к «Проблеме»: (chat_id, user_id) -> номер задачи
AWAITING_NOTE = {}


# --- вспомогательное ------------------------------------------------------------

def task_buttons(task):
    if task["status"] in (C.STATUS_DONE, C.STATUS_CANCELLED):
        return None
    row = []
    if task["status"] == C.STATUS_NEW:
        row.append(InlineKeyboardButton("Взял", callback_data=f"crew:take:{task['id']}"))
    row.append(InlineKeyboardButton("Готово", callback_data=f"crew:done:{task['id']}"))
    row.append(InlineKeyboardButton("Проблема", callback_data=f"crew:problem:{task['id']}"))
    return InlineKeyboardMarkup([row])


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
        days = {"1234567": "каждый день", "12345": "по будням"}.get(
            f["weekdays"], "по дням " + f["weekdays"])
        due_txt = (f" → до {f['due_hour']:02d}:{f['due_minute'] or 0:02d}"
                   if f["due_hour"] is not None else "")
        lines.append(f"`#f{f['id']}` *{p['name'] if p else '?'}* — {f['title']}\n"
                     f"       {days} в {f['hour']:02d}:{f['minute']:02d}{due_txt}")
    lines.append("\nУдалить: /fix\\_del 3")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


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
    tasks = C.tasks_for_day()
    if not tasks:
        await update.message.reply_text("На сегодня задач нет.")
        return
    await update.message.reply_text(render_digest(tasks, "Сегодня"),
                                    parse_mode=ParseMode.MARKDOWN)


async def debts_cmd(update, context):
    """/debts — всё просроченное."""
    if not is_allowed(update.effective_user.id):
        return
    C.crew_init_db()
    tasks = C.tasks_overdue()
    if not tasks:
        await update.message.reply_text("Просроченного нет. Редкий день.")
        return
    await update.message.reply_text(render_digest(tasks, "Просрочено", overdue=True),
                                    parse_mode=ParseMode.MARKDOWN)


def render_digest(tasks, title, overdue=False):
    """Сводка по людям: у кого что и в каком состоянии."""
    by_person = {}
    for t in tasks:
        by_person.setdefault(t["person_id"], []).append(t)

    lines = [f"*{title}*", ""]
    for pid, items in by_person.items():
        person = C.person_by_id(pid)
        lines.append(f"*{person['name'] if person else '?'}*")
        for t in items:
            icon = C.STATUS_ICON.get(t["status"], "•")
            tail = C.fmt_due(t["due_at"])
            if t["due_at"] and t["status"] in C.OPEN_STATUSES and \
                    t["due_at"].astimezone(now_local().tzinfo) < now_local():
                tail = f"просрочка {C.fmt_overdue(t['due_at'])}"
            lines.append(f"  {icon} {t['title']} — {tail}  `#{t['id']}`")
        lines.append("")
    return "\n".join(lines).strip()


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


# --- кнопки ---------------------------------------------------------------------

async def crew_button(update, context):
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "crew" or not parts[2].isdigit():
        return
    action, tid = parts[1], int(parts[2])

    C.crew_init_db()
    task = C.task_get(tid)
    if not task:
        await query.answer("Задача не найдена")
        return

    # Кнопку можно нажать только в том чате, куда ушла карточка задачи —
    # защита от нажатий по чужой задаче из другого чата.
    if task["chat_id"] and query.message and query.message.chat_id != task["chat_id"]:
        await query.answer()
        return

    person = C.person_by_id(task["person_id"])
    user = query.from_user

    # Кто нажал — тот и работник этой группы: запоминаем, чтобы упоминать по @
    if person and not person.get("username") and user.username:
        C.person_save(person["name"], person["chat_id"],
                      tg_user_id=user.id, username=user.username)
        person = C.person_by_id(task["person_id"])

    if action == "take":
        C.task_update(tid, status=C.STATUS_TAKEN, taken_at=now_local())
        await query.answer("Записал: взял в работу")
    elif action == "done":
        C.task_update(tid, status=C.STATUS_DONE, done_at=now_local())
        await query.answer("Записал: сделано")
        late = task["due_at"] and now_local() > task["due_at"]
        if late:
            await tell_boss(context.bot,
                            f"✅ *{person['name']}* закрыл с опозданием: {task['title']}\n"
                            f"Срок был {C.fmt_due(task['due_at'])}")
    elif action == "problem":
        C.task_update(tid, status=C.STATUS_PROBLEM)
        AWAITING_NOTE[(query.message.chat_id, user.id)] = tid
        await query.answer("Напишите, что случилось")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            message_thread_id=query.message.message_thread_id,
            text=f"{mention(person) if person else ''} напишите одним сообщением, "
                 f"что мешает по задаче «{task['title']}» — передам.",
            reply_markup=ForceReply(selective=bool(person and person.get("username"))))
    else:
        await query.answer()
        return

    await refresh_card(context.bot, tid)


async def catch_problem_note(update, context):
    """Ловит пояснение к «Проблеме» — следующее сообщение того, кто нажал."""
    msg = update.message
    if not msg or not msg.text:
        return
    # Только в группах и только по задаче — свободного разговора с ботом нет.
    if msg.chat.type not in ("group", "supergroup"):
        return
    key = (msg.chat_id, msg.from_user.id)
    tid = AWAITING_NOTE.pop(key, None)
    if not tid:
        return

    C.crew_init_db()
    task = C.task_get(tid)
    if not task:
        return
    C.task_update(tid, note=msg.text.strip()[:500])
    person = C.person_by_id(task["person_id"])
    await refresh_card(context.bot, tid)
    await msg.reply_text("Передал.")
    await tell_boss(
        context.bot,
        f"⚠️ *{person['name'] if person else '?'}* сообщает о проблеме\n"
        f"Задача: {task['title']}\n"
        f"Срок: {C.fmt_due(task['due_at'])}\n\n"
        f"_{msg.text.strip()[:500]}_")


# --- фоновый контроль -----------------------------------------------------------

async def spawn_fixed(bot):
    """Ставит постоянные задания, когда подошло их время."""
    now = now_local()
    today = now.date()
    weekday = str(now.isoweekday())

    for fx in C.fix_all():
        if weekday not in fx["weekdays"]:
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

        tid = C.task_create(person["id"], fx["title"], due, fix_id=fx["id"])
        C.fix_mark_spawned(fx["id"], today)
        try:
            await send_task_card(bot, C.task_get(tid), person)
            logger.info(f"Постоянное задание #{fx['id']} поставлено как #{tid}")
        except Exception as e:
            logger.error(f"Постоянное задание #{fx['id']} не отправилось: {e}")
        await asyncio.sleep(0.5)


async def chase(bot):
    """Сторож: спрашивает исполнителя, а когда ответа нет — сообщает вам.

    Решение принимает crew.decide, здесь только исполнение: что сказать,
    кому и куда. Так правила надзора проверяются отдельно от Telegram.
    """
    now = now_local()

    for task in C.tasks_open():
        action = C.decide(task, now)
        if not action:
            continue

        person = C.person_by_id(task["person_id"])
        if not person:
            continue
        chat = task["chat_id"] or person["chat_id"]
        who = mention(person)

        async def say(text):
            try:
                await bot.send_message(chat_id=chat, text=text,
                                       parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Не отправилось в группу {chat}: {e}")

        if action == C.ACT_NUDGE_TAKE:
            C.task_update(task["id"], nudged_take=True)
            await say(f"{who}, задача «{task['title']}» ждёт. "
                      f"Нажмите «Взял», когда возьмётесь.")

        elif action == C.ACT_ESCALATE_TAKE:
            C.task_update(task["id"], told_boss=True)
            await tell_boss(bot,
                            f"🕐 *{person['name']}* не принял задачу за час\n"
                            f"{task['title']} · срок {C.fmt_due(task['due_at'])}  "
                            f"`#{task['id']}`")

        elif action == C.ACT_WARN_DUE:
            C.task_update(task["id"], warned_due=True)
            await say(f"{who}, час до срока по задаче «{task['title']}».")

        elif action == C.ACT_ASK_DUE:
            C.task_update(task["id"], asked_due=True)
            await say(f"{who}, срок по задаче «{task['title']}» прошёл. "
                      f"Что по ней? Нажмите «Готово» или «Проблема».")

        elif action == C.ACT_FAIL:
            C.task_update(task["id"], told_boss=True, status=C.STATUS_FAILED)
            await refresh_card(bot, task["id"])
            await tell_boss(
                bot,
                f"❌ Тормозит *{person['name']}*\n"
                f"{task['title']}\n"
                f"Просрочка {C.fmt_overdue(task['due_at'])}, ответа нет.  "
                f"`#{task['id']}`")

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

    tasks = C.tasks_for_day()
    if not tasks:
        await tell_boss(bot, "*Итоги дня*\n\nЗадач на сегодня не было.")
        return

    done = [t for t in tasks if t["status"] == C.STATUS_DONE]
    failed = [t for t in tasks if t["status"] == C.STATUS_FAILED]
    problem = [t for t in tasks if t["status"] == C.STATUS_PROBLEM]
    open_ = [t for t in tasks if t["status"] in (C.STATUS_NEW, C.STATUS_TAKEN)]

    lines = ["*Итоги дня*", "",
             f"Сделано {len(done)} · в работе {len(open_)} · "
             f"проблем {len(problem)} · провалено {len(failed)}"]

    if failed or problem or open_:
        lines.append("")
        for group, label in ((failed, "Провалено"), (problem, "Проблемы"),
                             (open_, "Осталось")):
            if not group:
                continue
            lines.append(f"*{label}*")
            for t in group:
                p = C.person_by_id(t["person_id"])
                lines.append(f"  • {p['name'] if p else '?'} — {t['title']}  `#{t['id']}`")
            lines.append("")

    await tell_boss(bot, "\n".join(lines).strip())


async def crew_loop(bot):
    """Фоновый цикл контроля."""
    await asyncio.sleep(45)
    while True:
        try:
            C.crew_init_db()
            await spawn_fixed(bot)
            await chase(bot)
            await evening_report(bot)
        except Exception:
            logger.exception("Контроль задач: сбой цикла")
        await asyncio.sleep(CHECK_EVERY_SECONDS)
