"""Радар недвижимости: следит за 999.md и ловит объявления дешевле рынка.

Логика под стратегию «купить дёшево с плохим ремонтом → отремонтировать →
сдавать помесячно»:

  1. Раз в сутки бот строит карту рынка — медиану €/м² по каждой паре
     (сектор, количество комнат), считая по случайной выборке всех
     объявлений о продаже в Кишинёве, независимо от состояния.
  2. Каждые N минут проверяет свежие объявления по подписке
     (продажа, Кишинёв, плохое состояние, цена до потолка).
  3. Присылает только то, чей €/м² ниже медианы своей группы на заданный
     процент — с пометкой, насколько именно ниже рынка.

Данные берутся из GraphQL API 999.md (POST https://999.md/graphql), а не из
HTML — это переживает редизайны сайта и не упирается в капчу.
"""

import asyncio
import json
import logging
import statistics
from datetime import datetime

import aiohttp

from core import db_conn, send_md

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://999.md/graphql"
IMAGE_BASE = "https://i.simpalsmedia.com/999.md/BoardImages"
HEADERS = {
    "content-type": "application/json",
    "lang": "ru",
    "origin": "https://999.md",
    "referer": "https://999.md/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# --- справочник 999.md (снят с рабочего сайта) ---------------------------------

CAT_APARTMENTS = 1404
CAT_HOUSES = 1406

F_OFFER, F_PRICE, F_REGION, F_SECTOR, F_LOCALITY = 1, 2, 7, 9, 8
F_STREET, F_IMAGES, F_ROOMS, F_AREA, F_FLOOR = 10, 14, 241, 244, 248
F_COND_APT, F_COND_HOUSE = 253, 254
F_ROOMS_HOUSE = 588

FILTER_OFFER, FILTER_REGION, FILTER_PRICE = 16, 32, 9441
FILTER_COND_APT, FILTER_COND_HOUSE = 1074, 1207

OPT_SELL = 776           # «Продам»
OPT_CHISINAU = 12900     # регион «Кишинёв мун.»

# состояния, которые нам интересны: жильё, требующее вложений
COND_APT_BAD = [928, 952, 949, 925, 931]      # без ремонта, нуждается, серый, белый, косметический
COND_HOUSE_BAD = [1640, 1646, 1650, 1648, 1642]

CATEGORY_TITLES = {CAT_APARTMENTS: "Квартиры", CAT_HOUSES: "Дома"}

MARKET_SAMPLE_PAGES = 4      # 4 × 200 = 800 объявлений на категорию для карты рынка
MARKET_MIN_GROUP = 8         # меньше — группа слишком мала, медиане нельзя верить
MARKET_TTL_HOURS = 24

# Рынок считаем по такому же «убитому» жилью, а не по всему подряд: иначе любая
# квартира без ремонта выглядит на 30% дешевле евроремонта — это не находка,
# а просто её состояние.
SANE_M2_MIN, SANE_M2_MAX = 150, 8000   # за этими границами — мусор или приманка
ANOMALY_BELOW_PCT = 60                 # «дешевле на 60%+» = ошибка в объявлении


# --- слой данных ---------------------------------------------------------------

def radar_init_db():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_subs (
                id           SERIAL PRIMARY KEY,
                name         TEXT NOT NULL,
                chat_id      BIGINT NOT NULL,
                topic_id     BIGINT,
                category_id  INT NOT NULL,
                max_price    INT,
                discount     INT NOT NULL DEFAULT 20,
                filters      JSONB NOT NULL,
                active       BOOLEAN NOT NULL DEFAULT TRUE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_checked TIMESTAMPTZ
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_seen (
                sub_id  INT NOT NULL,
                ad_id   TEXT NOT NULL,
                seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (sub_id, ad_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_market (
                category_id INT NOT NULL,
                locality    TEXT NOT NULL,
                sector      TEXT NOT NULL,
                rooms       TEXT NOT NULL,
                median_m2   NUMERIC NOT NULL,
                sample_size INT NOT NULL,
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (category_id, locality, sector, rooms)
            )
        """)
        cur.close()


def radar_add_sub(name, chat_id, topic_id, category_id, max_price, discount, filters):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO radar_subs (name, chat_id, topic_id, category_id, max_price, "
            "discount, filters) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, chat_id, topic_id, category_id, max_price, discount,
             json.dumps(filters)),
        )
        sub_id = cur.fetchone()[0]
        cur.close()
    return sub_id


def radar_list_subs(only_active=False):
    sql = ("SELECT id, name, chat_id, topic_id, category_id, max_price, discount, "
           "filters, active, last_checked FROM radar_subs")
    if only_active:
        sql += " WHERE active"
    sql += " ORDER BY id"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
    keys = ("id", "name", "chat_id", "topic_id", "category_id", "max_price",
            "discount", "filters", "active", "last_checked")
    return [dict(zip(keys, row)) for row in rows]


def radar_set_active(sub_id, active):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE radar_subs SET active=%s WHERE id=%s", (active, sub_id))
        cur.close()


def radar_delete_sub(sub_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM radar_seen WHERE sub_id=%s", (sub_id,))
        cur.execute("DELETE FROM radar_subs WHERE id=%s", (sub_id,))
        cur.close()


def radar_new_ids(sub_id, ad_ids):
    if not ad_ids:
        return []
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT ad_id FROM radar_seen WHERE sub_id=%s AND ad_id = ANY(%s)",
                    (sub_id, list(ad_ids)))
        known = {row[0] for row in cur.fetchall()}
        cur.close()
    return [a for a in ad_ids if a not in known]


def radar_mark_seen(sub_id, ad_ids):
    if not ad_ids:
        return
    with db_conn() as conn:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO radar_seen (sub_id, ad_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            [(sub_id, a) for a in ad_ids],
        )
        cur.execute("UPDATE radar_subs SET last_checked=now() WHERE id=%s", (sub_id,))
        cur.close()


def radar_save_market(category_id, rows):
    """rows: [(locality, sector, rooms, median_m2, sample_size), ...]"""
    if not rows:
        return
    with db_conn() as conn:
        cur = conn.cursor()
        cur.executemany(
            "INSERT INTO radar_market (category_id, locality, sector, rooms, median_m2, "
            "sample_size) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (category_id, locality, sector, rooms) DO UPDATE SET "
            "median_m2=EXCLUDED.median_m2, sample_size=EXCLUDED.sample_size, updated_at=now()",
            [(category_id, loc, sec, r, m, n) for loc, sec, r, m, n in rows],
        )
        cur.close()


def radar_get_market(category_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT locality, sector, rooms, median_m2, sample_size FROM radar_market "
            "WHERE category_id=%s", (category_id,))
        rows = cur.fetchall()
        cur.close()
    return {(r[0], r[1], r[2]): (float(r[3]), r[4]) for r in rows}


def radar_market_age_hours(category_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (now() - MIN(updated_at)))/3600 "
            "FROM radar_market WHERE category_id=%s", (category_id,))
        row = cur.fetchone()
        cur.close()
    return float(row[0]) if row and row[0] is not None else None


# --- клиент 999.md --------------------------------------------------------------

def _literal(value):
    """Значение → литерал GraphQL. Enum-строки идут без кавычек."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if value.startswith(("SORT_", "UNIT_")):
            return value
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_literal(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"Нельзя сериализовать {type(value)!r}")


async def _gql(session, query):
    async with session.post(GRAPHQL_URL, json={"query": query}, headers=HEADERS,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    if payload.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload["data"]


def base_filters(category_id, max_price=None, bad_condition=True, extra=None):
    filters = [
        {"filterId": FILTER_OFFER,
         "features": [{"featureId": F_OFFER, "optionIds": [OPT_SELL]}]},
        {"filterId": FILTER_REGION,
         "features": [{"featureId": F_REGION, "optionIds": [OPT_CHISINAU]}]},
    ]
    if max_price:
        filters.append({"filterId": FILTER_PRICE,
                        "features": [{"featureId": F_PRICE,
                                      "range": {"max": max_price},
                                      "unit": "UNIT_EUR"}]})
    if bad_condition:
        if category_id == CAT_HOUSES:
            filters.append({"filterId": FILTER_COND_HOUSE,
                            "features": [{"featureId": F_COND_HOUSE,
                                          "optionIds": COND_HOUSE_BAD}]})
        else:
            filters.append({"filterId": FILTER_COND_APT,
                            "features": [{"featureId": F_COND_APT,
                                          "optionIds": COND_APT_BAD}]})
    if extra:
        filters.extend(extra)
    return filters


async def fetch_ads(session, category_id, filters, limit=40, skip=0, sort="SORT_ADS_DATE_DESC"):
    rooms_feature = F_ROOMS_HOUSE if category_id == CAT_HOUSES else F_ROOMS
    wanted = (F_PRICE, F_SECTOR, F_LOCALITY, F_STREET, F_IMAGES,
              rooms_feature, F_AREA, F_FLOOR)
    fields = " ".join(f"f{fid}: feature(id: {fid}) {{ value }}" for fid in wanted)
    payload = {
        "subCategoryId": category_id,
        "sort": sort,
        "pagination": {"limit": limit, "skip": skip},
    }
    if filters:
        payload["filters"] = filters
    data = await _gql(session, "{ searchAds(input: " + _literal(payload) +
                      ") { count ads { id title " + fields + " } } }")
    ads = [_parse_ad(raw, rooms_feature) for raw in data["searchAds"]["ads"]]
    return ads, data["searchAds"]["count"]


def _val(raw, fid):
    node = raw.get(f"f{fid}")
    return node.get("value") if node else None


def _text(raw, fid):
    v = _val(raw, fid)
    if isinstance(v, dict):
        return str(v.get("translated") or "")
    return str(v) if v is not None else ""


def _parse_ad(raw, rooms_feature):
    price_node = _val(raw, F_PRICE) or {}
    area_node = _val(raw, F_AREA) or {}
    images = _val(raw, F_IMAGES) or []

    price = price_node.get("value") if isinstance(price_node, dict) else None
    area = area_node.get("value") if isinstance(area_node, dict) else None
    unit = price_node.get("unit") if isinstance(price_node, dict) else None

    return {
        "id": str(raw["id"]),
        "title": " ".join((raw.get("title") or "").split()),
        "price": float(price) if isinstance(price, (int, float)) else None,
        "currency": unit or "",
        "area": float(area) if isinstance(area, (int, float)) else None,
        "rooms": _text(raw, rooms_feature),
        "floor": _text(raw, F_FLOOR),
        "sector": _text(raw, F_SECTOR),
        "locality": _text(raw, F_LOCALITY),
        "street": _text(raw, F_STREET),
        "images": [str(i) for i in images] if isinstance(images, list) else [],
    }


def price_per_m2(ad):
    """€/м². Объявления не в евро в расчёт рынка не берём — курс плавает."""
    if not ad["price"] or not ad["area"] or ad["currency"] != "UNIT_EUR":
        return None
    if ad["area"] < 10:  # мусорные данные
        return None
    return ad["price"] / ad["area"]


# --- карта рынка ----------------------------------------------------------------

async def rebuild_market(category_id):
    """Пересчитывает медиану €/м² по (город, сектор, комнаты) для категории."""
    groups = {}
    async with aiohttp.ClientSession() as session:
        filters = base_filters(category_id, bad_condition=True)
        for page in range(MARKET_SAMPLE_PAGES):
            try:
                ads, _ = await fetch_ads(session, category_id, filters, limit=200,
                                         skip=page * 200, sort="SORT_ADS_RANDOM")
            except Exception as e:
                logger.error(f"Радар: карта рынка, страница {page}: {e}")
                break
            for ad in ads:
                per_m2 = price_per_m2(ad)
                if per_m2 is None or not ad["sector"]:
                    continue
                if not (SANE_M2_MIN <= per_m2 <= SANE_M2_MAX):
                    continue
                key = (ad["locality"] or "—", ad["sector"], ad["rooms"] or "—")
                groups.setdefault(key, []).append(per_m2)
            await asyncio.sleep(1)

    rows = [(loc, sector, rooms, round(statistics.median(values), 2), len(values))
            for (loc, sector, rooms), values in groups.items()
            if len(values) >= MARKET_MIN_GROUP]
    radar_save_market(category_id, rows)
    logger.info(f"Радар: карта рынка {CATEGORY_TITLES.get(category_id)} — "
                f"{len(rows)} групп")
    return len(rows)


def evaluate(ad, market):
    """Насколько объявление дешевле сопоставимых. → (процент, медиана) или (None, None).

    Сравниваем с медианой такого же жилья в том же городе, секторе и
    комнатности. Слишком хорошие цифры (дешевле медианы более чем на
    ANOMALY_BELOW_PCT) — это почти всегда опечатка продавца или приманка
    «цена за долю», такое отбрасываем, чтобы не гонять на просмотры впустую.
    """
    per_m2 = price_per_m2(ad)
    if per_m2 is None or not (SANE_M2_MIN <= per_m2 <= SANE_M2_MAX):
        return None, None
    entry = market.get((ad["locality"] or "—", ad["sector"], ad["rooms"] or "—"))
    if not entry:
        return None, None
    median, _ = entry
    if median <= 0:
        return None, None
    below = round((1 - per_m2 / median) * 100)
    if below >= ANOMALY_BELOW_PCT:
        logger.info(f"Радар: {ad['id']} отброшен как аномалия ({below}% ниже медианы)")
        return None, None
    return below, median


# --- отправка -------------------------------------------------------------------

def render(ad, below_pct, median):
    per_m2 = price_per_m2(ad)
    head = f"🎯 *{below_pct}% ниже рынка*" if below_pct else "🏠 *Новое объявление*"

    lines = [head, f"*{ad['title']}*"]

    facts = []
    if ad["area"]:
        facts.append(f"{ad['area']:g} м²")
    if ad["floor"]:
        facts.append(f"этаж {ad['floor']}")
    if facts:
        lines.append(" · ".join(facts))

    if ad["price"]:
        sign = {"UNIT_EUR": "€", "UNIT_USD": "$", "UNIT_MDL": "лей"}.get(ad["currency"], "")
        price_line = f"💶 *{ad['price']:,.0f} {sign}*".replace(",", " ")
        if per_m2:
            price_line += f"  ·  {per_m2:,.0f} €/м²".replace(",", " ")
        lines.append(price_line)
    else:
        lines.append("💶 цена не указана")

    if median:
        lines.append(f"_медиана по группе: {median:,.0f} €/м²_".replace(",", " "))

    place = ", ".join(p for p in (ad["locality"], ad["sector"], ad["street"]) if p)
    if place:
        lines.append(f"📍 {place}")

    lines.append(f"\nhttps://999.md/ru/{ad['id']}")
    return "\n".join(lines)


def strip_markup(text):
    """Тот же текст без разметки — запасной вариант, если Telegram её не принял."""
    return text.replace("*", "").replace("_", "")


async def send_ad(bot, sub, ad, below_pct, median):
    kwargs = {"chat_id": sub["chat_id"]}
    if sub["topic_id"]:
        kwargs["message_thread_id"] = sub["topic_id"]
    text = render(ad, below_pct, median)

    if ad["images"]:
        photo = f"{IMAGE_BASE}/640x480/{ad['images'][0]}"
        try:
            await bot.send_photo(photo=photo, caption=text, parse_mode="Markdown", **kwargs)
            return
        except Exception as e:
            logger.warning(f"Радар: фото с разметкой не ушло ({e})")
            try:
                await bot.send_photo(photo=photo, caption=strip_markup(text), **kwargs)
                return
            except Exception as e2:
                logger.warning(f"Радар: фото не ушло совсем ({e2}), шлю текстом")
    try:
        await bot.send_message(text=text, parse_mode="Markdown", **kwargs)
    except Exception as e:
        logger.warning(f"Радар: разметка не прошла ({e}), шлю простым текстом")
        await bot.send_message(text=strip_markup(text), **kwargs)


# --- основной цикл --------------------------------------------------------------

async def radar_check_all(bot, force=False):
    """Проверяет все активные подписки. Возвращает число отправленных объявлений."""
    subs = radar_list_subs(only_active=True)
    if not subs:
        return 0

    sent_total = 0
    async with aiohttp.ClientSession() as session:
        for sub in subs:
            category_id = sub["category_id"]

            age = radar_market_age_hours(category_id)
            if age is None or age > MARKET_TTL_HOURS:
                await rebuild_market(category_id)
            market = radar_get_market(category_id)

            try:
                ads, _ = await fetch_ads(session, category_id, sub["filters"], limit=40)
            except Exception as e:
                logger.error(f"Радар #{sub['id']}: {e}")
                continue

            fresh = radar_new_ids(sub["id"], [a["id"] for a in ads])
            if not fresh:
                continue
            fresh_set = set(fresh)

            picked = []
            for ad in ads:
                if ad["id"] not in fresh_set:
                    continue
                below, median = evaluate(ad, market)
                if below is not None and below >= sub["discount"]:
                    picked.append((ad, below, median))

            # свежие идут первыми — отправляем в хронологическом порядке
            for ad, below, median in reversed(picked[:10]):
                try:
                    await send_ad(bot, sub, ad, below, median)
                    sent_total += 1
                except Exception as e:
                    logger.error(f"Радар #{sub['id']}: не отправилось {ad['id']}: {e}")
                await asyncio.sleep(0.5)

            radar_mark_seen(sub["id"], fresh)
            logger.info(f"Радар #{sub['id']} «{sub['name']}»: новых {len(fresh)}, "
                        f"из них подходящих {len(picked)}")
            await asyncio.sleep(1)

    return sent_total


async def radar_seed(sub_id):
    """Первый прогон: помечает всё текущее как виденное, чтобы не залить группу."""
    subs = [s for s in radar_list_subs() if s["id"] == sub_id]
    if not subs:
        return 0
    sub = subs[0]
    async with aiohttp.ClientSession() as session:
        ads, total = await fetch_ads(session, sub["category_id"], sub["filters"], limit=100)
    radar_mark_seen(sub_id, [a["id"] for a in ads])
    return total


# --- телеграм-хендлеры ----------------------------------------------------------

DEFAULT_MAX_PRICE = 60000
DEFAULT_DISCOUNT = 20

PRESETS = [
    ("Квартиры под ремонт", CAT_APARTMENTS),
    ("Дома под ремонт", CAT_HOUSES),
]


async def radar_here(update, context):
    """/radar_here — выполняется В ГРУППЕ: заводит темы и подписки."""
    from core import is_allowed
    if not is_allowed(update.effective_user.id):
        return
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "Эту команду нужно отправить в группе, где будут приходить объявления."
        )
        return

    radar_init_db()
    existing = {s["name"] for s in radar_list_subs()}
    created = []

    for name, category_id in PRESETS:
        if name in existing:
            continue
        topic_id = None
        try:
            topic = await context.bot.create_forum_topic(chat_id=chat.id, name=name)
            topic_id = topic.message_thread_id
        except Exception as e:
            logger.warning(f"Радар: тема «{name}» не создана: {e}")

        filters = base_filters(category_id, max_price=DEFAULT_MAX_PRICE)
        sub_id = radar_add_sub(name, chat.id, topic_id, category_id,
                               DEFAULT_MAX_PRICE, DEFAULT_DISCOUNT, filters)
        try:
            total = await radar_seed(sub_id)
        except Exception as e:
            logger.error(f"Радар: первичный прогон #{sub_id}: {e}")
            total = 0
        created.append(f"• {name} — сейчас в базе {total} подходящих")

    if not created:
        await update.message.reply_text("Радар здесь уже настроен, сэр.")
        return

    price_txt = f"{DEFAULT_MAX_PRICE:,}".replace(",", " ")
    await update.message.reply_text(
        "Радар настроен, сэр.\n\n" + "\n".join(created) +
        f"\n\nПотолок цены {price_txt} €, порог — дешевле медианы сопоставимых "
        f"на {DEFAULT_DISCOUNT}%. Всё, что есть сейчас, помечено как просмотренное — "
        "буду присылать только новое."
    )


async def radar_status(update, context):
    """/radar — что настроено и в каком состоянии."""
    from core import is_allowed
    if not is_allowed(update.effective_user.id):
        return
    radar_init_db()
    subs = radar_list_subs()
    if not subs:
        await update.message.reply_text(
            "Радар пока не настроен. Создайте группу с темами, добавьте меня "
            "админом и отправьте там /radar_here"
        )
        return

    lines = ["*Радар 999.md*"]
    for s in subs:
        state = "▶️" if s["active"] else "⏸"
        market = radar_get_market(s["category_id"])
        age = radar_market_age_hours(s["category_id"])
        age_txt = f"{age:.0f} ч назад" if age is not None else "ещё не строилась"
        last = s["last_checked"].strftime("%d.%m %H:%M") if s["last_checked"] else "—"
        lines.append(
            f"\n{state} *#{s['id']} {s['name']}*\n"
            f"до {s['max_price']:,} €".replace(",", " ") +
            f", порог {s['discount']}% ниже медианы\n"
            f"карта рынка: {len(market)} групп, обновлена {age_txt}\n"
            f"последняя проверка: {last}"
        )
    lines.append("\n/radar_check — проверить сейчас\n/radar_off N, /radar_on N — пауза")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def radar_check_cmd(update, context):
    """/radar_check — прогнать проверку немедленно."""
    from core import is_allowed
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Проверяю 999.md, сэр…")
    try:
        sent = await radar_check_all(context.bot)
    except Exception as e:
        logger.exception("Радар: ручная проверка")
        await update.message.reply_text(f"Не получилось: {e}")
        return
    await update.message.reply_text(
        f"Готово. Подходящих объявлений: {sent}" if sent
        else "Готово. Ничего нового, что стоило бы вашего внимания."
    )


async def radar_toggle_cmd(update, context):
    """/radar_off N и /radar_on N."""
    from core import is_allowed
    if not is_allowed(update.effective_user.id):
        return
    text = (update.message.text or "").strip()
    turn_on = text.startswith("/radar_on")
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("Укажите номер: /radar_off 1")
        return
    radar_set_active(int(parts[1]), turn_on)
    await update.message.reply_text("Включил." if turn_on else "Поставил на паузу.")


async def radar_loop(bot, interval_minutes=15):
    """Фоновый цикл: проверяет подписки, пока бот жив."""
    await asyncio.sleep(30)
    while True:
        try:
            radar_init_db()
            await radar_check_all(bot)
        except Exception:
            logger.exception("Радар: сбой цикла")
        await asyncio.sleep(interval_minutes * 60)
