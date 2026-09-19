"""Задачи сотрудникам: постановка, контроль, эскалация.

Устройство простое. У каждого человека своя группа: там он, бот и вы.
Чужих задач человек не видит. Отдельно есть «Штаб» — группа, где только
вы и бот; туда приходят сводки и сообщения о том, кто тормозит.

Задача живёт по шагам: поставлена → взял → сделал. На каждом шаге у бота
есть срок. Не уложились — сначала напоминание в группе человека, потом
сообщение вам. Бот не ругается и не уговаривает: он спрашивает и сообщает.
"""
import logging
import re
from datetime import datetime, timedelta, date

from core import db_conn, now_local, LOCAL_TZ

logger = logging.getLogger(__name__)

# --- сроки контроля -------------------------------------------------------------
# Числа подобраны так, чтобы бот не дёргал по пустякам: полчаса на то, чтобы
# просто увидеть задачу, час — прежде чем беспокоить вас.
TAKE_NUDGE_MIN = 30        # не нажал «Взял» — напоминаем в его группе
TAKE_ESCALATE_MIN = 60     # так и не нажал — сообщаем вам
PRE_DUE_MIN = 60           # за час до срока — предупреждение
DUE_GRACE_MIN = 60         # столько ждём после срока, прежде чем сообщить вам
EVENING_REPORT_HOUR = 20   # час вечерней сводки

STATUS_NEW = "new"
STATUS_TAKEN = "taken"
STATUS_DONE = "done"
STATUS_PROBLEM = "problem"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

STATUS_LABEL = {
    STATUS_NEW: "поставлена",
    STATUS_TAKEN: "в работе",
    STATUS_DONE: "сделано",
    STATUS_PROBLEM: "проблема",
    STATUS_FAILED: "провалена",
    STATUS_CANCELLED: "отменена",
}
STATUS_ICON = {
    STATUS_NEW: "🆕", STATUS_TAKEN: "⚙️", STATUS_DONE: "✅",
    STATUS_PROBLEM: "⚠️", STATUS_FAILED: "❌", STATUS_CANCELLED: "🚫",
}


# --- хранилище ------------------------------------------------------------------

def crew_init_db():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_people (
                id         SERIAL PRIMARY KEY,
                name       TEXT   NOT NULL,
                chat_id    BIGINT NOT NULL,
                tg_user_id BIGINT,
                username   TEXT,
                active     BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (chat_id)
            )""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_crew_people_name "
                    "ON crew_people (lower(name))")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_tasks (
                id          SERIAL PRIMARY KEY,
                person_id   INT    NOT NULL,
                title       TEXT   NOT NULL,
                due_at      TIMESTAMPTZ,
                status      TEXT   NOT NULL DEFAULT 'new',
                note        TEXT,
                fix_id      INT,
                chat_id     BIGINT,
                message_id  BIGINT,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                taken_at    TIMESTAMPTZ,
                done_at     TIMESTAMPTZ,
                nudged_take BOOLEAN NOT NULL DEFAULT FALSE,
                warned_due  BOOLEAN NOT NULL DEFAULT FALSE,
                asked_due   BOOLEAN NOT NULL DEFAULT FALSE,
                told_boss   BOOLEAN NOT NULL DEFAULT FALSE
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_fix (
                id         SERIAL PRIMARY KEY,
                person_id  INT  NOT NULL,
                title      TEXT NOT NULL,
                hour       INT  NOT NULL DEFAULT 9,
                minute     INT  NOT NULL DEFAULT 0,
                weekdays   TEXT NOT NULL DEFAULT '1234567',
                due_hour   INT,
                due_minute INT,
                active     BOOLEAN NOT NULL DEFAULT TRUE,
                last_spawn DATE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        # Служебное: номер группы «Штаб», отметка о вечерней сводке и т.п.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )""")
        # Черновик постановки задачи кнопками: по одному на пользователя.
        # Живёт в базе, а не в памяти, чтобы переживать передеплой Railway.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crew_draft (
                user_id    BIGINT PRIMARY KEY,
                chat_id    BIGINT,
                message_id BIGINT,
                person_id  INT,
                title      TEXT,
                due_at     TIMESTAMPTZ,
                pick_date  DATE,
                week_shift INT NOT NULL DEFAULT 0,
                kind       TEXT,
                weekdays   TEXT,
                hour       INT,
                minute     INT,
                due_hour   INT,
                due_minute INT,
                step       TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
        cur.close()


def state_get(key, default=None):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM crew_state WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else default


def state_set(key, value):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crew_state (key, value) VALUES (%s,%s) "
                    "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                    (key, str(value)))
        cur.close()


PERSON_KEYS = ("id", "name", "chat_id", "tg_user_id", "username", "active")
PERSON_COLS = "id, name, chat_id, tg_user_id, username, active"


def person_save(name, chat_id, tg_user_id=None, username=None):
    """Привязывает группу к человеку. Повторный вызов обновляет привязку."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO crew_people (name, chat_id, tg_user_id, username) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (chat_id) DO UPDATE SET "
            "name=EXCLUDED.name, active=TRUE, "
            "tg_user_id=COALESCE(EXCLUDED.tg_user_id, crew_people.tg_user_id), "
            "username=COALESCE(EXCLUDED.username, crew_people.username) "
            "RETURNING id", (name, chat_id, tg_user_id, username))
        pid = cur.fetchone()[0]
        cur.close()
    return pid


def person_by_name(name):
    if not name:
        return None
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {PERSON_COLS} FROM crew_people "
                    "WHERE lower(name)=lower(%s) AND active", (name.strip(),))
        row = cur.fetchone()
        if not row:
            # неточное совпадение: «эдику», «эдик» -> Эдик
            cur.execute(f"SELECT {PERSON_COLS} FROM crew_people "
                        "WHERE active AND lower(%s) LIKE lower(name) || '%%' "
                        "ORDER BY length(name) DESC LIMIT 1", (name.strip(),))
            row = cur.fetchone()
        cur.close()
    return dict(zip(PERSON_KEYS, row)) if row else None


def person_by_chat(chat_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {PERSON_COLS} FROM crew_people WHERE chat_id=%s", (chat_id,))
        row = cur.fetchone()
        cur.close()
    return dict(zip(PERSON_KEYS, row)) if row else None


def person_by_id(pid):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {PERSON_COLS} FROM crew_people WHERE id=%s", (pid,))
        row = cur.fetchone()
        cur.close()
    return dict(zip(PERSON_KEYS, row)) if row else None


def people_all(only_active=True):
    sql = f"SELECT {PERSON_COLS} FROM crew_people"
    if only_active:
        sql += " WHERE active"
    sql += " ORDER BY name"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    return [dict(zip(PERSON_KEYS, r)) for r in rows]


def person_deactivate(chat_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE crew_people SET active=FALSE WHERE chat_id=%s "
                    "RETURNING name", (chat_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


TASK_KEYS = ("id", "person_id", "title", "due_at", "status", "note", "fix_id",
             "chat_id", "message_id", "created_at", "taken_at", "done_at",
             "nudged_take", "warned_due", "asked_due", "told_boss")
TASK_COLS = ", ".join(TASK_KEYS)


def task_create(person_id, title, due_at, fix_id=None):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO crew_tasks (person_id, title, due_at, fix_id) "
                    "VALUES (%s,%s,%s,%s) RETURNING id",
                    (person_id, title, due_at, fix_id))
        tid = cur.fetchone()[0]
        cur.close()
    return tid


def task_get(task_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {TASK_COLS} FROM crew_tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
        cur.close()
    return dict(zip(TASK_KEYS, row)) if row else None


def task_update(task_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=%s" for k in fields)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE crew_tasks SET {sets} WHERE id=%s",
                    list(fields.values()) + [task_id])
        cur.close()


OPEN_STATUSES = (STATUS_NEW, STATUS_TAKEN, STATUS_PROBLEM)


def tasks_open(person_id=None):
    sql = (f"SELECT {TASK_COLS} FROM crew_tasks WHERE status = ANY(%s)")
    args = [list(OPEN_STATUSES)]
    if person_id:
        sql += " AND person_id=%s"
        args.append(person_id)
    sql += " ORDER BY due_at NULLS LAST, id"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
    return [dict(zip(TASK_KEYS, r)) for r in rows]


def tasks_for_day(day=None):
    """Задачи, у которых срок в этот день, плюс всё открытое просроченное."""
    day = day or now_local().date()
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {TASK_COLS} FROM crew_tasks "
            "WHERE (due_at AT TIME ZONE %s)::date = %s "
            "   OR (status = ANY(%s) AND due_at < now()) "
            "ORDER BY due_at NULLS LAST, id",
            (str(LOCAL_TZ), day, list(OPEN_STATUSES)))
        rows = cur.fetchall()
        cur.close()
    return [dict(zip(TASK_KEYS, r)) for r in rows]


def tasks_overdue():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT {TASK_COLS} FROM crew_tasks "
                    "WHERE status = ANY(%s) AND due_at < now() "
                    "ORDER BY due_at", (list(OPEN_STATUSES),))
        rows = cur.fetchall()
        cur.close()
    return [dict(zip(TASK_KEYS, r)) for r in rows]


def person_stats(person_id, days=30):
    """Сколько сделано вовремя, сколько с опозданием, сколько провалено."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FILTER (WHERE status='done' AND "
            "        (due_at IS NULL OR done_at <= due_at)), "
            "       count(*) FILTER (WHERE status='done' AND done_at > due_at), "
            "       count(*) FILTER (WHERE status='failed'), "
            "       count(*) FILTER (WHERE status = ANY(%s)) "
            "FROM crew_tasks WHERE person_id=%s AND created_at > now() - %s::interval",
            (list(OPEN_STATUSES), person_id, f"{days} days"))
        row = cur.fetchone()
        cur.close()
    return {"on_time": row[0] or 0, "late": row[1] or 0,
            "failed": row[2] or 0, "open": row[3] or 0}


FIX_KEYS = ("id", "person_id", "title", "hour", "minute", "weekdays",
            "due_hour", "due_minute", "active", "last_spawn")
FIX_COLS = ", ".join(FIX_KEYS)


def fix_create(person_id, title, hour, minute, weekdays, due_hour=None, due_minute=None):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO crew_fix (person_id, title, hour, minute, weekdays, "
            "due_hour, due_minute) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (person_id, title, hour, minute, weekdays, due_hour, due_minute))
        fid = cur.fetchone()[0]
        cur.close()
    return fid


def fix_all(only_active=True, person_id=None):
    sql = f"SELECT {FIX_COLS} FROM crew_fix WHERE TRUE"
    args = []
    if only_active:
        sql += " AND active"
    if person_id:
        sql += " AND person_id=%s"
        args.append(person_id)
    sql += " ORDER BY hour, minute, id"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
    return [dict(zip(FIX_KEYS, r)) for r in rows]


def fix_mark_spawned(fix_id, day):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE crew_fix SET last_spawn=%s WHERE id=%s", (day, fix_id))
        cur.close()


def fix_delete(fix_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE crew_fix SET active=FALSE WHERE id=%s RETURNING title",
                    (fix_id,))
        row = cur.fetchone()
        cur.close()
    return row[0] if row else None


# --- черновик постановки задачи кнопками ----------------------------------------

DRAFT_KEYS = ("user_id", "chat_id", "message_id", "person_id", "title", "due_at",
              "pick_date", "week_shift", "kind", "weekdays", "hour", "minute",
              "due_hour", "due_minute", "step")
DRAFT_COLS = ", ".join(DRAFT_KEYS)
DRAFT_STALE_MIN = 30


def _draft_gc(cur):
    """Убирает брошенные черновики: старый диалог не должен перехватывать
    сегодняшнее сообщение как название задачи."""
    cur.execute("DELETE FROM crew_draft "
                "WHERE updated_at < now() - %s::interval",
                (f"{DRAFT_STALE_MIN} minutes",))


def draft_get(user_id):
    with db_conn() as conn:
        cur = conn.cursor()
        _draft_gc(cur)
        cur.execute(f"SELECT {DRAFT_COLS} FROM crew_draft WHERE user_id=%s",
                    (user_id,))
        row = cur.fetchone()
        cur.close()
    return dict(zip(DRAFT_KEYS, row)) if row else None


def draft_reset(user_id, chat_id, step):
    """Начинает новый черновик, затирая старый (новый /menu или выбор человека)."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crew_draft (user_id, chat_id, step, week_shift)
            VALUES (%s,%s,%s,0)
            ON CONFLICT (user_id) DO UPDATE SET
              chat_id=EXCLUDED.chat_id, message_id=NULL, person_id=NULL,
              title=NULL, due_at=NULL, pick_date=NULL, week_shift=0, kind=NULL,
              weekdays=NULL, hour=NULL, minute=NULL, due_hour=NULL,
              due_minute=NULL, step=EXCLUDED.step, updated_at=now()
        """, (user_id, chat_id, step))
        cur.close()


def draft_set(user_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=%s" for k in fields)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE crew_draft SET {sets}, updated_at=now() WHERE user_id=%s",
                    list(fields.values()) + [user_id])
        cur.close()


def draft_clear(user_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM crew_draft WHERE user_id=%s", (user_id,))
        cur.close()


# --- разбор срока из обычной речи -----------------------------------------------

WEEKDAYS = {
    "понедельник": 1, "пн": 1, "вторник": 2, "вт": 2, "среда": 3, "среду": 3,
    "ср": 3, "четверг": 4, "чт": 4, "пятница": 5, "пятницу": 5, "пт": 5,
    "суббота": 6, "субботу": 6, "сб": 6, "воскресенье": 7, "вс": 7,
}

RE_TIME = re.compile(r"\b(?:в|до|к)?\s*(\d{1,2})[:.](\d{2})\b")
RE_HOUR = re.compile(r"\b(?:в|до|к)\s+(\d{1,2})\s*(?:ч|часов|часа)?\b")
RE_IN = re.compile(r"\bчерез\s+(\d{1,3})\s*(минут|мин|час|часа|часов|дня|дней|день)\b")
RE_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")

DEFAULT_DUE_HOUR = 18


def parse_due(text, base=None):
    """Достаёт срок из текста. Возвращает (срок, текст без срока).

    Понимает «до 18:00», «завтра до 12», «через 2 часа», «в пятницу»,
    «25.09 до 14:00». Если срока нет — сегодня 18:00, а если 18:00 уже
    прошло, то завтра: задача без срока не контролируется, а нам надо.

    Куски, опознанные как дата, из строки вычёркиваются перед поиском
    времени. Иначе «25.09» читается как «25 часов 09 минут» и срок теряется.
    """
    base = base or now_local()
    src = (text or "").strip()
    work = " " + src.lower() + " "
    eaten = []

    def consume(m):
        """Вычёркивает разобранный кусок, чтобы он не попался второй раз."""
        nonlocal work
        eaten.append(m.group(0))
        work = work[:m.start()] + " " * (m.end() - m.start()) + work[m.end():]

    # --- «через N ...» перекрывает всё остальное: это срок от текущего момента
    m_in = RE_IN.search(work)
    if m_in:
        n, unit = int(m_in.group(1)), m_in.group(2)
        if unit.startswith("мин"):
            due = base + timedelta(minutes=n)
        elif unit.startswith("час"):
            due = base + timedelta(hours=n)
        else:
            due = base + timedelta(days=n)
        return due, _strip(src, [m_in.group(0)])

    # --- дата: сначала явная, она же вычёркивается из строки
    target_date = None
    m_date = RE_DATE.search(work)
    if m_date:
        d, mo = int(m_date.group(1)), int(m_date.group(2))
        y = int(m_date.group(3) or base.year)
        if y < 100:
            y += 2000
        try:
            target_date = date(y, mo, d)
            consume(m_date)
        except ValueError:
            target_date = None

    day_shift = None
    m_rel = re.search(r"\b(сегодня|завтра|послезавтра)\b", work)
    if m_rel:
        day_shift = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[m_rel.group(1)]
        consume(m_rel)

    weekday_target = None
    if target_date is None and day_shift is None:
        for word, num in WEEKDAYS.items():
            m_wd = re.search(r"\b" + word + r"\b", work)
            if m_wd:
                weekday_target = num
                consume(m_wd)
                break

    # --- время: берём первое осмысленное, мусор вроде «25:09» пропускаем
    hh = mm = None
    for m_t in RE_TIME.finditer(work):
        h, mi = int(m_t.group(1)), int(m_t.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            hh, mm = h, mi
            consume(m_t)
            break
    if hh is None:
        for m_h in RE_HOUR.finditer(work):
            h = int(m_h.group(1))
            if 0 <= h <= 23:
                hh, mm = h, 0
                consume(m_h)
                break

    # --- собираем день
    if target_date is not None:
        day = target_date
    elif weekday_target is not None:
        ahead = (weekday_target - base.isoweekday()) % 7
        day = (base + timedelta(days=ahead or 7)).date()
    elif day_shift is not None:
        day = (base + timedelta(days=day_shift)).date()
    else:
        day = base.date()

    if hh is None:
        hh, mm = DEFAULT_DUE_HOUR, 0

    due = datetime(day.year, day.month, day.day, hh, mm, tzinfo=LOCAL_TZ)
    # срок в прошлом, а день явно не назван — значит, имели в виду завтра
    if due <= base and target_date is None and weekday_target is None \
            and day_shift is None:
        due += timedelta(days=1)

    return due, _strip(src, eaten)


def _strip(text, parts):
    """Убирает из текста куски, опознанные как срок, и чистит хвосты.

    Сравнение по словам, а не посимвольно: в разобранный кусок могли попасть
    лишние пробелы, и точное совпадение тогда не срабатывает, а время
    остаётся торчать в названии задачи.
    """
    out = text or ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        pattern = r"\s+".join(re.escape(tok) for tok in part.split())
        out = re.sub(pattern, " ", out, flags=re.I)
    # висящий предлог в конце: «отчёт в», «сдать до»
    out = re.sub(r"[\s,;]*\b(до|к|в|на)\s*$", "", out.strip(), flags=re.I)
    out = re.sub(r"[\s]+", " ", out)
    return out.strip(" ,;.-—")


RE_EVERY = re.compile(
    r"\bкажд(?:ый|ую|ые)\s+(день|дня|будни|неделю|"
    r"понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b", re.I)


def _all_times(text):
    """Все осмысленные времена в строке, по порядку: [(ч, м, кусок), ...]."""
    found = []
    for m in RE_TIME.finditer(text):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            found.append((h, mi, m.group(0)))
    if not found:
        for m in RE_HOUR.finditer(text):
            h = int(m.group(1))
            if 0 <= h <= 23:
                found.append((h, 0, m.group(0)))
    return found


def parse_fix(text):
    """Разбирает повторяющееся задание.

    «каждый день 9:00» — поставить в 9:00, срок по умолчанию.
    «каждый день 23:00 до 23:59» — поставить в 23:00, сделать до 23:59.
    Второе время, если оно есть, и есть срок: для ночных и сменных задач
    срок по умолчанию (18:00) бессмыслен.

    Возвращает (час, минута, дни недели, час срока, минута срока, текст)
    или None, если повторения в тексте нет.
    """
    low = " " + (text or "").lower() + " "
    m = RE_EVERY.search(low)
    if not m and not re.search(r"\bежедневн|\bпо будням\b", low):
        return None

    word = (m.group(1) if m else "день").lower()
    if word in ("день", "дня") or "ежедневн" in low:
        weekdays = "1234567"
    elif word == "будни" or "по будням" in low:
        weekdays = "12345"
    elif word == "неделю":
        weekdays = str(now_local().isoweekday())
    else:
        weekdays = str(WEEKDAYS.get(word, 1))

    times = _all_times(low)
    hh, mm = (times[0][0], times[0][1]) if times else (9, 0)
    due_h = due_m = None
    if len(times) > 1:
        due_h, due_m = times[1][0], times[1][1]

    parts = [m.group(0)] if m else []
    parts += [t[2] for t in times[:2]]
    parts += ["ежедневно", "по будням"]
    return hh, mm, weekdays, due_h, due_m, _strip(text, parts)


def parse_assignment(text, default_person=None):
    """«Эдик починить бойлер до 18:00» -> (человек, что делать, срок).

    Имя ищем в первом слове: так короче всего писать. Если команда пришла
    в группе самого человека, имя можно не писать вообще.
    """
    text = (text or "").strip()
    if not text:
        return None, "", None

    person = None
    rest = text
    first = text.split()[0].strip(",:").lstrip("@")
    found = person_by_name(first)
    if found:
        person = found
        rest = text[len(text.split()[0]):].strip(" ,:")
    elif default_person:
        person = default_person

    due, title = parse_due(rest)
    return person, title, due


def _has_when(text):
    """Есть ли во фразе явное указание срока или даты. Нужно кнопочному
    диалогу: если человек написал «к 17:00» — берём срок из текста, если нет —
    показываем стрелки. Опирается на те же правила, что и parse_due."""
    low = " " + (text or "").lower() + " "
    if RE_IN.search(low):
        return True
    if re.search(r"\b(сегодня|завтра|послезавтра)\b", low):
        return True
    for word in WEEKDAYS:
        if re.search(r"\b" + word + r"\b", low):
            return True
    m = RE_DATE.search(low)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or now_local().year)
        if y < 100:
            y += 2000
        try:
            date(y, mo, d)
            return True
        except ValueError:
            pass
    return bool(_all_times(low))


def parse_due_explicit(text):
    """Как parse_due, но возвращает (None, текст), если срока во фразе нет —
    тогда его выберут кнопками. Иначе (срок, текст без срока)."""
    if not _has_when(text):
        return None, (text or "").strip()
    return parse_due(text)


# --- тексты ---------------------------------------------------------------------

def fmt_due(due):
    if not due:
        return "без срока"
    now = now_local()
    local = due.astimezone(LOCAL_TZ)
    days = (local.date() - now.date()).days
    when = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get(days)
    if when is None:
        when = local.strftime("%d.%m")
    return f"{when} {local:%H:%M}"


def fmt_overdue(due):
    """«просрочка 3 ч 20 мин»."""
    delta = now_local() - due.astimezone(LOCAL_TZ)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


def task_card(task, person):
    icon = STATUS_ICON.get(task["status"], "•")
    lines = [f"{icon} *{task['title']}*"]

    who = person["name"]
    if person.get("username"):
        who += f" (@{person['username']})"
    lines.append(f"Кому: {who} · Срок: {fmt_due(task['due_at'])}")

    if task["status"] == STATUS_TAKEN:
        lines.append("_взял в работу_")
    elif task["status"] == STATUS_DONE:
        late = (task["due_at"] and task["done_at"] and task["done_at"] > task["due_at"])
        lines.append("_сделано с опозданием_" if late else "_сделано вовремя_")
    elif task["status"] == STATUS_PROBLEM:
        lines.append(f"_проблема: {task['note'] or 'без пояснения'}_")
    elif task["status"] == STATUS_FAILED:
        lines.append("_срок прошёл, ответа нет_")

    lines.append(f"`#{task['id']}`")
    return "\n".join(lines)


# --- правила контроля -----------------------------------------------------------
# Вынесены отдельной функцией сознательно: это и есть вся логика надзора,
# её надо уметь проверить, не поднимая ни базу, ни Telegram.

ACT_NUDGE_TAKE = "nudge_take"        # напомнить в группе: задача не принята
ACT_ESCALATE_TAKE = "escalate_take"  # сказать вам: не принял за час
ACT_WARN_DUE = "warn_due"            # предупредить: час до срока
ACT_ASK_DUE = "ask_due"              # срок прошёл, спросить что по ней
ACT_FAIL = "fail"                    # тишина после срока, сказать вам


def decide(task, now=None):
    """Что бот должен сделать с задачей прямо сейчас. None — ничего.

    Порядок важен: сначала то, что касается принятия задачи, потом срок.
    Каждое действие делается один раз — за этим следят отметки в задаче,
    поэтому функция и смотрит на них, а не только на часы.
    """
    now = now or now_local()
    if task["status"] not in OPEN_STATUSES:
        return None

    age_min = (now - task["created_at"]).total_seconds() / 60

    if task["status"] == STATUS_NEW:
        if not task["nudged_take"] and age_min >= TAKE_NUDGE_MIN:
            return ACT_NUDGE_TAKE
        if not task["told_boss"] and age_min >= TAKE_ESCALATE_MIN:
            return ACT_ESCALATE_TAKE

    if not task["due_at"]:
        return None

    left_min = (task["due_at"] - now).total_seconds() / 60

    if not task["warned_due"] and 0 < left_min <= PRE_DUE_MIN:
        return ACT_WARN_DUE
    if not task["asked_due"] and left_min <= 0:
        return ACT_ASK_DUE
    if not task["told_boss"] and -left_min >= DUE_GRACE_MIN:
        return ACT_FAIL
    return None
