"""Второй источник объявлений: публичные телеграм-каналы.

Каналы читаются через веб-версию t.me/s/<канал> — ту самую страницу
предпросмотра, которую Telegram отдаёт всем без авторизации. Ни второй
аккаунт, ни api_id, ни номер телефона для этого не нужны: бот просто
открывает страницу, как открыл бы её браузер.

Текст поста разбирается регулярками и превращается в такой же словарь,
какой приходит из 999.md, — поэтому дальше работают ровно те же правила
отбора: не аренда, потолок €/м², этажи, дедупликация, повтор только при
падении цены.
"""
import asyncio
import html
import logging
import re
from urllib.parse import urljoin

import aiohttp

from core import db_conn

logger = logging.getLogger(__name__)

TME_BASE = "https://t.me/s/"
TME_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Accept-Language": "ru,ro;q=0.9,en;q=0.8",
}

# Сколько страниц предпросмотра листаем за одну проверку. Одна страница —
# это около 20 последних постов; при проверке раз в 15 минут этого хватает
# с запасом, больше нужно только при первой выгрузке канала.
PAGES_PER_CHECK = 1
PAGES_ON_FIRST_DUMP = 8


# --- хранилище ------------------------------------------------------------------

def ch_init_db():
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_channels (
                id           SERIAL PRIMARY KEY,
                username     TEXT NOT NULL,
                chat_id      BIGINT NOT NULL,
                title        TEXT,
                active       BOOLEAN NOT NULL DEFAULT TRUE,
                last_post_id BIGINT NOT NULL DEFAULT 0,
                added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (chat_id, username)
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS radar_ch_sent (
                channel_id INTEGER NOT NULL,
                post_id    BIGINT NOT NULL,
                price      NUMERIC,
                sent_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (channel_id, post_id)
            )""")
        cur.close()


def ch_add(chat_id, username, title=None):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO radar_channels (username, chat_id, title) VALUES (%s,%s,%s) "
            "ON CONFLICT (chat_id, username) DO UPDATE SET active=TRUE, "
            "title=COALESCE(EXCLUDED.title, radar_channels.title) RETURNING id",
            (username, chat_id, title))
        cid = cur.fetchone()[0]
        cur.close()
    return cid


def ch_list(chat_id=None, only_active=True):
    sql = ("SELECT id, username, chat_id, title, active, last_post_id "
           "FROM radar_channels WHERE TRUE")
    args = []
    if chat_id is not None:
        sql += " AND chat_id=%s"
        args.append(chat_id)
    if only_active:
        sql += " AND active"
    sql += " ORDER BY username"
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = cur.fetchall()
        cur.close()
    keys = ("id", "username", "chat_id", "title", "active", "last_post_id")
    return [dict(zip(keys, r)) for r in rows]


def ch_delete(chat_id, username):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM radar_channels WHERE chat_id=%s AND username=%s "
                    "RETURNING id", (chat_id, username))
        row = cur.fetchone()
        cur.close()
    return bool(row)


def ch_set_last(channel_id, post_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE radar_channels SET last_post_id=GREATEST(last_post_id, %s) "
                    "WHERE id=%s", (post_id, channel_id))
        cur.close()


def ch_sent_price(channel_id, post_id):
    """Цена, с которой пост уже уходил, или None если не уходил."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT price FROM radar_ch_sent WHERE channel_id=%s AND post_id=%s",
                    (channel_id, post_id))
        row = cur.fetchone()
        cur.close()
    if not row:
        return None
    return float(row[0]) if row[0] is not None else 0.0


def ch_mark_sent(channel_id, post_id, price):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO radar_ch_sent (channel_id, post_id, price) VALUES (%s,%s,%s) "
            "ON CONFLICT (channel_id, post_id) DO UPDATE SET price=EXCLUDED.price, "
            "sent_at=now()", (channel_id, post_id, price))
        cur.close()


def ch_forget(channel_id):
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM radar_ch_sent WHERE channel_id=%s", (channel_id,))
        cur.execute("UPDATE radar_channels SET last_post_id=0 WHERE id=%s", (channel_id,))
        cur.close()


# --- чтение страницы предпросмотра ----------------------------------------------

RE_POST = re.compile(r'data-post="([^"/]+)/(\d+)"')
RE_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
RE_PHOTO = re.compile(r"tgme_widget_message_photo_wrap[^>]*background-image:\s*url\('([^']+)'\)")
RE_TAG = re.compile(r"<[^>]+>")
RE_BR = re.compile(r"<br\s*/?>", re.I)


def _clean(fragment):
    """HTML куска поста -> обычный текст с переносами строк."""
    text = RE_BR.sub("\n", fragment or "")
    text = RE_TAG.sub("", text)
    text = html.unescape(text)
    # неразрывные и прочие «невидимые» пробелы ломают регулярки
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ")
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_page(html_text, username):
    """Разбирает страницу t.me/s/<канал> на посты.

    Страница склеена из блоков, каждый начинается с data-post="канал/номер".
    Режем по этим меткам и внутри куска ищем текст и первую картинку — так
    разбор не рассыпется, если Telegram поменяет что-то в оформлении.
    """
    marks = list(RE_POST.finditer(html_text))
    posts = []
    for i, m in enumerate(marks):
        if m.group(1).lower() != username.lower():
            continue  # чужой пост: репост или реклама
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html_text)
        block = html_text[start:end]

        text_m = RE_TEXT.search(block)
        text = _clean(text_m.group(1)) if text_m else ""
        photo_m = RE_PHOTO.search(block)

        posts.append({
            "post_id": int(m.group(2)),
            "channel": m.group(1),
            "url": f"https://t.me/{m.group(1)}/{m.group(2)}",
            "text": text,
            "photo": photo_m.group(1) if photo_m else None,
        })
    # разные блоки одного альбома дублируют номер поста — оставляем первый
    seen, out = set(), []
    for p in posts:
        if p["post_id"] in seen:
            continue
        seen.add(p["post_id"])
        out.append(p)
    return out


async def fetch_channel(session, username, pages=PAGES_PER_CHECK, stop_at=0):
    """Последние посты канала. Листает вглубь, пока не упрётся в stop_at."""
    username = username.lstrip("@").strip()
    posts, before, title = [], None, None
    for _ in range(max(1, pages)):
        url = TME_BASE + username + (f"?before={before}" if before else "")
        async with session.get(url, headers=TME_HEADERS,
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"t.me ответил {resp.status}")
            page = await resp.text()
        if title is None:
            t = re.search(r'<meta property="og:title" content="([^"]*)"', page)
            title = html.unescape(t.group(1)) if t else username
        chunk = parse_page(page, username)
        if not chunk:
            break
        posts.extend(chunk)
        oldest = min(p["post_id"] for p in chunk)
        if oldest <= stop_at:
            break
        before = oldest
        await asyncio.sleep(0.6)
    posts = [p for p in posts if p["post_id"] > stop_at]
    posts.sort(key=lambda p: p["post_id"])
    return posts, title


# --- разбор текста объявления ---------------------------------------------------

# Цена. Ловим и «45 000 €», и «€45000», и «45.000 euro», и «900 000 lei».
RE_PRICE = re.compile(
    r"(?:(?P<sign1>€|\$|eur|euro|usd|mdl|lei|лей|леев)\s*)?"
    r"(?P<num>\d{1,3}(?:[ ., ]\d{3})+|\d{3,9})"
    r"\s*(?P<sign2>€|\$|eur\b|euro\b|usd\b|mdl\b|lei\b|лей\b|леев\b|евро\b|у\.?е\.?)?",
    re.I)

RE_AREA = re.compile(
    r"(?P<num>\d{1,4}(?:[.,]\d{1,2})?)\s*"
    r"(?:кв\.?\s*м|м\s*[²2]|m\s*[²2]|mp\b|кв\.метр|квадрат)", re.I)
RE_AREA_WORD = re.compile(
    r"(?:площад[ьи]|suprafa[țt]a?|общая)\D{0,12}(?P<num>\d{1,4}(?:[.,]\d{1,2})?)", re.I)

RE_FLOOR = re.compile(
    r"(?:этаж|эт\.?|etaj(?:ul)?|nivel)\D{0,4}(?P<f>\d{1,2})\s*(?:/|из|din|из\s)\s*(?P<t>\d{1,2})",
    re.I)
RE_FLOOR_REV = re.compile(
    r"(?P<f>\d{1,2})\s*/\s*(?P<t>\d{1,2})\s*(?:этаж|эт\.?|etaj)", re.I)

RE_ROOMS = re.compile(
    r"(?:#?(?P<n1>\d)\s*-?\s*(?:комнатн|комн|ком\b|odai|odăi|odai|camere|camer))"
    r"|(?:(?:комнат|odăi|camere)\D{0,4}(?P<n2>\d))", re.I)

# Аренда узнаётся по словам — цену в 650 € отсечёт и общий фильтр, но лучше
# видеть причину отказа в логе.
RENT_WORDS = ("сдает", "сдаёт", "сдам", "сда[её]тся", "аренд", "в месяц", "/мес",
              "за месяц", "chirie", "lunar", "/lun", "pe lun", "посуточн", "суточн",
              "сниму", "куплю", "cumpăr", "cumpar", "обмен", "schimb")
RE_RENT = re.compile("|".join(RENT_WORDS), re.I)

CUR_BY_SIGN = {
    "€": "UNIT_EUR", "eur": "UNIT_EUR", "euro": "UNIT_EUR", "евро": "UNIT_EUR",
    "у.е": "UNIT_EUR", "уе": "UNIT_EUR",
    "$": "UNIT_USD", "usd": "UNIT_USD",
    "mdl": "UNIT_MDL", "lei": "UNIT_MDL", "лей": "UNIT_MDL", "леев": "UNIT_MDL",
}

# Как районы называют в каналах -> как они называются у нас
SECTOR_ALIASES = {
    "центр": "Центр", "centru": "Центр",
    "ботаник": "Ботаника", "botanica": "Ботаника",
    "буюкан": "Буюканы", "buiucani": "Буюканы",
    "рышкановк": "Рышкановка", "рышкан": "Рышкановка", "riscani": "Рышкановка",
    "rîșcani": "Рышкановка", "râșcani": "Рышкановка",
    "чокан": "Чокана", "ciocana": "Чокана",
    "телецентр": "Телецентр", "telecentru": "Телецентр",
    "старая почта": "Старая Почта", "posta veche": "Старая Почта",
    "poșta veche": "Старая Почта",
    "скулянк": "Скулянка", "sculeni": "Скулянка",
    "аэропорт": "Аэропорт", "aeroport": "Аэропорт",
}


def _num(raw):
    if raw is None:
        return None
    cleaned = raw.replace(" ", "").replace(" ", "")
    # «45.000» и «45,000» — это разделитель тысяч, а «55,5» — дробь
    if re.fullmatch(r"\d{1,3}[.,]\d{3}", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_price(text):
    """Самая крупная сумма в тексте и её валюта.

    В посте часто мелькают и номер телефона, и год постройки, и площадь.
    Берём числа, у которых рядом стоит знак валюты, и из них — наибольшее:
    цена объекта в объявлении всегда крупнее прочих чисел.
    """
    best, best_cur = None, None
    for m in RE_PRICE.finditer(text):
        sign = (m.group("sign1") or m.group("sign2") or "").lower().strip().rstrip(".")
        if not sign:
            continue
        value = _num(m.group("num"))
        if value is None or value < 100:
            continue
        cur = CUR_BY_SIGN.get(sign) or CUR_BY_SIGN.get(sign.replace(".", ""))
        if not cur:
            continue
        # в леях суммы на порядок крупнее — сравниваем в евро
        weight = value * (1 / 19.5 if cur == "UNIT_MDL" else 0.92 if cur == "UNIT_USD" else 1)
        if best is None or weight > best[0]:
            best, best_cur = (weight, value), cur
    if best is None:
        return None, ""
    return best[1], best_cur


def extract_area(text):
    m = RE_AREA.search(text) or RE_AREA_WORD.search(text)
    if not m:
        return None
    value = _num(m.group("num"))
    if value is None or not (8 <= value <= 1000):
        return None
    return value


def extract_floor(text):
    m = RE_FLOOR.search(text) or RE_FLOOR_REV.search(text)
    if not m:
        return "", ""
    floor, total = m.group("f"), m.group("t")
    if int(total) > 40 or int(floor) > int(total):
        return "", ""
    return floor, total


def extract_rooms(text):
    m = RE_ROOMS.search(text)
    if m:
        return (m.group("n1") or m.group("n2") or "").strip()
    if re.search(r"студи|garsonier", text, re.I):
        return "1"
    return ""


def extract_sector(text):
    low = text.lower()
    hits = [(low.index(alias), name) for alias, name in SECTOR_ALIASES.items()
            if alias in low]
    if not hits:
        return ""
    hits.sort()
    return hits[0][1]


def looks_like_house(text):
    return bool(re.search(r"\bдом\b|коттедж|особняк|\bcas[ăa]\b|vil[ăa]", text, re.I))


def post_to_ad(post):
    """Пост канала -> словарь того же вида, что объявление с 999.md.

    Дальше он проходит те же проверки и рисуется той же карточкой — отдельной
    логики для каналов нет, значит и расходиться правилам негде.
    """
    text = post.get("text") or ""
    if not text:
        return None

    price, currency = extract_price(text)
    floor, floors_total = extract_floor(text)
    first_line = next((ln for ln in text.split("\n") if len(ln) > 3), text[:80])

    return {
        "id": f"tg:{post['channel']}:{post['post_id']}",
        "title": first_line[:120],
        "price": price,
        "currency": currency,
        "area": extract_area(text),
        "rooms": extract_rooms(text),
        "floor": floor,
        "floors_total": floors_total,
        "author": "",
        # если в тексте есть слова аренды — так и пишем, общий фильтр это увидит
        "offer": "Сдам" if RE_RENT.search(text) else "Продам",
        "sector": extract_sector(text),
        "locality": "Кишинёв",
        "street": "",
        "images": [],
        "photo_url": post.get("photo"),
        "url": post["url"],
        "source": f"@{post['channel']}",
        "is_house": looks_like_house(text),
        "text": text,
    }
