"""Слой данных: схема БД и все операции с таблицами."""
import logging
from datetime import datetime, timedelta
from decimal import Decimal
import psycopg2

from core import (
    db_conn, now_msk, today_msk, to_decimal,
    HISTORY_WINDOW, HISTORY_KEEP, DATABASE_URL,
)

logger = logging.getLogger(__name__)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            deadline TEXT,
            priority TEXT,
            assignee TEXT,
            status TEXT DEFAULT 'Открыта',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            postpone_count INTEGER NOT NULL DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS decisions (
            id SERIAL PRIMARY KEY,
            with_whom TEXT NOT NULL,
            what_decided TEXT NOT NULL,
            deadline TEXT,
            next_step TEXT,
            status TEXT DEFAULT 'Открыта',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS finance (
            id SERIAL PRIMARY KEY,
            amount NUMERIC NOT NULL,
            category TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'расход',
            comment TEXT,
            date DATE DEFAULT CURRENT_DATE,
            currency TEXT NOT NULL DEFAULT 'MDL'
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS preferences (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS reminders (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            sent INTEGER DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS conversation_history (
            id SERIAL PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS apartments (
            id SERIAL PRIMARY KEY,
            address TEXT NOT NULL UNIQUE,
            owner_name TEXT,
            tenant_rent NUMERIC,
            tenant_pay_day INTEGER,
            owner_rent NUMERIC,
            rent_day INTEGER,
            deposit NUMERIC,
            notes TEXT,
            lease_start DATE,
            lease_end DATE,
            lease_end_reminder_sent BOOLEAN DEFAULT false,
            deposit_reminder_sent BOOLEAN DEFAULT false,
            last_collection_reminder_month TEXT,
            manual_paid_month TEXT,
            utilities_fixed NUMERIC,
            floor TEXT,
            unit_number TEXT,
            wifi_login TEXT,
            wifi_password TEXT,
            owner_contacts TEXT,
            tenant_name TEXT,
            tenant_phone TEXT,
            tenant_phone2 TEXT,
            active BOOLEAN DEFAULT true,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS apartment_operations (
            id SERIAL PRIMARY KEY,
            apartment_id INTEGER REFERENCES apartments(id),
            op_date DATE DEFAULT CURRENT_DATE,
            direction TEXT NOT NULL,
            category TEXT NOT NULL,
            counterpart TEXT,
            amount NUMERIC NOT NULL,
            currency TEXT NOT NULL DEFAULT 'MDL',
            payment_method TEXT NOT NULL DEFAULT 'наличные',
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS apartment_balance_checks (
            id SERIAL PRIMARY KEY,
            check_date DATE DEFAULT CURRENT_DATE,
            currency TEXT NOT NULL,
            expected_balance NUMERIC NOT NULL,
            actual_balance NUMERIC NOT NULL,
            difference NUMERIC NOT NULL,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS sop_reminders (
            id SERIAL PRIMARY KEY,
            day_of_month INTEGER NOT NULL,
            text TEXT NOT NULL,
            active BOOLEAN DEFAULT true,
            last_sent_month TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute("SELECT COUNT(*) FROM sop_reminders")
        if c.fetchone()[0] == 0:
            c.executemany(
                "INSERT INTO sop_reminders (day_of_month, text) VALUES (%s, %s)",
                [
                    (10, "Проверить почту — отметить какие фактуры забрали: Старнет, ARAX, Газ, Газ (передача показаний), Свет"),
                    (13, "Заполнить файл «Газ»: посмотреть суммы в банке, запросить у квартирантов фото газовых счётчиков, занести показания в таблицу и оплатить на почте"),
                    (22, "Начать собирать фактуры за месяц"),
                    (23, "Передать показания газа в Энергоком — звонок 1305 (номер договора + показания из файла «Газ»). Обязательно, иначе придут огромные счета!"),
                    (24, "Отправить Михаилу фото фактур и счётчиков по квартирам: Валя Кручий, Дечебал 82/2, Florilor 1/1"),
                    (25, "Оплатить обслуживание дома Трандафирилор 16 — срок до 25 числа включительно!"),
                    (26, "Оплатить интернет: Старнет (почта или банк) и ARAX (только банк)"),
                    (28, "Лев Толстой 27 — фото счётчиков у охранника, просчёт, договориться о встрече на завтра (оплата аренды+коммуналки 29 числа)"),
                    (29, "Срок оплаты оставшихся фактур — до 29 числа (Тестемицану, Садовяну 15/1: счета за свет могут прийти 28-29 числа)"),
                ]
            )
        c.execute('''CREATE TABLE IF NOT EXISTS utility_tariffs (
            utility_type TEXT PRIMARY KEY,
            tariff NUMERIC NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS job_runs (
            job TEXT PRIMARY KEY,
            last_run_date TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS goals (
            id SERIAL PRIMARY KEY,
            text TEXT NOT NULL,
            horizon TEXT NOT NULL DEFAULT 'day',
            period TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            progress TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS maps (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS map_nodes (
            id SERIAL PRIMARY KEY,
            map_id INTEGER NOT NULL REFERENCES maps(id) ON DELETE CASCADE,
            parent_id INTEGER REFERENCES map_nodes(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            note TEXT,
            side TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        try:
            c.execute("ALTER TABLE map_nodes ADD COLUMN IF NOT EXISTS side TEXT")
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS apartment_meters (
            id SERIAL PRIMARY KEY,
            apartment_id INTEGER REFERENCES apartments(id),
            utility_type TEXT NOT NULL,
            last_reading NUMERIC NOT NULL,
            last_reading_date DATE DEFAULT CURRENT_DATE,
            UNIQUE (apartment_id, utility_type)
        )''')
        conn.commit()
    finally:
        conn.close()

    # Миграции для баз, созданных до перехода amount->NUMERIC и добавления type
    conn = psycopg2.connect(DATABASE_URL)
    try:
        c = conn.cursor()
        try:
            c.execute("ALTER TABLE finance ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'расход'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция finance.type пропущена: {e}")
        try:
            c.execute("ALTER TABLE finance ALTER COLUMN amount TYPE NUMERIC USING amount::numeric")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция finance.amount пропущена: {e}")
        try:
            c.execute("ALTER TABLE finance ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'MDL'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция finance.currency пропущена: {e}")
        try:
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_start DATE")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_end DATE")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_end_reminder_sent BOOLEAN DEFAULT false")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS deposit_reminder_sent BOOLEAN DEFAULT false")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS manual_paid_month TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS last_collection_reminder_month TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS utilities_fixed NUMERIC")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция apartments lease-полей пропущена: {e}")
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee TEXT")
            c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP")
            c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS postpone_count INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция tasks.assignee пропущена: {e}")
        try:
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS floor TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS unit_number TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS wifi_login TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS wifi_password TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS owner_contacts TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS tenant_name TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS tenant_phone TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS tenant_phone2 TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS rent_day INTEGER")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS tenant_pay_day INTEGER")
            c.execute("ALTER TABLE apartment_operations ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT 'наличные'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция apartments contact-полей пропущена: {e}")
    finally:
        conn.close()


PRIORITY_ORDER = {"Высокий": 0, "Средний": 1, "Низкий": 2}


def format_deadline(deadline):
    try:
        return datetime.strptime(deadline, "%Y-%m-%d").strftime("%d.%m")
    except (ValueError, TypeError):
        return deadline


def format_task_line(prefix, name, deadline, assignee):
    line = f"{prefix} {name}"
    extra = []
    if deadline:
        extra.append(f"до {format_deadline(deadline)}")
    if assignee:
        extra.append(assignee)
    if extra:
        line += " — " + " · ".join(extra)
    return line


def db_create_task(name, deadline=None, priority=None, assignee=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO tasks (name, deadline, priority, assignee) VALUES (%s, %s, %s, %s)", (name, deadline, priority, assignee))


def db_create_task_if_absent(name, deadline=None):
    """Создаёт задачу, только если открытой задачи с таким же названием ещё нет (защита от дублей SOP)."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM tasks WHERE name=%s AND status != 'Готово' LIMIT 1", (name,))
        if c.fetchone():
            return False
        c.execute("INSERT INTO tasks (name, deadline) VALUES (%s, %s)", (name, deadline))
        return True


def db_get_tasks():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, name, deadline, priority, assignee FROM tasks WHERE status != 'Готово'
                     ORDER BY created_at DESC""")
        rows = c.fetchall()
    if not rows:
        return "Открытых задач нет."

    today = now_msk().strftime("%Y-%m-%d")
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")

    groups = {"Срочно (сегодня и просрочено)": [], "На завтра": [], "Позже": [], "Без срока": []}
    for row in rows:
        deadline = row[2]
        if deadline is None:
            groups["Без срока"].append(row)
        elif deadline <= today:
            groups["Срочно (сегодня и просрочено)"].append(row)
        elif deadline == tomorrow:
            groups["На завтра"].append(row)
        else:
            groups["Позже"].append(row)

    blocks = []
    for title, items in groups.items():
        if not items:
            continue
        items.sort(key=lambda r: (PRIORITY_ORDER.get(r[3], 3), r[2] or ""))
        high = [r for r in items if r[3] == "Высокий"]
        rest = [r for r in items if r[3] != "Высокий"]

        sections = [f"*{title}:*"]
        for emoji, group_items in (("🔴", high), ("🟡", rest)):
            if not group_items:
                continue
            lines = "\n".join(
                format_task_line(f"#{tid}", name, deadline, assignee)
                for (tid, name, deadline, priority, assignee) in group_items
            )
            sections.append(f"{emoji}\n{lines}")
        blocks.append("\n\n".join(sections))
    return "\n\n".join(blocks)


def _task_flags(priority, postpone_count):
    fl = []
    if priority == "Высокий":
        fl.append("высокий приоритет")
    if postpone_count and postpone_count >= 3:
        fl.append(f"откладываешь {postpone_count}×")
    return fl


def db_get_today_tasks():
    """Фокус-вид: главное на сегодня (цели дня) + просроченное + дедлайн сегодня, с пометками почему важно."""
    today = today_msk()
    today_s = today.strftime("%Y-%m-%d")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, name, deadline, priority, postpone_count FROM tasks
                     WHERE status!='Готово' AND deadline IS NOT NULL AND deadline <= %s
                     ORDER BY deadline ASC, created_at""", (today_s,))
        rows = c.fetchall()
        c.execute("""SELECT text, progress FROM goals WHERE status='active' AND horizon='day'
                     ORDER BY created_at""")
        day_goals = c.fetchall()
    overdue = [r for r in rows if str(r[2]) < today_s]
    todays = [r for r in rows if str(r[2]) == today_s]

    def fmt(r, is_overdue):
        tid, name, deadline, priority, pc = r
        reasons = _task_flags(priority, pc)
        if is_overdue:
            try:
                days = (today - datetime.strptime(deadline, "%Y-%m-%d").date()).days
                reasons.insert(0, f"просрочено {days} дн.")
            except (ValueError, TypeError):
                pass
        line = f"- #{tid} {name}"
        if reasons:
            line += " — " + ", ".join(reasons)
        return line

    out = [f"*Сегодня:* {len(todays)} на сегодня, {len(overdue)} просрочено"]
    if day_goals:
        gl = "\n".join(f"- {t}" + (f" (прогресс: {p})" if p else "") for t, p in day_goals)
        out.append(f"\n*🎯 Главное сегодня закрыть:*\n{gl}")
    if overdue:
        out.append("\n*🔴 Просрочено:*\n" + "\n".join(fmt(r, True) for r in overdue))
    if todays:
        out.append("\n*🟠 На сегодня:*\n" + "\n".join(fmt(r, False) for r in todays))
    if not overdue and not todays and not day_goals:
        out.append("\nНа сегодня и просроченных задач нет, сэр. 👍")
    out.append("\n_Полный список — напишите «все задачи» или /alltasks._")
    return "\n".join(out)


def db_get_urgent_tasks():
    with db_conn() as conn:
        c = conn.cursor()
        tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        c.execute("""SELECT name, deadline, assignee FROM tasks
                     WHERE status != 'Готово' AND deadline IS NOT NULL
                     AND deadline <= %s ORDER BY deadline ASC LIMIT 5""", (tomorrow,))
        rows = c.fetchall()
    if not rows:
        return ""
    return "\n".join(
        format_task_line(f"{i}.", name, deadline, assignee)
        for i, (name, deadline, assignee) in enumerate(rows, 1)
    )


def _task_id_from(identifier):
    """Если идентификатор похож на номер (#123 / 123) — вернуть int, иначе None."""
    s = str(identifier).strip().lstrip("#").strip()
    return int(s) if s.isdigit() else None


def db_close_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        task_id = _task_id_from(name_part)
        if task_id is not None:
            c.execute("SELECT name FROM tasks WHERE id=%s AND status != 'Готово'", (task_id,))
            row = c.fetchone()
            if not row:
                return "not_found", []
            c.execute("UPDATE tasks SET status='Готово', closed_at=%s WHERE id=%s", (now_msk().replace(tzinfo=None), task_id))
            return "closed", [row[0]]
        c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [f"#{tid} {name}" for tid, name in rows]
        task_id, name = rows[0]
        c.execute("UPDATE tasks SET status='Готово', closed_at=%s WHERE id=%s", (now_msk().replace(tzinfo=None), task_id))
        return "closed", [name]


def db_delete_task(task_id):
    """Полностью удалить задачу по номеру (для дублей/мусора)."""
    task_id = _task_id_from(task_id)
    if task_id is None:
        return "not_found", None
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM tasks WHERE id=%s RETURNING name", (task_id,))
        row = c.fetchone()
    return ("deleted", row[0]) if row else ("not_found", None)


def db_update_task(name_part, deadline=None, priority=None, assignee=None):
    with db_conn() as conn:
        c = conn.cursor()
        tid = _task_id_from(name_part)
        if tid is not None:
            c.execute("SELECT id, name FROM tasks WHERE id=%s AND status != 'Готово'", (tid,))
        else:
            c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [f"#{tid} {name}" for tid, name in rows]
        task_id, name = rows[0]
        if deadline is not None:
            # Если срок двигают на более позднюю дату — это перенос (прокрастинация), считаем.
            c.execute("SELECT deadline FROM tasks WHERE id=%s", (task_id,))
            old_deadline = c.fetchone()[0]
            postponed = bool(old_deadline) and str(deadline) > str(old_deadline)
            if postponed:
                c.execute("UPDATE tasks SET deadline=%s, postpone_count=postpone_count+1 WHERE id=%s", (deadline, task_id))
            else:
                c.execute("UPDATE tasks SET deadline=%s WHERE id=%s", (deadline, task_id))
        if priority is not None:
            c.execute("UPDATE tasks SET priority=%s WHERE id=%s", (priority, task_id))
        if assignee is not None:
            c.execute("UPDATE tasks SET assignee=%s WHERE id=%s", (assignee, task_id))
        return "updated", [name]


def db_find_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, name, deadline, priority, assignee, status, created_at FROM tasks
                     WHERE name ILIKE %s ORDER BY created_at DESC LIMIT 5""", (f"%{name_part}%",))
        rows = c.fetchall()
    if not rows:
        return "Ничего не найдено."
    result = []
    for tid, name, deadline, priority, assignee, status, created_at in rows:
        line = format_task_line(f"#{tid}", name, deadline, assignee)
        if priority:
            line += f" [{priority}]"
        line += f" [{status}], создана {created_at.strftime('%d.%m.%Y')}"
        result.append(line)
    return "\n".join(result)


def db_create_decision(with_whom, what_decided, deadline=None, next_step=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO decisions (with_whom, what_decided, deadline, next_step) VALUES (%s, %s, %s, %s)",
                  (with_whom, what_decided, deadline, next_step))


def db_get_decisions():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT with_whom, what_decided, deadline FROM decisions WHERE status='Открыта' ORDER BY created_at DESC LIMIT 10")
        rows = c.fetchall()
    if not rows:
        return "Открытых договорённостей нет."
    result = []
    for with_whom, what_decided, deadline in rows:
        line = f"- *{with_whom}*: {what_decided}"
        if deadline:
            line += f" (до {deadline})"
        result.append(line)
    return "\n".join(result)


def db_close_decision(text_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, with_whom, what_decided FROM decisions
                     WHERE status='Открыта' AND (with_whom ILIKE %s OR what_decided ILIKE %s)""",
                  (f"%{text_part}%", f"%{text_part}%"))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [f"{w}: {d}" for _, w, d in rows]
        dec_id, w, d = rows[0]
        c.execute("UPDATE decisions SET status='Готово' WHERE id=%s", (dec_id,))
        return "closed", [f"{w}: {d}"]


def db_create_finance(amount, category, fin_type="расход", comment=None, currency="MDL"):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO finance (amount, category, type, comment, date, currency) VALUES (%s, %s, %s, %s, %s, %s)",
                  (amount, category, fin_type, comment, today_msk(), currency or "MDL"))


def db_get_finance():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT amount, category, type, date, currency FROM finance ORDER BY date DESC, id DESC LIMIT 10")
        rows = c.fetchall()
        month_start = today_msk().replace(day=1)
        c.execute("""SELECT type, currency, COALESCE(SUM(amount), 0) FROM finance
                     WHERE date >= %s GROUP BY type, currency""", (month_start,))
        totals = c.fetchall()
    if not rows:
        return "Финансовых записей нет."
    result = []
    for amount, category, fin_type, date, currency in rows:
        sign = "-" if fin_type == "расход" else "+"
        result.append(f"- {category}: {sign}{amount} {currency or 'MDL'} ({date.strftime('%d.%m')})")
    # Итоги за месяц по каждой валюте
    by_cur = {}
    for fin_type, currency, total in totals:
        cur = currency or "MDL"
        by_cur.setdefault(cur, {"расход": 0, "доход": 0})[fin_type] = total
    if by_cur:
        result.append("\n*Итого в этом месяце:*")
        for cur, d in by_cur.items():
            result.append(f"- {cur}: расходы {d['расход']}, доходы {d['доход']}")
    return "\n".join(result)


# ===== Цели и планирование =====

GOAL_HORIZONS = ("day", "week", "month")
GOAL_TITLES = {"day": "🎯 На день", "week": "📅 На неделю", "month": "🗓 На месяц"}


def goal_period_key(horizon, d=None):
    d = d or today_msk()
    if horizon == "week":
        monday = d - timedelta(days=d.weekday())
        return monday.strftime("%Y-%m-%d")
    if horizon == "month":
        return d.strftime("%Y-%m")
    return d.strftime("%Y-%m-%d")


def db_create_goal(text, horizon="day", period=None):
    horizon = horizon if horizon in GOAL_HORIZONS else "day"
    period = period or goal_period_key(horizon)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO goals (text, horizon, period) VALUES (%s, %s, %s) RETURNING id",
                  (text, horizon, period))
        return c.fetchone()[0]


def db_update_goal(goal_id, progress=None, status=None, text=None):
    gid = _task_id_from(goal_id)
    if gid is None:
        return "not_found"
    sets, vals = [], []
    if progress is not None:
        sets.append("progress=%s"); vals.append(progress)
    if status is not None:
        sets.append("status=%s"); vals.append(status)
    if text is not None:
        sets.append("text=%s"); vals.append(text)
    if not sets:
        return "no_changes"
    sets.append("updated_at=now()")
    vals.append(gid)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id=%s RETURNING id", vals)
        return "updated" if c.fetchone() else "not_found"


def db_delete_goal(goal_id):
    gid = _task_id_from(goal_id)
    if gid is None:
        return "not_found"
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM goals WHERE id=%s RETURNING id", (gid,))
        return "deleted" if c.fetchone() else "not_found"


def db_get_goals(active_only=True):
    with db_conn() as conn:
        c = conn.cursor()
        q = "SELECT id, text, horizon, progress FROM goals"
        if active_only:
            q += " WHERE status='active'"
        q += " ORDER BY CASE horizon WHEN 'day' THEN 0 WHEN 'week' THEN 1 ELSE 2 END, created_at"
        c.execute(q)
        rows = c.fetchall()
    if not rows:
        return "Целей пока нет. Поставьте через чат или я предложу утром, сэр."
    groups = {}
    for gid, text, horizon, progress in rows:
        line = f"- #{gid} {text}"
        if progress:
            line += f" — прогресс: {progress}"
        groups.setdefault(horizon, []).append(line)
    out = []
    for h in GOAL_HORIZONS:
        if h in groups:
            out.append(f"*{GOAL_TITLES[h]}:*\n" + "\n".join(groups[h]))
    return "\n\n".join(out)


def db_get_streak():
    """Сколько дней подряд (заканчивая сегодня/вчера) закрыта хотя бы одна задача."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT closed_at::date FROM tasks WHERE closed_at IS NOT NULL")
        dates = {r[0] for r in c.fetchall()}
    today = today_msk()
    d = today if today in dates else today - timedelta(days=1)
    streak = 0
    while d in dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


def db_get_day_activity():
    """Сводка активности за сегодня (для вечернего разбора-наставничества)."""
    today = today_msk()
    today_s = today.strftime("%Y-%m-%d")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT name FROM tasks WHERE status='Готово' AND closed_at::date=%s ORDER BY closed_at", (today,))
        closed = [r[0] for r in c.fetchall()]
        c.execute("SELECT count(*) FROM tasks WHERE created_at::date=%s", (today,))
        created = c.fetchone()[0]
        c.execute("SELECT name FROM tasks WHERE status!='Готово' AND deadline=%s", (today_s,))
        due_today = [r[0] for r in c.fetchall()]
        c.execute("""SELECT name, deadline FROM tasks WHERE status!='Готово' AND deadline IS NOT NULL
                     AND deadline < %s ORDER BY deadline""", (today_s,))
        overdue = c.fetchall()
        c.execute("""SELECT text, progress FROM goals WHERE status='active'
                     AND updated_at::date=%s AND progress IS NOT NULL""", (today,))
        goals_moved = c.fetchall()
    parts = []
    parts.append(f"Серия (дней подряд с закрытыми задачами): {db_get_streak()}")
    parts.append(f"Закрыто задач сегодня: {len(closed)}" + ((" — " + "; ".join(closed)) if closed else ""))
    parts.append(f"Создано задач сегодня: {created}")
    parts.append(f"Задачи с дедлайном сегодня (ещё открыты): {len(due_today)}" + ((" — " + "; ".join(due_today)) if due_today else ""))
    if overdue:
        parts.append("Просрочено: " + "; ".join(f"{n} (до {d})" for n, d in overdue))
    else:
        parts.append("Просроченных задач нет")
    if goals_moved:
        parts.append("Движение по целям сегодня: " + "; ".join(f"{t} — {p}" for t, p in goals_moved))
    return "\n".join(parts)


APARTMENT_FIELDS = [
    "owner_name", "tenant_rent", "tenant_pay_day", "owner_rent", "rent_day", "deposit", "notes",
    "lease_start", "lease_end", "utilities_fixed",
    "floor", "unit_number", "wifi_login", "wifi_password", "owner_contacts",
    "tenant_name", "tenant_phone", "tenant_phone2",
]


def db_add_apartment(address, **fields):
    cols = ["address"] + APARTMENT_FIELDS
    values = [address] + [fields.get(f) for f in APARTMENT_FIELDS]
    set_clause = ", ".join(f"{f} = COALESCE(EXCLUDED.{f}, apartments.{f})" for f in APARTMENT_FIELDS)
    placeholders = ", ".join(["%s"] * len(cols))
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"""INSERT INTO apartments ({", ".join(cols)}) VALUES ({placeholders})
                     ON CONFLICT (address) DO UPDATE SET
                         {set_clause},
                         lease_end_reminder_sent = CASE
                             WHEN EXCLUDED.lease_end IS NOT NULL AND EXCLUDED.lease_end IS DISTINCT FROM apartments.lease_end
                             THEN false ELSE apartments.lease_end_reminder_sent END,
                         deposit_reminder_sent = CASE
                             WHEN EXCLUDED.lease_end IS NOT NULL AND EXCLUDED.lease_end IS DISTINCT FROM apartments.lease_end
                             THEN false ELSE apartments.deposit_reminder_sent END""",
                  values)


def db_clear_tenant(apartment_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""UPDATE apartments SET tenant_name=NULL, tenant_phone=NULL, tenant_phone2=NULL,
                     tenant_pay_day=NULL, lease_start=NULL, lease_end=NULL, tenant_rent=NULL, deposit=NULL,
                     lease_end_reminder_sent=false, deposit_reminder_sent=false,
                     last_collection_reminder_month=NULL
                     WHERE id=%s""", (apartment_id,))


def db_get_apartments():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT address, owner_name, tenant_rent, tenant_pay_day, owner_rent, rent_day, deposit, lease_start, lease_end, utilities_fixed,
                            floor, unit_number, wifi_login, wifi_password, owner_contacts, tenant_name, tenant_phone, tenant_phone2
                     FROM apartments WHERE active ORDER BY address""")
        rows = c.fetchall()
    if not rows:
        return "Квартир в справочнике нет."
    result = []
    for (address, owner_name, tenant_rent, tenant_pay_day, owner_rent, rent_day, deposit, lease_start, lease_end, utilities_fixed,
         floor, unit_number, wifi_login, wifi_password, owner_contacts, tenant_name, tenant_phone, tenant_phone2) in rows:
        line = f"*{address}*"
        if floor or unit_number:
            line += f"\n  этаж {floor or '?'}, кв. {unit_number or '?'}"
        if owner_name:
            line += f"\n  собственник: {owner_name}"
        if owner_contacts:
            line += f"\n  контакты собственника: {owner_contacts}"
        if owner_rent is not None:
            line += f"\n  сумма аренды: {owner_rent}"
        if rent_day is not None:
            line += f"\n  день аренды: {rent_day}-го числа"
        if tenant_rent is not None:
            line += f"\n  аренда с квартиранта: {tenant_rent}"
        if tenant_pay_day is not None:
            line += f"\n  квартирант платит {tenant_pay_day}-го числа"
        if tenant_rent is not None and owner_rent is not None:
            line += f"\n  маржа: {tenant_rent - owner_rent}"
        if deposit is not None:
            line += f"\n  депозит: {deposit}"
        if lease_start or lease_end:
            line += f"\n  срок: {lease_start.strftime('%d.%m.%Y') if lease_start else '?'} – {lease_end.strftime('%d.%m.%Y') if lease_end else '?'}"
        if tenant_name or tenant_phone:
            tenant_line = tenant_name or "?"
            if tenant_phone:
                tenant_line += f", {tenant_phone}"
            if tenant_phone2:
                tenant_line += f", {tenant_phone2}"
            line += f"\n  квартирант: {tenant_line}"
        if utilities_fixed is not None:
            line += f"\n  фикс. коммуналка: {utilities_fixed}"
        if wifi_login or wifi_password:
            line += f"\n  wifi: {wifi_login or '?'} / {wifi_password or '?'}"
        result.append(line)
    return "\n\n".join(result)


def db_find_apartment(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, address FROM apartments WHERE active AND address ILIKE %s", (f"%{name_part}%",))
        rows = c.fetchall()
    if not rows:
        return "not_found", []
    if len(rows) > 1:
        return "ambiguous", [address for _, address in rows]
    return "found", rows[0]


def db_record_apartment_operation(apartment, direction, category, amount, currency="MDL", counterpart=None, op_date=None, comment=None, payment_method="наличные"):
    apartment_id = None
    apartment_address = None
    if apartment:
        status, info = db_find_apartment(apartment)
        if status == "not_found":
            return "apartment_not_found", apartment
        if status == "ambiguous":
            return "ambiguous", info
        apartment_id, apartment_address = info
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO apartment_operations
                     (apartment_id, op_date, direction, category, counterpart, amount, currency, comment, payment_method)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                  (apartment_id, op_date or today_msk(), direction, category, counterpart, amount, currency, comment, payment_method or "наличные"))
    return "recorded", apartment_address


def db_update_apartment_operation(op_id, **fields):
    """Отредактировать поля существующей операции кассы. Допустимые поля:
    direction, category, counterpart, amount, currency, comment, payment_method, op_date."""
    op_id = _task_id_from(op_id)
    if op_id is None:
        return "not_found"
    allowed = ("direction", "category", "counterpart", "amount", "currency", "comment", "payment_method", "op_date")
    sets, vals = [], []
    for f in allowed:
        if fields.get(f) is not None:
            sets.append(f"{f}=%s")
            vals.append(fields[f])
    if not sets:
        return "no_changes"
    vals.append(op_id)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE apartment_operations SET {', '.join(sets)} WHERE id=%s RETURNING id", vals)
        return "updated" if c.fetchone() else "not_found"


def db_delete_apartment_operation(op_id):
    op_id = _task_id_from(op_id)
    if op_id is None:
        return "not_found"
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM apartment_operations WHERE id=%s RETURNING id", (op_id,))
        return "deleted" if c.fetchone() else "not_found"


def db_get_apartment_balance():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT currency, COALESCE(payment_method, 'наличные'), direction, COALESCE(SUM(amount), 0)
                     FROM apartment_operations GROUP BY currency, COALESCE(payment_method, 'наличные'), direction""")
        rows = c.fetchall()
        c.execute("""SELECT DISTINCT ON (currency) currency, check_date, difference
                     FROM apartment_balance_checks ORDER BY currency, check_date DESC, id DESC""")
        checks = c.fetchall()
    if not rows:
        return "Операций по кассе квартир пока нет."
    balances = {}
    for currency, method, direction, total in rows:
        cur = balances.setdefault(currency, {"приход": 0, "расход": 0, "methods": {}})
        cur[direction] += total
        m = cur["methods"].setdefault(method, {"приход": 0, "расход": 0})
        m[direction] += total
    checks_map = {currency: (check_date, diff) for currency, check_date, diff in checks}
    result = []
    for currency, d in balances.items():
        balance = d["приход"] - d["расход"]
        line = f"*Касса ({currency}): {balance}*\nприход {d['приход']}, расход {d['расход']}"
        methods = d["methods"]
        # Разбивка по способу оплаты, если используется не только наличные
        if len(methods) > 1 or (methods and "наличные" not in methods):
            for method, m in methods.items():
                line += f"\n  • {method}: {m['приход'] - m['расход']}"
        if currency in checks_map:
            check_date, diff = checks_map[currency]
            if diff:
                line += f"\n  последняя сверка {check_date.strftime('%d.%m.%Y')}: расхождение {diff}"
            else:
                line += f"\n  последняя сверка {check_date.strftime('%d.%m.%Y')}: совпало"
        result.append(line)
    return "\n".join(result)


def db_reconcile_apartment_balance(actual_balance, currency="MDL", comment=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT COALESCE(SUM(CASE WHEN direction='приход' THEN amount ELSE -amount END), 0)
                     FROM apartment_operations WHERE currency=%s""", (currency,))
        expected = float(c.fetchone()[0])
        diff = actual_balance - expected
        c.execute("""INSERT INTO apartment_balance_checks (currency, expected_balance, actual_balance, difference, comment)
                     VALUES (%s, %s, %s, %s, %s)""",
                  (currency, expected, actual_balance, diff, comment))
        if diff:
            direction = "приход" if diff > 0 else "расход"
            c.execute("""INSERT INTO apartment_operations (direction, category, counterpart, amount, currency, comment)
                         VALUES (%s, 'Корректировка', 'сверка кассы', %s, %s, %s)""",
                      (direction, abs(diff), currency, comment or f"Корректировка по сверке {now_msk().strftime('%d.%m.%Y')}"))
    return expected, actual_balance, diff


def db_get_apartment_report(apartment=None, category=None, direction=None, date_from=None, date_to=None):
    query = """SELECT o.id, o.op_date, a.address, o.direction, o.category, o.counterpart, o.amount, o.currency, o.comment, COALESCE(o.payment_method, 'наличные')
               FROM apartment_operations o
               LEFT JOIN apartments a ON a.id = o.apartment_id
               WHERE 1=1"""
    params = []
    if apartment:
        query += " AND a.address ILIKE %s"
        params.append(f"%{apartment}%")
    if category:
        query += " AND o.category ILIKE %s"
        params.append(f"%{category}%")
    if direction:
        query += " AND o.direction = %s"
        params.append(direction)
    if date_from:
        query += " AND o.op_date >= %s"
        params.append(date_from)
    if date_to:
        query += " AND o.op_date <= %s"
        params.append(date_to)
    query += " ORDER BY o.op_date DESC, o.id DESC LIMIT 200"
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
    if not rows:
        return "Операций по заданным условиям не найдено."
    result = []
    sums = {}
    for op_id, op_date, address, op_dir, op_cat, counterpart, amount, currency, op_comment, method in rows:
        sign = "+" if op_dir == "приход" else "-"
        line = f"- #{op_id} {op_date.strftime('%d.%m.%Y')} {address or '(без квартиры)'}: {sign}{amount} {currency} [{op_cat}]"
        if method and method != "наличные":
            line += f" ({method})"
        if counterpart:
            line += f" — {counterpart}"
        if op_comment:
            line += f" ({op_comment})"
        result.append(line)
        key = (currency, op_dir)
        sums[key] = sums.get(key, 0) + amount
    result.append("")
    for (currency, op_dir), total in sums.items():
        result.append(f"*Итого {op_dir} ({currency}): {total}*")
    return "\n".join(result)


def db_get_apartments_board(month=None):
    """Данные для таблицы и тепловой карты: по каждой активной квартире —
    квартирант, аренда, день оплаты, конец контракта, оплачено ли за месяц."""
    if month is None:
        month = today_msk().strftime("%Y-%m")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT a.address, a.tenant_name, a.tenant_rent, a.tenant_pay_day, a.lease_end,
                   COALESCE(a.manual_paid_month = %s, false) OR EXISTS(SELECT 1 FROM apartment_operations o WHERE o.apartment_id = a.id
                          AND o.direction='приход' AND o.category ILIKE 'аренда'
                          AND to_char(o.op_date, 'YYYY-MM') = %s) AS rent_paid,
                   EXISTS(SELECT 1 FROM apartment_operations o WHERE o.apartment_id = a.id
                          AND o.direction='приход' AND o.category ILIKE 'коммунал%%'
                          AND to_char(o.op_date, 'YYYY-MM') = %s) AS util_paid
            FROM apartments a
            WHERE a.active
            ORDER BY a.tenant_pay_day NULLS LAST, a.address
        """, (month, month, month))
        return c.fetchall()


def apartment_pay_status(tenant_pay_day, paid, lease_end, today=None):
    """Возвращает (код_цвета, нужен_звонок). Коды: green/red/yellow/grey/none."""
    today = today or today_msk()
    call_due = bool(lease_end) and 0 <= (lease_end - today).days <= 10
    if not tenant_pay_day:
        return "none", call_due  # аренду не берём
    if paid:
        return "green", call_due
    if today.day >= tenant_pay_day:
        return "red", call_due
    if (tenant_pay_day - today.day) <= 10:
        return "yellow", call_due
    return "grey", call_due


_EDITABLE_APT_FIELDS = {
    "tenant_name", "tenant_phone", "tenant_phone2", "tenant_rent", "tenant_pay_day",
    "deposit", "lease_start", "lease_end", "owner_name", "owner_rent", "rent_day",
    "utilities_fixed", "notes",
}


def db_update_apartment_fields(address, fields):
    """Прямое обновление разрешённых полей квартиры (для панели редактирования). Можно ставить и NULL."""
    sets, vals = [], []
    for k, v in fields.items():
        if k in _EDITABLE_APT_FIELDS:
            sets.append(f"{k}=%s")
            vals.append(v)
    if not sets:
        return "no_changes"
    # сброс флагов напоминаний, если поменяли дату окончания контракта
    if "lease_end" in fields:
        sets.append("lease_end_reminder_sent=false")
        sets.append("deposit_reminder_sent=false")
    vals.append(address)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE apartments SET {', '.join(sets)} WHERE address=%s RETURNING id", vals)
        return "updated" if c.fetchone() else "not_found"


def db_set_rent_paid(address, paid):
    """Ручная отметка статуса аренды (ТОЛЬКО цвет плитки, без записей в кассу).
    Ставит/снимает флаг manual_paid_month за текущий месяц."""
    month = today_msk().strftime("%Y-%m")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE apartments SET manual_paid_month=%s WHERE address=%s RETURNING id",
                  (month if paid else None, address))
        if not c.fetchone():
            return "not_found"
    return "paid" if paid else "unpaid"


# ===== Brain map: карты-проекты (центр → блоки → задачи → атомы) =====

def db_create_map(title):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO maps (title) VALUES (%s) RETURNING id", (title,))
        return c.fetchone()[0]


def db_get_maps():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT m.id, m.title, count(n.id)
                     FROM maps m LEFT JOIN map_nodes n ON n.map_id = m.id
                     GROUP BY m.id, m.title ORDER BY m.created_at""")
        return [{"id": i, "title": t, "nodes": n} for i, t, n in c.fetchall()]


def db_rename_map(map_id, title):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE maps SET title=%s WHERE id=%s RETURNING id", (title, _task_id_from(map_id)))
        return "ok" if c.fetchone() else "not_found"


def db_delete_map(map_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM maps WHERE id=%s RETURNING id", (_task_id_from(map_id),))
        return "ok" if c.fetchone() else "not_found"


def db_get_map(map_id):
    mid = _task_id_from(map_id)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, title FROM maps WHERE id=%s", (mid,))
        row = c.fetchone()
        if not row:
            return None
        c.execute("""SELECT id, parent_id, title, note, side FROM map_nodes
                     WHERE map_id=%s ORDER BY created_at""", (mid,))
        nodes = [{"id": i, "parent_id": p, "title": t, "note": nt, "side": sd} for i, p, t, nt, sd in c.fetchall()]
    return {"id": row[0], "title": row[1], "nodes": nodes}


def db_add_map_node(map_id, parent_id, title):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO map_nodes (map_id, parent_id, title) VALUES (%s, %s, %s) RETURNING id",
                  (_task_id_from(map_id), _task_id_from(parent_id) if parent_id else None, title))
        return c.fetchone()[0]


def db_update_map_node(node_id, title=None, note=None, side=None):
    sets, vals = [], []
    if title is not None:
        sets.append("title=%s"); vals.append(title)
    if note is not None:
        sets.append("note=%s"); vals.append(note)
    if side is not None:
        sets.append("side=%s"); vals.append(side or None)  # "" -> авто (NULL)
    if not sets:
        return "no_changes"
    vals.append(_task_id_from(node_id))
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE map_nodes SET {', '.join(sets)} WHERE id=%s RETURNING id", vals)
        return "ok" if c.fetchone() else "not_found"


def db_delete_map_node(node_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM map_nodes WHERE id=%s RETURNING id", (_task_id_from(node_id),))
        return "ok" if c.fetchone() else "not_found"


def db_get_overview():
    """Сводка для хаба brain map (живые цифры по доменам)."""
    today = today_msk()
    today_s = today.strftime("%Y-%m-%d")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT count(*) FROM tasks WHERE status!='Готово'")
        open_tasks = c.fetchone()[0]
        c.execute("SELECT count(*) FROM tasks WHERE status!='Готово' AND deadline=%s", (today_s,))
        today_tasks = c.fetchone()[0]
        c.execute("SELECT count(*) FROM tasks WHERE status!='Готово' AND deadline IS NOT NULL AND deadline<%s", (today_s,))
        overdue = c.fetchone()[0]
        c.execute("SELECT horizon, count(*) FROM goals WHERE status='active' GROUP BY horizon")
        g = dict(c.fetchall())
        c.execute("SELECT count(*) FROM decisions WHERE status='Открыта'")
        decisions = c.fetchone()[0]
        month_start = today.replace(day=1)
        c.execute("SELECT type, currency, COALESCE(SUM(amount),0) FROM finance WHERE date>=%s GROUP BY type, currency", (month_start,))
        fin = c.fetchall()
    unpaid = call = 0
    board = db_get_apartments_board()
    for address, tn, tr, pd, le, rent_paid, util_paid in board:
        code, cd = apartment_pay_status(pd, rent_paid, le)
        if code in ("red", "yellow"):
            unpaid += 1
        if cd:
            call += 1
    by_cur = {}
    for t, cur, total in fin:
        cur = cur or "MDL"
        by_cur.setdefault(cur, {"расход": 0, "доход": 0})[t] = float(total)
    return {
        "tasks": {"open": open_tasks, "today": today_tasks, "overdue": overdue},
        "goals": {"day": g.get("day", 0), "week": g.get("week", 0), "month": g.get("month", 0)},
        "apartments": {"total": len(board), "unpaid": unpaid, "call": call},
        "finance": by_cur,
        "discipline": {"streak": db_get_streak()},
        "decisions": {"open": decisions},
    }


def db_get_tenants_contacts():
    """Список квартирантов с телефоном/телеграмом — для массовой рассылки."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT address, tenant_name, tenant_phone FROM apartments
                     WHERE active AND tenant_name IS NOT NULL AND tenant_phone IS NOT NULL
                     ORDER BY address""")
        return [{"address": a, "tenant_name": t, "tenant_phone": p} for a, t, p in c.fetchall()]


def db_get_apartment_detail(address, month=None):
    """Полные данные квартиры для интерактивной панели (JSON-совместимый dict)."""
    if month is None:
        month = today_msk().strftime("%Y-%m")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, address, owner_name, owner_rent, rent_day, tenant_name, tenant_phone, tenant_phone2,
                            tenant_rent, tenant_pay_day, deposit, lease_start, lease_end, utilities_fixed, notes, manual_paid_month
                     FROM apartments WHERE address=%s""", (address,))
        row = c.fetchone()
        if not row:
            return None
        (aid, address, owner_name, owner_rent, rent_day, tenant_name, tenant_phone, tenant_phone2,
         tenant_rent, tenant_pay_day, deposit, lease_start, lease_end, utilities_fixed, notes, manual_paid_month) = row
        c.execute("SELECT utility_type, last_reading FROM apartment_meters WHERE apartment_id=%s", (aid,))
        meters = {ut: float(r) for ut, r in c.fetchall()}
        c.execute("""SELECT category FROM apartment_operations
                     WHERE apartment_id=%s AND direction='приход' AND to_char(op_date,'YYYY-MM')=%s""",
                  (aid, month))
        cats = [r[0].lower() for r in c.fetchall()]

    def num(x):
        return float(x) if x is not None else None

    def dt(x):
        return x.strftime("%Y-%m-%d") if x else None

    return {
        "address": address, "owner_name": owner_name, "owner_rent": num(owner_rent), "rent_day": rent_day,
        "tenant_name": tenant_name, "tenant_phone": tenant_phone, "tenant_phone2": tenant_phone2,
        "tenant_rent": num(tenant_rent), "tenant_pay_day": tenant_pay_day, "deposit": num(deposit),
        "lease_start": dt(lease_start), "lease_end": dt(lease_end), "utilities_fixed": num(utilities_fixed),
        "notes": notes, "meters": meters,
        "rent_paid": (manual_paid_month == month) or any(c.startswith("аренд") for c in cats),
        "util_paid": any(c.startswith("коммунал") for c in cats),
    }


def db_get_rent_status(month=None):
    """Кто из квартирантов заплатил аренду за месяц, а кто нет (по приходам категории «Аренда»)."""
    if month is None:
        month = today_msk().strftime("%Y-%m")
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT a.address, a.tenant_name, a.tenant_rent, a.tenant_pay_day,
                   EXISTS(SELECT 1 FROM apartment_operations o
                          WHERE o.apartment_id = a.id AND o.direction = 'приход'
                            AND o.category ILIKE 'аренда'
                            AND to_char(o.op_date, 'YYYY-MM') = %s) AS paid
            FROM apartments a
            WHERE a.active AND a.tenant_name IS NOT NULL AND a.tenant_pay_day IS NOT NULL
            ORDER BY a.tenant_pay_day, a.address
        """, (month,))
        rows = c.fetchall()
    if not rows:
        return "Нет квартир с заданной датой оплаты квартиранта."

    def fmt(address, tenant_name, tenant_rent, pay_day):
        line = f"- {address} — {tenant_name}"
        if tenant_rent is not None:
            line += f" — {tenant_rent}"
        line += f" (платит {pay_day}-го)"
        return line

    unpaid = [fmt(a, t, r, d) for a, t, r, d, paid in rows if not paid]
    paid_list = [fmt(a, t, r, d) for a, t, r, d, paid in rows if paid]
    blocks = [f"*Оплата аренды за {month}:*"]
    if unpaid:
        blocks.append(f"\n*Не оплатили ({len(unpaid)}):*\n" + "\n".join(unpaid))
    if paid_list:
        blocks.append(f"\n*Оплатили ({len(paid_list)}):*\n" + "\n".join(paid_list))
    return "\n".join(blocks)


UTILITY_UNITS = {
    "свет": "кВт·ч",
    "газ": "м³",
    "вода": "м³",
    "горячая вода": "м³",
    "отопление": "м³",
}


def db_get_utility_tariffs():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT utility_type, tariff FROM utility_tariffs ORDER BY utility_type")
        rows = c.fetchall()
    if not rows:
        return "Тарифы пока не заданы."
    return "\n".join(f"- {utility_type}: {tariff} MDL/{UTILITY_UNITS.get(utility_type, 'ед.')}" for utility_type, tariff in rows)


def db_set_utility_tariff(utility_type, tariff):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO utility_tariffs (utility_type, tariff) VALUES (%s, %s)
                     ON CONFLICT (utility_type) DO UPDATE SET tariff = EXCLUDED.tariff, updated_at = CURRENT_TIMESTAMP""",
                  (utility_type, tariff))


def db_set_meter_reading(apartment_id, utility_type, reading, reading_date=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO apartment_meters (apartment_id, utility_type, last_reading, last_reading_date)
                     VALUES (%s, %s, %s, COALESCE(%s, CURRENT_DATE))
                     ON CONFLICT (apartment_id, utility_type) DO UPDATE SET
                         last_reading = EXCLUDED.last_reading, last_reading_date = EXCLUDED.last_reading_date""",
                  (apartment_id, utility_type, reading, reading_date))


def db_calculate_utilities(apartment, readings, extra_items=None):
    status, info = db_find_apartment(apartment)
    if status == "not_found":
        return "apartment_not_found", apartment, None, None
    if status == "ambiguous":
        return "ambiguous", info, None, None
    apartment_id, address = info

    with db_conn() as conn:
        c = conn.cursor()
        lines = []
        total = Decimal(0)

        for item in readings:
            utility_type = item["utility_type"]
            new_reading = to_decimal(item["reading"])
            unit = UTILITY_UNITS.get(utility_type, "ед.")
            if new_reading is None:
                lines.append(f"- {utility_type}: не понял показание ({item.get('reading')!r}) — пропущено")
                continue

            c.execute("SELECT last_reading FROM apartment_meters WHERE apartment_id=%s AND utility_type=%s", (apartment_id, utility_type))
            row = c.fetchone()

            if row is None:
                lines.append(f"- {utility_type}: первое показание {new_reading} {unit} — сохранено как базовое, стоимость в этот раз 0")
            else:
                last_reading = row[0]
                diff = new_reading - last_reading
                if diff < 0:
                    lines.append(f"- {utility_type}: новое показание ({new_reading}) меньше прошлого ({last_reading}) — проверьте счётчик, стоимость не посчитана")
                else:
                    c.execute("SELECT tariff FROM utility_tariffs WHERE utility_type=%s", (utility_type,))
                    tariff_row = c.fetchone()
                    if tariff_row is None:
                        lines.append(f"- {utility_type}: {last_reading} → {new_reading} = {diff} {unit}, тариф не задан (используй set_utility_tariff)")
                    else:
                        tariff = tariff_row[0]
                        cost = diff * tariff
                        total += cost
                        lines.append(f"- {utility_type}: {last_reading} → {new_reading} = {diff} {unit} × {tariff} = {cost} MDL")

            c.execute("""INSERT INTO apartment_meters (apartment_id, utility_type, last_reading)
                         VALUES (%s, %s, %s)
                         ON CONFLICT (apartment_id, utility_type) DO UPDATE SET
                             last_reading = EXCLUDED.last_reading, last_reading_date = CURRENT_DATE""",
                      (apartment_id, utility_type, new_reading))

        c.execute("SELECT utilities_fixed FROM apartments WHERE id=%s", (apartment_id,))
        utilities_fixed = c.fetchone()[0]
        if utilities_fixed:
            lines.append(f"- фиксированная часть: {utilities_fixed} MDL")
            total += utilities_fixed

        for extra in (extra_items or []):
            amount = to_decimal(extra.get("amount"))
            if amount is None:
                continue
            lines.append(f"- {extra['description']}: {amount} MDL")
            total += amount

    return "ok", address, lines, total


def db_save_preference(key, value):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO preferences (key, value) VALUES (%s, %s)", (key, value))


def db_get_preferences():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT key, value FROM preferences ORDER BY created_at DESC")
        rows = c.fetchall()
    if not rows:
        return ""
    return "\n".join(f"- {key}: {value}" for key, value in rows)


def db_create_reminder(text, remind_at):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO reminders (text, remind_at) VALUES (%s, %s)", (text, remind_at))


def db_get_pending_reminders():
    with db_conn() as conn:
        c = conn.cursor()
        now = now_msk().strftime("%Y-%m-%d %H:%M")
        c.execute("SELECT id, text FROM reminders WHERE sent=0 AND remind_at <= %s", (now,))
        return c.fetchall()


def db_mark_reminder_sent(reminder_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE reminders SET sent=1 WHERE id=%s", (reminder_id,))


def db_get_sop_reminders():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT day_of_month, text FROM sop_reminders WHERE active ORDER BY day_of_month")
        rows = c.fetchall()
    if not rows:
        return "Напоминаний по SOP нет."
    return "\n".join(f"- {day} числа: {text}" for day, text in rows)


def db_add_sop_reminder(day_of_month, text):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO sop_reminders (day_of_month, text) VALUES (%s, %s)", (day_of_month, text))


def db_remove_sop_reminder(text_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, text FROM sop_reminders WHERE active AND text ILIKE %s", (f"%{text_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [text for _, text in rows]
        rem_id, text = rows[0]
        c.execute("DELETE FROM sop_reminders WHERE id=%s", (rem_id,))
        return "removed", [text]


def db_get_due_sop_reminders(day_of_month, current_month):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, text FROM sop_reminders
                     WHERE active AND day_of_month=%s
                     AND (last_sent_month IS NULL OR last_sent_month != %s)""", (day_of_month, current_month))
        return c.fetchall()


def db_mark_sop_reminder_sent(reminder_id, current_month):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE sop_reminders SET last_sent_month=%s WHERE id=%s", (current_month, reminder_id))


def db_set_setting(key, value):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO settings (key, value) VALUES (%s, %s)
                     ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, str(value)))


def db_get_setting(key, default=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=%s", (key,))
        row = c.fetchone()
    return row[0] if row else default


def db_claim_daily_job(job, today_str):
    """Возвращает True ровно один раз за день для данной задачи (защита от пропусков и дублей)."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""INSERT INTO job_runs (job, last_run_date) VALUES (%s, %s)
                     ON CONFLICT (job) DO UPDATE SET last_run_date = EXCLUDED.last_run_date
                     WHERE job_runs.last_run_date IS DISTINCT FROM EXCLUDED.last_run_date
                     RETURNING job""", (job, today_str))
        return c.fetchone() is not None


def db_get_apartments_for_reminders():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, address, tenant_name, tenant_pay_day, lease_end,
                            lease_end_reminder_sent, deposit_reminder_sent, last_collection_reminder_month
                     FROM apartments WHERE active""")
        return c.fetchall()


def db_set_collection_reminder_sent(apartment_id, current_month):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE apartments SET last_collection_reminder_month=%s WHERE id=%s", (current_month, apartment_id))


def db_set_lease_end_reminder_sent(apartment_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE apartments SET lease_end_reminder_sent=true WHERE id=%s", (apartment_id,))


def db_set_deposit_reminder_sent(apartment_id):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("UPDATE apartments SET deposit_reminder_sent=true WHERE id=%s", (apartment_id,))


def db_save_message(role, content):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO conversation_history (role, content) VALUES (%s, %s)", (role, content))


def db_get_recent_history(limit=HISTORY_WINDOW):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT %s", (limit,))
        rows = c.fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def db_prune_history(keep=HISTORY_KEEP):
    """Чистим старую историю, оставляя последние `keep` записей — чтобы таблица не росла вечно."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""DELETE FROM conversation_history WHERE id NOT IN (
                         SELECT id FROM conversation_history ORDER BY id DESC LIMIT %s)""", (keep,))


def db_clear_history():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM conversation_history")


SELF_DESTRUCT_PHRASE = "УНИЧТОЖИТЬ ВСЁ НАВСЕГДА"
_SELF_DESTRUCT_TABLES = [
    "tasks", "decisions", "finance", "preferences", "reminders", "conversation_history",
    "apartment_operations", "apartment_balance_checks", "apartment_meters", "apartments",
    "sop_reminders", "utility_tariffs", "job_runs", "goals",
]


def db_self_destruct():
    """Полностью и безвозвратно очищает все таблицы (TRUNCATE ... CASCADE), сбрасывает счётчики id.
    Таблицы остаются (пустые), бот продолжает работать с чистого листа."""
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("TRUNCATE TABLE " + ", ".join(_SELF_DESTRUCT_TABLES) + " RESTART IDENTITY CASCADE")


def needs_context(message):
    keywords = ["задач", "список", "что у нас", "покажи", "напомни", "дедлайн",
                "договор", "решени", "финанс", "потратил", "расход", "брифинг",
                "план", "цел", "недел", "месяц", "сегодня", "достич", "итог"]
    return any(k in message.lower() for k in keywords)


def get_db_context():
    tasks = db_get_tasks()
    decisions = db_get_decisions()
    goals = db_get_goals()
    return f"Цели:\n{goals}\n\nЗадачи:\n{tasks}\n\nДоговорённости:\n{decisions}"

