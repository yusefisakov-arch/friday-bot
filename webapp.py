"""Слой веб-приложений: HTTP-сервер форм, API, проверка подписи Telegram,
обработчики мини-форм (быстрые задачи/финансы/квартиры/заселение/выселение)."""
import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update
from telegram.ext import ContextTypes

from core import *
from db import *

logger = logging.getLogger(__name__)


def verify_init_data(init_data, max_age_seconds=86400):
    """Проверяет подпись Telegram WebApp initData и что это наш пользователь."""
    if not init_data:
        logger.warning("initData: пусто (форма открыта вне Telegram или клиент не передал подпись)")
        return False
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        logger.warning("initData: не удалось разобрать строку")
        return False
    received_hash = parsed.pop("hash", None)
    # Поле signature (новые клиенты Telegram) тоже исключается из строки проверки
    parsed.pop("signature", None)
    if not received_hash:
        logger.warning("initData: нет поля hash")
        return False
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        logger.warning("initData: подпись не совпала")
        return False
    # Защита от старых/повторных данных
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        if max_age_seconds and (now_msk().timestamp() - auth_date) > max_age_seconds:
            logger.warning("initData: данные устарели (auth_date старше суток)")
            return False
    except ValueError:
        return False
    # Только разрешённый пользователь
    try:
        user = json.loads(parsed.get("user", "{}"))
        if int(user.get("id", 0)) == ALLOWED_USER_ID:
            return True
        logger.warning("initData: чужой пользователь id=%s", user.get("id"))
        return False
    except (ValueError, TypeError):
        return False


def is_webapp_request_allowed(request):
    # Мини-формы открываются кнопками reply-клавиатуры — Telegram им initData не даёт,
    # поэтому доступ к /api/* проверяем по секретному ключу из адреса формы.
    key = request.headers.get("X-Webapp-Key", "") or request.query.get("k", "")
    if not WEBAPP_SECRET:
        return False
    if hmac.compare_digest(key, WEBAPP_SECRET):
        return True
    logger.warning("api: неверный или отсутствующий ключ доступа")
    return False


async def get_staff(request):
    if not is_webapp_request_allowed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response(list(STAFF.keys()))


async def get_apartments_api(request):
    if not is_webapp_request_allowed(request):
        return web.json_response({"error": "unauthorized"}, status=401)
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
    app.router.add_get("/move_out", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "move_out.html")))
    app.router.add_get("/utilities", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "utilities.html")))
    app.router.add_get("/", lambda r: web.FileResponse(os.path.join(WEBAPP_DIR, "form.html")))
    app.router.add_get("/api/staff", get_staff)
    app.router.add_get("/api/apartments", get_apartments_api)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер форм запущен на порту {PORT}")


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


def create_quick_move_out(apartment, move_out_date=None, meters=None, deposit_return=None, deposit_comment=None):
    status, info = db_find_apartment(apartment)
    if status == "not_found":
        return f"Квартира '{apartment}' не найдена в справочнике, сэр."
    if status == "ambiguous":
        return "Нашлось несколько подходящих квартир: " + ", ".join(info) + ". Уточните, сэр."
    apartment_id, address = info

    summary = f"Выселение записано, сэр: {address}"

    if meters:
        readings = [{"utility_type": k, "reading": v} for k, v in meters.items()]
        _, _, lines, total = db_calculate_utilities(address, readings)
        if lines:
            summary += "\n\nКоммуналка на момент выезда:\n" + "\n".join(lines)
            summary += f"\n*Итого: {total} MDL*"

    if deposit_return is not None:
        db_record_apartment_operation(address, "расход", "Депозит", deposit_return, comment=deposit_comment, op_date=move_out_date)
        summary += f"\n\nВозврат депозита: {deposit_return} MDL"
        if deposit_comment:
            summary += f" ({deposit_comment})"

    db_clear_tenant(apartment_id)
    summary += "\n\nКвартира освобождена, готова к новому заселению."
    return summary


def create_quick_utilities(apartment, meters=None, record_cash=False):
    """Считает коммуналку по новым показаниям, сохраняет их как базу и (опционально) пишет итог в кассу."""
    readings = [{"utility_type": k, "reading": v} for k, v in (meters or {}).items()]
    if not readings:
        return "Не переданы показания счётчиков, сэр."
    status, address, lines, total = db_calculate_utilities(apartment, readings)
    if status == "ambiguous":
        return "Нашлось несколько подходящих квартир: " + ", ".join(address) + ". Уточните, сэр."
    if status != "ok":
        return f"Квартира '{address}' не найдена в справочнике, сэр."
    summary = f"*Коммуналка — {address}:*\n" + "\n".join(lines) + f"\n\n*ИТОГО: {total} MDL*"
    if record_cash and total and total > 0:
        db_record_apartment_operation(address, "приход", "Коммуналка", total, "MDL", counterpart="Квартирант")
        summary += "\n\nЗаписал итог в кассу квартир (приход, Коммуналка)."
    summary += "\n\nНовые показания сохранены как базовые для следующего расчёта."
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


def normalize_deadline(value):
    """Приводит дедлайн к YYYY-MM-DD. Понимает кнопки и распространённые форматы дат.
    Если распознать как дату не удалось — возвращает None (чтобы не ломать сортировку по срокам)."""
    if not value:
        return None
    v = str(value).strip()
    low = v.lower()
    if low in ("нет", "no", "none"):
        return None
    if low in ("today", "сегодня"):
        return now_msk().strftime("%Y-%m-%d")
    if low in ("tomorrow", "завтра"):
        return (now_msk() + timedelta(days=1)).strftime("%Y-%m-%d")
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d.%m"):
        try:
            d = datetime.strptime(v, fmt)
            if fmt == "%d.%m":
                d = d.replace(year=now_msk().year)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def resolve_deadline(value):
    return normalize_deadline(value)


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

    if form == "move_out":
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

        summary = create_quick_move_out(
            apartment,
            move_out_date=(data.get("move_out_date") or "").strip() or None,
            meters=meters,
            deposit_return=_num("deposit_return"),
            deposit_comment=(data.get("deposit_comment") or "").strip() or None,
        )
        await reply_md(update.message, summary, reply_markup=MAIN_KEYBOARD)
        return

    if form == "utilities":
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
        summary = create_quick_utilities(apartment, meters, bool(data.get("record_cash")))
        await reply_md(update.message, summary, reply_markup=MAIN_KEYBOARD)
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

