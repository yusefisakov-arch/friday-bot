import os
import json
import logging
import uuid
import tempfile
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool as pg_pool
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
from aiohttp import web
from anthropic import Anthropic
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from telegram import Update, Bot, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, WebAppInfo
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
if not ALLOWED_USER_ID:
    logger.critical("ALLOWED_USER_ID не задан — бот не будет отвечать никому, пока переменная не настроена!")

DATABASE_URL = os.environ["DATABASE_URL"]
MSK = ZoneInfo("Europe/Moscow")
conversation_history = []

WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

STAFF = {
    "Жанель": "финансы, администрация",
    "Эдик": "исполнитель, руками",
    "Филадельфия": "всё остальное",
}

QUICK_TASK_BUTTON = "📝 Задача"
QUICK_FINANCE_BUTTON = "💰 Финансы"
QUICK_DECISION_BUTTON = "🤝 Договорённость"
VIEW_TASKS_BUTTON = "📋 Задачи"
VIEW_DECISIONS_BUTTON = "📒 Договорённости"
VIEW_FINANCE_BUTTON = "💵 Баланс"
APARTMENT_BUTTON = "🏠 Квартиры"
VIEW_APARTMENT_BALANCE_BUTTON = "🏦 Касса квартир"
MOVE_IN_BUTTON = "🔑 Заселение"

DEADLINE_KEYBOARD = ReplyKeyboardMarkup([["Сегодня", "Завтра"], ["Нет"]], resize_keyboard=True)
PRIORITY_KEYBOARD = ReplyKeyboardMarkup([["Высокий", "Средний", "Низкий"], ["Нет"]], resize_keyboard=True)
ASSIGNEE_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys()), ["Нет"]], resize_keyboard=True)
WITH_WHOM_KEYBOARD = ReplyKeyboardMarkup([list(STAFF.keys())], resize_keyboard=True)
FINANCE_TYPE_KEYBOARD = ReplyKeyboardMarkup([["Расход", "Доход"]], resize_keyboard=True)
SKIP_KEYBOARD = ReplyKeyboardMarkup([["Нет"]], resize_keyboard=True)

if WEBAPP_URL:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [KeyboardButton(QUICK_TASK_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/form")), VIEW_TASKS_BUTTON],
            [KeyboardButton(QUICK_FINANCE_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/finance")), VIEW_FINANCE_BUTTON],
            [KeyboardButton(QUICK_DECISION_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/decisions")), VIEW_DECISIONS_BUTTON],
            [KeyboardButton(APARTMENT_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/apartments")), VIEW_APARTMENT_BALANCE_BUTTON],
            [KeyboardButton(MOVE_IN_BUTTON, web_app=WebAppInfo(url=f"{WEBAPP_URL}/move_in"))],
        ],
        resize_keyboard=True,
    )
else:
    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [QUICK_TASK_BUTTON, VIEW_TASKS_BUTTON],
            [QUICK_FINANCE_BUTTON, VIEW_FINANCE_BUTTON],
            [QUICK_DECISION_BUTTON, VIEW_DECISIONS_BUTTON],
            [APARTMENT_BUTTON, VIEW_APARTMENT_BALANCE_BUTTON],
            [MOVE_IN_BUTTON],
        ],
        resize_keyboard=True,
    )

_pool = pg_pool.SimpleConnectionPool(1, 5, DATABASE_URL)


@contextmanager
def db_conn():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def is_allowed(user_id):
    return ALLOWED_USER_ID != 0 and user_id == ALLOWED_USER_ID


def now_msk():
    return datetime.now(MSK)


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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            date DATE DEFAULT CURRENT_DATE
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
            owner_rent NUMERIC,
            rent_day INTEGER,
            deposit NUMERIC,
            notes TEXT,
            lease_start DATE,
            lease_end DATE,
            lease_end_reminder_sent BOOLEAN DEFAULT false,
            last_collection_reminder_month TEXT,
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
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_start DATE")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_end DATE")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS lease_end_reminder_sent BOOLEAN DEFAULT false")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS last_collection_reminder_month TEXT")
            c.execute("ALTER TABLE apartments ADD COLUMN IF NOT EXISTS utilities_fixed NUMERIC")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Миграция apartments lease-полей пропущена: {e}")
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee TEXT")
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


def format_task_line(name, deadline, assignee):
    line = f"- {name}"
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


def db_get_tasks():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT name, deadline, priority, assignee FROM tasks WHERE status != 'Готово'
                     ORDER BY created_at DESC""")
        rows = c.fetchall()
    if not rows:
        return "Открытых задач нет."

    today = now_msk().strftime("%Y-%m-%d")
    tomorrow = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")

    groups = {"Срочно (сегодня и просрочено)": [], "На завтра": [], "Позже": [], "Без срока": []}
    for row in rows:
        deadline = row[1]
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
        items.sort(key=lambda r: (PRIORITY_ORDER.get(r[2], 3), r[1] or ""))
        lines = "\n".join(format_task_line(name, deadline, assignee) for name, deadline, priority, assignee in items)
        blocks.append(f"*{title}:*\n{lines}")
    return "\n\n".join(blocks)


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
    return "\n".join(format_task_line(name, deadline, assignee) for name, deadline, assignee in rows)


def db_close_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [name for _, name in rows]
        task_id, name = rows[0]
        c.execute("UPDATE tasks SET status='Готово' WHERE id=%s", (task_id,))
        return "closed", [name]


def db_update_task(name_part, deadline=None, priority=None, assignee=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name FROM tasks WHERE name ILIKE %s AND status != 'Готово'", (f"%{name_part}%",))
        rows = c.fetchall()
        if not rows:
            return "not_found", []
        if len(rows) > 1:
            return "ambiguous", [name for _, name in rows]
        task_id, name = rows[0]
        if deadline is not None:
            c.execute("UPDATE tasks SET deadline=%s WHERE id=%s", (deadline, task_id))
        if priority is not None:
            c.execute("UPDATE tasks SET priority=%s WHERE id=%s", (priority, task_id))
        if assignee is not None:
            c.execute("UPDATE tasks SET assignee=%s WHERE id=%s", (assignee, task_id))
        return "updated", [name]


def db_find_task(name_part):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT name, deadline, priority, assignee, status, created_at FROM tasks
                     WHERE name ILIKE %s ORDER BY created_at DESC LIMIT 5""", (f"%{name_part}%",))
        rows = c.fetchall()
    if not rows:
        return "Ничего не найдено."
    result = []
    for name, deadline, priority, assignee, status, created_at in rows:
        line = format_task_line(name, deadline, assignee)
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


def db_create_finance(amount, category, fin_type="расход", comment=None):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO finance (amount, category, type, comment) VALUES (%s, %s, %s, %s)",
                  (amount, category, fin_type, comment))


def db_get_finance():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT amount, category, type, date FROM finance ORDER BY date DESC, id DESC LIMIT 10")
        rows = c.fetchall()
        c.execute("""SELECT type, COALESCE(SUM(amount), 0) FROM finance
                     WHERE date >= date_trunc('month', CURRENT_DATE) GROUP BY type""")
        totals = dict(c.fetchall())
    if not rows:
        return "Финансовых записей нет."
    result = []
    for amount, category, fin_type, date in rows:
        sign = "-" if fin_type == "расход" else "+"
        result.append(f"- {category}: {sign}{amount} ({date.strftime('%d.%m')})")
    expense = totals.get("расход", 0)
    income = totals.get("доход", 0)
    result.append(f"\n*Итого в этом месяце:* расходы {expense}, доходы {income}")
    return "\n".join(result)


APARTMENT_FIELDS = [
    "owner_name", "tenant_rent", "owner_rent", "rent_day", "deposit", "notes",
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
                             THEN false ELSE apartments.lease_end_reminder_sent END""",
                  values)


def db_get_apartments():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT address, owner_name, tenant_rent, owner_rent, rent_day, deposit, lease_start, lease_end, utilities_fixed,
                            floor, unit_number, wifi_login, wifi_password, owner_contacts, tenant_name, tenant_phone, tenant_phone2
                     FROM apartments WHERE active ORDER BY address""")
        rows = c.fetchall()
    if not rows:
        return "Квартир в справочнике нет."
    result = []
    for (address, owner_name, tenant_rent, owner_rent, rent_day, deposit, lease_start, lease_end, utilities_fixed,
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


def db_record_apartment_operation(apartment, direction, category, amount, currency="MDL", counterpart=None, op_date=None, comment=None):
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
                     (apartment_id, op_date, direction, category, counterpart, amount, currency, comment)
                     VALUES (%s, COALESCE(%s, CURRENT_DATE), %s, %s, %s, %s, %s, %s)""",
                  (apartment_id, op_date, direction, category, counterpart, amount, currency, comment))
    return "recorded", apartment_address


def db_get_apartment_balance():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT currency, direction, COALESCE(SUM(amount), 0)
                     FROM apartment_operations GROUP BY currency, direction""")
        rows = c.fetchall()
        c.execute("""SELECT DISTINCT ON (currency) currency, check_date, difference
                     FROM apartment_balance_checks ORDER BY currency, check_date DESC, id DESC""")
        checks = c.fetchall()
    if not rows:
        return "Операций по кассе квартир пока нет."
    balances = {}
    for currency, direction, total in rows:
        d = balances.setdefault(currency, {"приход": 0, "расход": 0})
        d[direction] = total
    checks_map = {currency: (check_date, diff) for currency, check_date, diff in checks}
    result = []
    for currency, d in balances.items():
        balance = d["приход"] - d["расход"]
        line = f"*Касса ({currency}): {balance}*\nприход {d['приход']}, расход {d['расход']}"
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
    query = """SELECT o.op_date, a.address, o.direction, o.category, o.counterpart, o.amount, o.currency, o.comment
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
    for op_date, address, op_dir, op_cat, counterpart, amount, currency, op_comment in rows:
        sign = "+" if op_dir == "приход" else "-"
        line = f"- {op_date.strftime('%d.%m.%Y')} {address or '(без квартиры)'}: {sign}{amount} {currency} [{op_cat}]"
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
        total = 0

        for item in readings:
            utility_type = item["utility_type"]
            new_reading = item["reading"]
            unit = UTILITY_UNITS.get(utility_type, "ед.")

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
            lines.append(f"- {extra['description']}: {extra['amount']} MDL")
            total += extra["amount"]

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


def db_get_apartments_for_reminders():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""SELECT id, address, rent_day, lease_end, lease_end_reminder_sent, last_collection_reminder_month
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


def db_save_message(role, content):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO conversation_history (role, content) VALUES (%s, %s)", (role, content))


def db_get_recent_history(limit=10):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT %s", (limit,))
        rows = c.fetchall()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def db_clear_history():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM conversation_history")


def needs_context(message):
    keywords = ["задач", "список", "что у нас", "покажи", "напомни", "дедлайн",
                "договор", "решени", "финанс", "потратил", "расход", "брифинг", "план"]
    return any(k in message.lower() for k in keywords)


def get_db_context():
    tasks = db_get_tasks()
    decisions = db_get_decisions()
    return f"Задачи:\n{tasks}\n\nДоговорённости:\n{decisions}"


def build_system(context_str=""):
    prefs = db_get_preferences()
    prefs_block = f"\n\nЗапомненные предпочтения сэра:\n{prefs}" if prefs else ""
    current_datetime = now_msk().strftime("%d.%m.%Y %H:%M")
    staff_lines = ", ".join(f"{name} ({role})" for name, role in STAFF.items())
    return f"""Ты FRIDAY — исполнительный ассистент Юсефа, предпринимателя (отели, апартаменты, общепит, крипто).
Сотрудники: {staff_lines}.
При делегировании — создавай задачу с пометкой исполнителя и задачу контроля для Юсефа.

Обращайся к нему "сэр". Говори как доверенный советник — прямо, коротко, без воды. Всегда подтверждай что зафиксировал.
Приоритеты: финансовые риски → просроченные договорённости → зависшие задачи → хаос в планах.
Когда создаёшь задачу с дедлайном — всегда спрашивай: "Напомнить вам за день до дедлайна, сэр?" Если говорит да — ставь напоминание автоматически на 08:00 за день до дедлайна.
Если при закрытии или изменении задачи/договорённости находится несколько подходящих — переспроси сэра, какую именно он имеет в виду, не выбирай сам.
При записи финансов уточняй тип (расход или доход), если это не очевидно из контекста.

Учёт квартир (касса по сдаче квартир в субаренду) — отдельная система от личных финансов (finance), не путай их. Валюта операций по умолчанию MDL (лей); если сэр называет сумму в евро — указывай currency='EUR'. При записи операции по квартире уточняй направление (приход/расход) и категорию (Аренда/Коммуналка/Депозит/Прочее), если не очевидно из контекста. Если адрес квартиры не найден или найдено несколько подходящих — переспроси сэра, не выбирай сам, и предложи добавить квартиру через add_apartment, если её действительно нет в справочнике. Сверку кассы (reconcile_apartment_balance) делай только когда сэр явно называет фактическую сумму на руках.

У квартиры есть два разных понятия аренды: rent_day — постоянное число месяца (1-31), когда обычно собирают показания счётчиков и арендную плату, не меняется при смене квартиранта (задаётся один раз через add_apartment вместе с owner_rent — суммой аренды, которую отдаём собственнику). И lease_start/lease_end/tenant_rent/deposit — данные ТЕКУЩЕГО квартиранта (период проживания, сумма его аренды, депозит), которые обновляются при каждом заселении. Когда сэр сообщает, что заехал новый квартирант "с такого-то по такое-то число", сначала вызови get_apartments, чтобы найти точный адрес этой квартиры как он записан в справочнике (квартира уже должна существовать), и вызови add_apartment с этим же адресом и lease_start/lease_end (формат YYYY-MM-DD), а также tenant_rent/deposit, если сэр их называет — остальные поля не указывай, они не изменятся. Если адрес не нашёлся в справочнике — переспроси сэра, не создавай новую квартиру по неточному адресу. Бот сам каждый день в 8:00 проверяет: за день до rent_day (по числу месяца) — напоминает собрать показания счётчиков и сделать просчёт перед встречей; а в последние 10 дней перед lease_end — напоминает спросить квартиранта про продление или выезд (один раз за контракт). Дополнительно есть регулярные ежемесячные напоминания по SOP (sop_reminders) — фиксированные задачи по числам месяца (фактуры, газ, интернет и т.д.), которые бот тоже сам присылает в 8:00. По просьбе сэра показывай список (get_sop_reminders), добавляй (add_sop_reminder) или убирай (remove_sop_reminder) такие напоминания.

Расчёт коммуналки по счётчикам (calculate_utilities) — сэр называет новые показания (свет/газ/вода, иногда отопление/горячая вода — не у всех квартир), бот сам помнит прошлые показания, считает разницу × тариф и выводит разбивку с итогом. Тарифы (utility_tariffs) единые для всех квартир — если сэр говорит "тариф на газ теперь X" — вызови set_utility_tariff; текущие тарифы — get_utility_tariffs. Если для квартиры/услуги ещё нет сохранённого показания — текущее становится базовым, стоимость в этот раз 0. Фиксированная часть коммуналки (интернет и т.п., apartments.utilities_fixed) добавляется к итогу автоматически — задаётся/обновляется через add_apartment. Разовые статьи "по платёжке" (обслуживание дома, отопление в старых домах, уборка при выселении 500-1000 и т.п.) передавай через extra_items каждый раз отдельно, они не сохраняются. После расчёта, если сэр просит записать итог в кассу — отдельно вызови record_apartment_operation (приход, категория "Коммуналка").

Стиль: простой текст, язык — тот на котором пишет Юсеф. Для выделения важного (итоговые суммы, заголовки разделов в списках/отчётах) можно использовать *жирный* (одна звёздочка с каждой стороны) — Telegram отрендерит это жирным шрифтом. Не используй markdown-таблицы, заголовки с #, ---, двойные звёздочки ** , обратные кавычки и квадратные скобки. Обычные ответы — максимум 3-4 предложения; для списков/отчётов длина может быть больше.

Дата: {current_datetime}{prefs_block}{context_str}"""


async def reply_md(message, text, **kwargs):
    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest:
        await message.reply_text(text, **kwargs)


async def send_md(bot, chat_id, text, **kwargs):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN, **kwargs)
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)


def make_chart(title, chart_type, labels, values):
    fig, ax = plt.subplots(figsize=(6, 4))
    if chart_type == "pie":
        ax.pie(values, labels=labels, autopct="%1.0f%%")
    elif chart_type == "line":
        ax.plot(labels, values, marker="o")
        ax.tick_params(axis="x", rotation=45)
    else:
        ax.bar(labels, values)
        ax.tick_params(axis="x", rotation=45)
    ax.set_title(title)
    fig.tight_layout()
    path = os.path.join(tempfile.gettempdir(), f"chart_{uuid.uuid4().hex}.png")
    fig.savefig(path)
    plt.close(fig)
    return path


def process_message(user_message, system):
    tools = [
        {
            "name": "create_task",
            "description": "Создать задачу",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"},
                    "assignee": {"type": "string", "description": "Кому поручено, если делегируется, например: " + ", ".join(STAFF.keys())}
                },
                "required": ["name"]
            }
        },
        {
            "name": "get_tasks",
            "description": "Получить список задач",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_task",
            "description": "Закрыть задачу как выполненную",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "update_task",
            "description": "Изменить дедлайн, приоритет и/или исполнителя существующей открытой задачи",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name_part": {"type": "string"},
                    "deadline": {"type": "string", "description": "YYYY-MM-DD"},
                    "priority": {"type": "string", "description": "Высокий, Средний, Низкий"},
                    "assignee": {"type": "string", "description": "Кому поручено, например: " + ", ".join(STAFF.keys())}
                },
                "required": ["name_part"]
            }
        },
        {
            "name": "find_task",
            "description": "Найти задачу по части названия, включая уже закрытые. Используй чтобы проверить статус или историю задачи",
            "input_schema": {
                "type": "object",
                "properties": {"name_part": {"type": "string"}},
                "required": ["name_part"]
            }
        },
        {
            "name": "create_decision",
            "description": "Записать договорённость",
            "input_schema": {
                "type": "object",
                "properties": {
                    "with_whom": {"type": "string"},
                    "what_decided": {"type": "string"},
                    "deadline": {"type": "string"},
                    "next_step": {"type": "string"}
                },
                "required": ["with_whom", "what_decided"]
            }
        },
        {
            "name": "get_decisions",
            "description": "Получить список открытых договорённостей",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "close_decision",
            "description": "Отметить договорённость выполненной",
            "input_schema": {
                "type": "object",
                "properties": {"text_part": {"type": "string", "description": "Часть текста договорённости или имени, с кем договорились"}},
                "required": ["text_part"]
            }
        },
        {
            "name": "create_finance",
            "description": "Записать расход или доход",
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Сумма, положительное число"},
                    "category": {"type": "string"},
                    "type": {"type": "string", "enum": ["расход", "доход"], "description": "расход или доход"},
                    "comment": {"type": "string"}
                },
                "required": ["amount", "category", "type"]
            }
        },
        {
            "name": "get_finance",
            "description": "Получить финансовые записи и итоги за месяц",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "add_apartment",
            "description": "Добавить квартиру в справочник учёта квартир, или обновить данные существующей (по адресу)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "address": {"type": "string"},
                    "owner_name": {"type": "string"},
                    "tenant_rent": {"type": "number", "description": "Сумма аренды, которую платит текущий квартирант (обычно задаётся при заселении)"},
                    "owner_rent": {"type": "number", "description": "Сумма аренды, которую отдаём собственнику — постоянная для квартиры"},
                    "rent_day": {"type": "integer", "description": "Число месяца (1-31), когда нужно собирать показания счётчиков и арендную плату. Постоянное для квартиры, не меняется при смене квартиранта"},
                    "deposit": {"type": "number", "description": "Депозит текущего квартиранта (обычно задаётся при заселении)"},
                    "notes": {"type": "string"},
                    "lease_start": {"type": "string", "description": "Дата заезда текущего квартиранта, YYYY-MM-DD"},
                    "lease_end": {"type": "string", "description": "Дата выезда текущего квартиранта, YYYY-MM-DD"},
                    "utilities_fixed": {"type": "number", "description": "Фиксированная часть коммуналки в месяц (интернет и т.п.), автоматически прибавляется при расчёте через calculate_utilities"},
                    "floor": {"type": "string", "description": "Этаж"},
                    "unit_number": {"type": "string", "description": "Номер квартиры"},
                    "wifi_login": {"type": "string"},
                    "wifi_password": {"type": "string"},
                    "owner_contacts": {"type": "string", "description": "Телефон/телеграм собственника"}
                },
                "required": ["address"]
            }
        },
        {
            "name": "get_apartments",
            "description": "Получить справочник квартир (адрес, собственник, аренда, маржа, депозит)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "record_apartment_operation",
            "description": "Записать операцию в кассу по квартирам (приход или расход денег)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string", "description": "Адрес или часть адреса квартиры. Можно не указывать для общих операций"},
                    "direction": {"type": "string", "enum": ["приход", "расход"]},
                    "category": {"type": "string", "description": "Аренда, Коммуналка, Депозит, Прочее и т.п."},
                    "amount": {"type": "number"},
                    "currency": {"type": "string", "description": "MDL или EUR, по умолчанию MDL"},
                    "counterpart": {"type": "string", "description": "Квартирант, Собственник, название провайдера и т.п."},
                    "date": {"type": "string", "description": "YYYY-MM-DD, по умолчанию сегодня"},
                    "comment": {"type": "string"}
                },
                "required": ["direction", "category", "amount"]
            }
        },
        {
            "name": "get_apartment_balance",
            "description": "Получить расчётный баланс кассы по квартирам и результат последней сверки",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "reconcile_apartment_balance",
            "description": "Сверить расчётный баланс кассы по квартирам с фактической суммой на руках. Вызывай только когда сэр явно называет фактическую сумму",
            "input_schema": {
                "type": "object",
                "properties": {
                    "actual_balance": {"type": "number"},
                    "currency": {"type": "string", "description": "MDL или EUR, по умолчанию MDL"},
                    "comment": {"type": "string"}
                },
                "required": ["actual_balance"]
            }
        },
        {
            "name": "get_apartment_report",
            "description": "Получить операции по кассе квартир с фильтрами — для произвольных отчётов",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string"},
                    "category": {"type": "string"},
                    "direction": {"type": "string", "enum": ["приход", "расход"]},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"}
                }
            }
        },
        {
            "name": "generate_chart",
            "description": "Построить картинку-график по готовым данным (например, из get_apartment_report) для наглядного отчёта",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "chart_type": {"type": "string", "enum": ["bar", "pie", "line"]},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "array", "items": {"type": "number"}}
                },
                "required": ["title", "chart_type", "labels", "values"]
            }
        },
        {
            "name": "get_utility_tariffs",
            "description": "Получить текущие тарифы на коммунальные услуги (свет, газ, вода и т.п.)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "set_utility_tariff",
            "description": "Задать или обновить тариф на коммунальную услугу (единый для всех квартир)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "utility_type": {"type": "string", "description": "свет, газ, вода, отопление, горячая вода и т.п."},
                    "tariff": {"type": "number", "description": "Цена за единицу (MDL за кВт·ч или м³)"}
                },
                "required": ["utility_type", "tariff"]
            }
        },
        {
            "name": "calculate_utilities",
            "description": "Рассчитать коммуналку для квартиры по новым показаниям счётчиков. Бот сам помнит прошлые показания и считает разницу × тариф, плюс фиксированную часть квартиры и доп. статьи",
            "input_schema": {
                "type": "object",
                "properties": {
                    "apartment": {"type": "string", "description": "Адрес или часть адреса квартиры"},
                    "readings": {
                        "type": "array",
                        "description": "Новые показания счётчиков",
                        "items": {
                            "type": "object",
                            "properties": {
                                "utility_type": {"type": "string", "description": "свет, газ, вода, отопление, горячая вода и т.п."},
                                "reading": {"type": "number"}
                            },
                            "required": ["utility_type", "reading"]
                        }
                    },
                    "extra_items": {
                        "type": "array",
                        "description": "Разовые дополнительные статьи: обслуживание дома по платёжке, отопление по платёжке, уборка при выселении и т.п.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "amount": {"type": "number"}
                            },
                            "required": ["description", "amount"]
                        }
                    }
                },
                "required": ["apartment", "readings"]
            }
        },
        {
            "name": "save_preference",
            "description": "Запомнить предпочтение или важную информацию",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"}
                },
                "required": ["key", "value"]
            }
        },
        {
            "name": "create_reminder",
            "description": "Создать напоминание на конкретное время. Используй когда говорят 'напомни через X' или 'напомни в X'",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Текст напоминания"},
                    "remind_at": {"type": "string", "description": "Время в формате YYYY-MM-DD HH:MM"}
                },
                "required": ["text", "remind_at"]
            }
        },
        {
            "name": "get_sop_reminders",
            "description": "Получить список регулярных ежемесячных напоминаний по сопровождению квартир (SOP)",
            "input_schema": {"type": "object", "properties": {}}
        },
        {
            "name": "add_sop_reminder",
            "description": "Добавить новое регулярное ежемесячное напоминание по сопровождению квартир (SOP)",
            "input_schema": {
                "type": "object",
                "properties": {
                    "day_of_month": {"type": "integer", "description": "День месяца, 1-31"},
                    "text": {"type": "string"}
                },
                "required": ["day_of_month", "text"]
            }
        },
        {
            "name": "remove_sop_reminder",
            "description": "Убрать регулярное напоминание по SOP по части текста",
            "input_schema": {
                "type": "object",
                "properties": {"text_part": {"type": "string"}},
                "required": ["text_part"]
            }
        }
    ]

    messages = conversation_history
    text = ""
    chart_path = None
    for _ in range(5):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=system,
            messages=messages,
            tools=tools
        )

        text = "".join(b.text for b in response.content if hasattr(b, "text"))
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            break

        tool_results = []
        for block in tool_uses:
            inp = block.input
            result = ""
            if block.name == "create_task":
                db_create_task(inp["name"], inp.get("deadline"), inp.get("priority"), inp.get("assignee"))
                result = f"Задача создана: {inp['name']}"
            elif block.name == "get_tasks":
                result = db_get_tasks()
            elif block.name == "close_task":
                status, items = db_close_task(inp["name_part"])
                if status == "closed":
                    result = f"Задача закрыта: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих задач, уточни у сэра какую закрыть:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Задача не найдена"
            elif block.name == "update_task":
                status, items = db_update_task(inp["name_part"], inp.get("deadline"), inp.get("priority"), inp.get("assignee"))
                if status == "updated":
                    result = f"Задача обновлена: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих задач, уточни у сэра какую менять:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Задача не найдена"
            elif block.name == "find_task":
                result = db_find_task(inp["name_part"])
            elif block.name == "create_decision":
                db_create_decision(inp["with_whom"], inp["what_decided"], inp.get("deadline"), inp.get("next_step"))
                result = f"Записано: {inp['with_whom']} — {inp['what_decided']}"
            elif block.name == "get_decisions":
                result = db_get_decisions()
            elif block.name == "close_decision":
                status, items = db_close_decision(inp["text_part"])
                if status == "closed":
                    result = f"Договорённость закрыта: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих договорённостей, уточни у сэра какую закрыть:\n" + "\n".join(f"- {n}" for n in items)
                else:
                    result = "Договорённость не найдена"
            elif block.name == "create_finance":
                db_create_finance(inp["amount"], inp["category"], inp.get("type", "расход"), inp.get("comment"))
                sign = "-" if inp.get("type", "расход") == "расход" else "+"
                result = f"Записано: {inp['category']} {sign}{inp['amount']}"
            elif block.name == "get_finance":
                result = db_get_finance()
            elif block.name == "add_apartment":
                db_add_apartment(inp["address"], **{f: inp.get(f) for f in APARTMENT_FIELDS})
                result = f"Квартира сохранена: {inp['address']}"
            elif block.name == "get_apartments":
                result = db_get_apartments()
            elif block.name == "record_apartment_operation":
                status, info = db_record_apartment_operation(
                    inp.get("apartment"), inp["direction"], inp["category"], inp["amount"],
                    inp.get("currency", "MDL"), inp.get("counterpart"), inp.get("date"), inp.get("comment")
                )
                if status == "recorded":
                    sign = "+" if inp["direction"] == "приход" else "-"
                    address_part = f" ({info})" if info else ""
                    result = f"Записано в кассу квартир{address_part}: {sign}{inp['amount']} {inp.get('currency', 'MDL')} [{inp['category']}]"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих квартир, уточни у сэра какую он имеет в виду:\n" + "\n".join(f"- {a}" for a in info)
                else:
                    result = f"Квартира '{info}' не найдена в справочнике. Уточни у сэра адрес или предложи добавить квартиру через add_apartment"
            elif block.name == "get_apartment_balance":
                result = db_get_apartment_balance()
            elif block.name == "reconcile_apartment_balance":
                currency = inp.get("currency", "MDL")
                expected, actual, diff = db_reconcile_apartment_balance(inp["actual_balance"], currency, inp.get("comment"))
                if diff == 0:
                    result = f"Сверка ({currency}): расчётный баланс {expected} совпал с фактическим {actual}."
                else:
                    result = f"Сверка ({currency}): расчётный баланс {expected}, по факту {actual}, расхождение {diff}. Записал корректирующую операцию."
            elif block.name == "get_apartment_report":
                result = db_get_apartment_report(inp.get("apartment"), inp.get("category"), inp.get("direction"), inp.get("date_from"), inp.get("date_to"))
            elif block.name == "generate_chart":
                chart_path = make_chart(inp["title"], inp["chart_type"], inp["labels"], inp["values"])
                result = "График построен, будет отправлен сэру отдельным сообщением."
            elif block.name == "get_utility_tariffs":
                result = db_get_utility_tariffs()
            elif block.name == "set_utility_tariff":
                db_set_utility_tariff(inp["utility_type"], inp["tariff"])
                result = f"Тариф обновлён: {inp['utility_type']} — {inp['tariff']} MDL/{UTILITY_UNITS.get(inp['utility_type'], 'ед.')}"
            elif block.name == "calculate_utilities":
                status, info, lines, total = db_calculate_utilities(inp["apartment"], inp["readings"], inp.get("extra_items"))
                if status == "ok":
                    result = f"Расчёт коммуналки для {info}:\n" + "\n".join(lines) + f"\n\nИТОГО: {total} MDL"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих квартир, уточни у сэра какую он имеет в виду:\n" + "\n".join(f"- {a}" for a in info)
                else:
                    result = f"Квартира '{info}' не найдена в справочнике. Уточни у сэра адрес или предложи добавить квартиру через add_apartment"
            elif block.name == "save_preference":
                db_save_preference(inp["key"], inp["value"])
                result = f"Запомнено: {inp['key']}"
            elif block.name == "create_reminder":
                db_create_reminder(inp["text"], inp["remind_at"])
                result = f"Напоминание установлено на {inp['remind_at']}"
            elif block.name == "get_sop_reminders":
                result = db_get_sop_reminders()
            elif block.name == "add_sop_reminder":
                db_add_sop_reminder(inp["day_of_month"], inp["text"])
                result = f"Напоминание добавлено на {inp['day_of_month']} число: {inp['text']}"
            elif block.name == "remove_sop_reminder":
                status, items = db_remove_sop_reminder(inp["text_part"])
                if status == "removed":
                    result = f"Напоминание убрано: {items[0]}"
                elif status == "ambiguous":
                    result = "Нашлось несколько подходящих напоминаний, уточни у сэра какое убрать:\n" + "\n".join(f"- {t}" for t in items)
                else:
                    result = "Напоминание не найдено"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages = messages + [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": tool_results}
        ]

    return (text or "Готово, сэр."), chart_path


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
    for reminder_id, _ in due:
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

        if now.hour == 8 and now.minute == 0:
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

        if now.hour == 21 and now.minute == 0:
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


async def get_staff(request):
    return web.json_response(list(STAFF.keys()))


async def get_apartments_api(request):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT address FROM apartments WHERE active ORDER BY address")
        rows = c.fetchall()
    return web.json_response([address for (address,) in rows])


async def health(request):
    return web.json_response({"status": "ok"})


async def run_webapp_server():
    app = web.Application()
    app.router.add_get("/form", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "form.html")))
    app.router.add_get("/finance", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "finance.html")))
    app.router.add_get("/decisions", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "decisions.html")))
    app.router.add_get("/apartments", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "apartments.html")))
    app.router.add_get("/move_in", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "move_in.html")))
    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "form.html")))
    app.router.add_get("/api/staff", get_staff)
    app.router.add_get("/api/apartments", get_apartments_api)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер форм запущен на порту {PORT}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("FRIDAY на связи, сэр. Готов к работе.", reply_markup=MAIN_KEYBOARD)


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    global conversation_history
    conversation_history = []
    db_clear_history()
    await update.message.reply_text("История очищена, сэр.")


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await reply_md(update.message, db_get_tasks())


async def decisions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await reply_md(update.message, db_get_decisions())


async def finance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await reply_md(update.message, db_get_finance())


async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    prefs = db_get_preferences()
    if prefs:
        await update.message.reply_text(f"Что я о вас знаю, сэр:\n{prefs}")
    else:
        await update.message.reply_text("Пока ничего не запомнено, сэр.")


def create_quick_task(name, deadline=None, priority=None, assignee=None):
    db_create_task(name, deadline, priority, assignee)
    summary = f"Записал, сэр: {name}"
    if deadline:
        summary += f" (дедлайн: {deadline})"
    if priority:
        summary += f" [{priority}]"
    if assignee:
        summary += f" [{assignee}]"
    return summary


def create_quick_finance(amount, category, fin_type="расход", comment=None):
    db_create_finance(amount, category, fin_type, comment)
    sign = "-" if fin_type == "расход" else "+"
    summary = f"Записал, сэр: {category} {sign}{amount}"
    if comment:
        summary += f" ({comment})"
    return summary


def create_quick_apartment(address, **fields):
    db_add_apartment(address, **fields)
    summary = f"Квартира сохранена, сэр: {address}"
    tenant_rent, owner_rent = fields.get("tenant_rent"), fields.get("owner_rent")
    if tenant_rent is not None and owner_rent is not None:
        summary += f" (маржа: {tenant_rent - owner_rent})"
    rent_day = fields.get("rent_day")
    if rent_day:
        summary += f", аренда {rent_day}-го числа каждый месяц"
    lease_start, lease_end = fields.get("lease_start"), fields.get("lease_end")
    if lease_start or lease_end:
        summary += f", срок: {lease_start or '?'} – {lease_end or '?'}"
    return summary


def create_quick_move_in(apartment, tenant_name=None, tenant_phone=None, tenant_phone2=None, lease_start=None, lease_end=None, tenant_rent=None, deposit=None, meters=None):
    status, info = db_find_apartment(apartment)
    if status == "not_found":
        return f"Квартира '{apartment}' не найдена в справочнике, сэр."
    if status == "ambiguous":
        return "Нашлось несколько подходящих квартир: " + ", ".join(info) + ". Уточните, сэр."
    apartment_id, address = info
    db_add_apartment(address, tenant_name=tenant_name, tenant_phone=tenant_phone, tenant_phone2=tenant_phone2,
                     lease_start=lease_start, lease_end=lease_end, tenant_rent=tenant_rent, deposit=deposit)
    for utility_type, reading in (meters or {}).items():
        db_set_meter_reading(apartment_id, utility_type, reading, lease_start)
    summary = f"Заселение записано, сэр: {address}"
    if tenant_name:
        summary += f" — {tenant_name}"
    if lease_start or lease_end:
        summary += f", срок: {lease_start or '?'} – {lease_end or '?'}"
    if tenant_rent is not None:
        summary += f", аренда {tenant_rent}"
    if deposit is not None:
        summary += f", депозит {deposit}"
    if meters:
        summary += ". Показания на момент заселения: " + ", ".join(f"{k} {v}" for k, v in meters.items())
    return summary


def create_quick_apartment_operation(apartment, direction, category, amount, currency="MDL", counterpart=None, op_date=None, comment=None):
    status, info = db_record_apartment_operation(apartment, direction, category, amount, currency, counterpart, op_date, comment)
    if status == "recorded":
        sign = "+" if direction == "приход" else "-"
        address_part = f" ({info})" if info else ""
        summary = f"Записал в кассу квартир{address_part}, сэр: {sign}{amount} {currency} [{category}]"
        if comment:
            summary += f" ({comment})"
        return summary
    if status == "ambiguous":
        return "Нашлось несколько подходящих квартир: " + ", ".join(info) + ". Уточните, сэр."
    return f"Квартира '{info}' не найдена в справочнике, сэр. Добавьте её сначала через форму или чат."


def create_quick_decision(with_whom, what_decided, deadline=None, next_step=None):
    db_create_decision(with_whom, what_decided, deadline, next_step)
    summary = f"Записал, сэр: {with_whom} — {what_decided}"
    if deadline:
        summary += f" (до {deadline})"
    if next_step:
        summary += f". Следующий шаг: {next_step}"
    return summary


def resolve_deadline(value):
    if value == "today":
        return now_msk().strftime("%Y-%m-%d")
    if value == "tomorrow":
        return (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    return value or None


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except (TypeError, ValueError, AttributeError):
        await update.message.reply_text("Не понял форму, сэр. Попробуйте ещё раз.", reply_markup=MAIN_KEYBOARD)
        return

    form = data.get("form", "task")

    if form == "finance":
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            await update.message.reply_text("Не понял сумму, сэр. Попробуйте ещё раз.", reply_markup=MAIN_KEYBOARD)
            return
        category = (data.get("category") or "").strip()
        if not category:
            await update.message.reply_text("Не указана категория, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        fin_type = "доход" if data.get("type") == "доход" else "расход"
        comment = (data.get("comment") or "").strip() or None
        summary = create_quick_finance(amount, category, fin_type, comment)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "apartment_new":
        address = (data.get("address") or "").strip()
        if not address:
            await update.message.reply_text("Не указан адрес квартиры, сэр.", reply_markup=MAIN_KEYBOARD)
            return

        def _num(key):
            value = data.get(key)
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _int(key):
            value = data.get(key)
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _str(key):
            return (data.get(key) or "").strip() or None

        summary = create_quick_apartment(
            address,
            owner_name=_str("owner_name"),
            owner_rent=_num("owner_rent"),
            rent_day=_int("rent_day"),
            notes=_str("notes"),
            floor=_str("floor"),
            unit_number=_str("unit_number"),
            wifi_login=_str("wifi_login"),
            wifi_password=_str("wifi_password"),
            owner_contacts=_str("owner_contacts"),
        )
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "move_in":
        apartment = (data.get("apartment") or "").strip()
        if not apartment:
            await update.message.reply_text("Не указана квартира, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        meters = {}
        for utility_type, value in (data.get("meters") or {}).items():
            try:
                meters[utility_type] = float(value)
            except (TypeError, ValueError):
                pass

        def _num(key):
            value = data.get(key)
            try:
                return float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        summary = create_quick_move_in(
            apartment,
            (data.get("tenant_name") or "").strip() or None,
            (data.get("tenant_phone") or "").strip() or None,
            (data.get("tenant_phone2") or "").strip() or None,
            (data.get("lease_start") or "").strip() or None,
            (data.get("lease_end") or "").strip() or None,
            tenant_rent=_num("tenant_rent"),
            deposit=_num("deposit"),
            meters=meters,
        )
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "apartment_operation":
        try:
            amount = float(data.get("amount"))
        except (TypeError, ValueError):
            await update.message.reply_text("Не понял сумму, сэр. Попробуйте ещё раз.", reply_markup=MAIN_KEYBOARD)
            return
        category = (data.get("category") or "").strip()
        if not category:
            await update.message.reply_text("Не указана категория, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        direction = "приход" if data.get("direction") == "приход" else "расход"
        apartment = (data.get("apartment") or "").strip() or None
        currency = (data.get("currency") or "MDL").strip() or "MDL"
        counterpart = (data.get("counterpart") or "").strip() or None
        op_date = resolve_deadline(data.get("date") or "")
        comment = (data.get("comment") or "").strip() or None
        summary = create_quick_apartment_operation(apartment, direction, category, amount, currency, counterpart, op_date, comment)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "decision":
        with_whom = (data.get("with_whom") or "").strip()
        what_decided = (data.get("what_decided") or "").strip()
        if not with_whom or not what_decided:
            await update.message.reply_text("Заполните, с кем и о чём договорились, сэр.", reply_markup=MAIN_KEYBOARD)
            return
        deadline = resolve_deadline(data.get("deadline") or "")
        next_step = (data.get("next_step") or "").strip() or None
        summary = create_quick_decision(with_whom, what_decided, deadline, next_step)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # form == "task"
    name = (data.get("name") or "").strip()
    if not name:
        await update.message.reply_text("Пустое описание задачи, сэр.", reply_markup=MAIN_KEYBOARD)
        return

    deadline = resolve_deadline(data.get("deadline") or "")
    priority = (data.get("priority") or "").strip() or None
    assignee = (data.get("assignee") or "").strip() or None

    summary = create_quick_task(name, deadline, priority, assignee)
    await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    user_message = update.message.text

    # --- Быстрая задача (запасной пошаговый сценарий без WebApp) ---
    if user_message == QUICK_TASK_BUTTON:
        context.user_data["quick_task"] = {}
        context.user_data["quick_task_step"] = "name"
        await update.message.reply_text("Описание задачи, сэр?", reply_markup=ReplyKeyboardRemove())
        return

    quick_task_step = context.user_data.get("quick_task_step")

    if quick_task_step == "name":
        context.user_data["quick_task"]["name"] = user_message
        context.user_data["quick_task_step"] = "deadline"
        await update.message.reply_text("Дедлайн? Выберите или напишите дату вручную.", reply_markup=DEADLINE_KEYBOARD)
        return

    if quick_task_step == "deadline":
        if user_message == "Сегодня":
            deadline = now_msk().strftime("%Y-%m-%d")
        elif user_message == "Завтра":
            deadline = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif user_message == "Нет":
            deadline = None
        else:
            deadline = user_message
        context.user_data["quick_task"]["deadline"] = deadline
        context.user_data["quick_task_step"] = "priority"
        await update.message.reply_text("Приоритет? Выберите или напишите свой вариант.", reply_markup=PRIORITY_KEYBOARD)
        return

    if quick_task_step == "priority":
        priority = None if user_message == "Нет" else user_message
        context.user_data["quick_task"]["priority"] = priority
        context.user_data["quick_task_step"] = "assignee"
        await update.message.reply_text("Кто исполнитель? Выберите или напишите имя.", reply_markup=ASSIGNEE_KEYBOARD)
        return

    if quick_task_step == "assignee":
        assignee = None if user_message == "Нет" else user_message
        task = context.user_data["quick_task"]
        summary = create_quick_task(task["name"], task.get("deadline"), task.get("priority"), assignee)
        context.user_data["quick_task_step"] = None
        context.user_data["quick_task"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Быстрые финансы (запасной пошаговый сценарий) ---
    if user_message == QUICK_FINANCE_BUTTON:
        context.user_data["quick_finance"] = {}
        context.user_data["quick_finance_step"] = "type"
        await update.message.reply_text("Расход или доход, сэр?", reply_markup=FINANCE_TYPE_KEYBOARD)
        return

    quick_finance_step = context.user_data.get("quick_finance_step")

    if quick_finance_step == "type":
        fin_type = "доход" if user_message == "Доход" else "расход"
        context.user_data["quick_finance"]["type"] = fin_type
        context.user_data["quick_finance_step"] = "amount"
        await update.message.reply_text("Сумма?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_finance_step == "amount":
        try:
            amount = float(user_message.replace(",", ".").strip())
        except ValueError:
            await update.message.reply_text("Нужно число, сэр. Сколько?")
            return
        context.user_data["quick_finance"]["amount"] = amount
        context.user_data["quick_finance_step"] = "category"
        await update.message.reply_text("Категория?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_finance_step == "category":
        context.user_data["quick_finance"]["category"] = user_message
        context.user_data["quick_finance_step"] = "comment"
        await update.message.reply_text("Комментарий? Напишите или 'Нет'.", reply_markup=SKIP_KEYBOARD)
        return

    if quick_finance_step == "comment":
        comment = None if user_message == "Нет" else user_message
        fin = context.user_data["quick_finance"]
        summary = create_quick_finance(fin["amount"], fin["category"], fin["type"], comment)
        context.user_data["quick_finance_step"] = None
        context.user_data["quick_finance"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Быстрая договорённость (запасной пошаговый сценарий) ---
    if user_message == QUICK_DECISION_BUTTON:
        context.user_data["quick_decision"] = {}
        context.user_data["quick_decision_step"] = "with_whom"
        await update.message.reply_text("С кем договорились, сэр?", reply_markup=WITH_WHOM_KEYBOARD)
        return

    quick_decision_step = context.user_data.get("quick_decision_step")

    if quick_decision_step == "with_whom":
        context.user_data["quick_decision"]["with_whom"] = user_message
        context.user_data["quick_decision_step"] = "what_decided"
        await update.message.reply_text("О чём договорились?", reply_markup=ReplyKeyboardRemove())
        return

    if quick_decision_step == "what_decided":
        context.user_data["quick_decision"]["what_decided"] = user_message
        context.user_data["quick_decision_step"] = "deadline"
        await update.message.reply_text("Дедлайн? Выберите или напишите дату вручную.", reply_markup=DEADLINE_KEYBOARD)
        return

    if quick_decision_step == "deadline":
        if user_message == "Сегодня":
            deadline = now_msk().strftime("%Y-%m-%d")
        elif user_message == "Завтра":
            deadline = (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif user_message == "Нет":
            deadline = None
        else:
            deadline = user_message
        context.user_data["quick_decision"]["deadline"] = deadline
        context.user_data["quick_decision_step"] = "next_step"
        await update.message.reply_text("Следующий шаг? Напишите или 'Нет'.", reply_markup=SKIP_KEYBOARD)
        return

    if quick_decision_step == "next_step":
        next_step = None if user_message == "Нет" else user_message
        dec = context.user_data["quick_decision"]
        summary = create_quick_decision(dec["with_whom"], dec["what_decided"], dec.get("deadline"), next_step)
        context.user_data["quick_decision_step"] = None
        context.user_data["quick_decision"] = {}
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
        return

    # --- Квартиры без мини-формы (если WEBAPP_URL не настроен) ---
    if user_message == APARTMENT_BUTTON:
        await update.message.reply_text(
            "Опишите операцию текстом, сэр, например: \"запиши приход 700 лей аренды по Лев Толстой от квартиранта\". "
            "Я разберусь сам."
        )
        return

    # --- Кнопки мониторинга ---
    if user_message == VIEW_TASKS_BUTTON:
        await reply_md(update.message, db_get_tasks())
        return

    if user_message == VIEW_DECISIONS_BUTTON:
        await reply_md(update.message, db_get_decisions())
        return

    if user_message == VIEW_FINANCE_BUTTON:
        await reply_md(update.message, db_get_finance())
        return

    if user_message == VIEW_APARTMENT_BALANCE_BUTTON:
        await reply_md(update.message, db_get_apartment_balance())
        return

    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})
    if len(conversation_history) > 10:
        conversation_history = conversation_history[-10:]

    context_str = f"\n\n{get_db_context()}" if needs_context(user_message) else ""
    system = build_system(context_str)

    try:
        db_save_message("user", user_message)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        assistant_message, chart_path = process_message(user_message, system)
        conversation_history.append({"role": "assistant", "content": assistant_message})
        db_save_message("assistant", assistant_message)
        await reply_md(update.message, assistant_message)
        if chart_path:
            try:
                with open(chart_path, "rb") as f:
                    await update.message.reply_photo(photo=f)
            finally:
                os.remove(chart_path)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text("Произошла ошибка, сэр. Попробуйте ещё раз.")


async def post_init(application: Application):
    asyncio.create_task(scheduler(application.bot))
    asyncio.create_task(run_webapp_server())


def main():
    init_db()
    global conversation_history
    conversation_history = db_get_recent_history()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("tasks", tasks))
    app.add_handler(CommandHandler("decisions", decisions_cmd))
    app.add_handler(CommandHandler("finance", finance_cmd))
    app.add_handler(CommandHandler("memory", memory))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("FRIDAY запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
