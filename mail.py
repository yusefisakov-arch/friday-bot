"""Почтовый модуль: сбор писем по IMAP с нескольких ящиков, отсев рутины
правилами (бесплатно) и оценка важности дешёвой моделью.

Доступы задаются переменной окружения MAIL_ACCOUNTS — JSON-список:
[{"label": "Аренда", "user": "x@gmail.com", "pass": "<пароль приложения>", "noisy": true}]
«noisy» — ящик, где 90% почты это рутина (букинг): из него берём только важное.
Пароли приложений создаёт сэр сам, мы в его аккаунты не входим.
"""
import os
import re
import json
import base64
import hashlib
import email
import imaplib
import logging
from email.header import decode_header, make_header
from datetime import datetime, timedelta

from db import db_mail_list

logger = logging.getLogger(__name__)

# Пароли приложений шифруются ключом, производным от токена бота (он и так секретный).
_FERNET = None


def _fernet():
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet
        token = os.environ.get("TELEGRAM_TOKEN", "")
        key = base64.urlsafe_b64encode(hashlib.sha256(("mail-enc:" + token).encode()).digest())
        _FERNET = Fernet(key)
    return _FERNET


def encrypt_secret(plain):
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(blob):
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except Exception as e:
        logger.error(f"Не расшифровать пароль ящика: {e}")
        return ""

IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "mail.ru": "imap.mail.ru",
    "yandex.ru": "imap.yandex.ru",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
}

# Рутина, которую отсекаем бесплатно, до всякой модели.
NOISE_PATTERNS = [
    r"\bновое бронирование\b", r"\bnew booking\b", r"\bbooking confirm",
    r"\bреserv", r"\breservation (confirmed|received|reminder)\b",
    r"\bподтверждение брони", r"\bгость заедет\b", r"\bguest arriv",
    r"\bотзыв о проживании\b", r"\bguest review\b", r"\brate your stay\b",
    r"\bнапоминание о заезде\b", r"\bcheck-?in remind",
    r"\bunsubscribe\b", r"\bотписаться\b", r"\bрассылк", r"\bnewsletter\b",
    r"\bспецпредложен", r"\bскидк", r"\bpromo\b", r"\bsale\b", r"\bакция\b",
    r"\bдайджест\b", r"\bdigest\b", r"\bwebinar\b", r"\bвебинар\b",
]
NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.I)

NOISE_SENDERS = re.compile(
    r"(noreply|no-reply|donotreply|notification|mailer|newsletter|news@|info@booking|"
    r"automated|support@booking|@booking\.com|@airbnb\.|@expedia\.)", re.I)

# Признаки важного — перевешивают отсев рутины даже в шумном ящике.
IMPORTANT_RE = re.compile(
    r"(отмен\w*\s+брон|cancel\w*\s+(booking|reservation)|жалоб|claim|dispute|"
    r"возврат средств|refund|chargeback|штраф|суд|юрист|налог|проверк\w+|"
    r"договор|контракт|счёт на оплату|invoice|оплат\w+ просроч|задолженн|debt|"
    r"срочно|urgent|важно|important|полиц|banca|банк|blocked|заблокирован)", re.I)


def _load_accounts():
    """Ящики: сначала из базы (кнопка «Почта»), иначе из переменной окружения."""
    try:
        rows = db_mail_list(include_secret=True)
    except Exception as e:
        logger.error(f"Ящики из базы: {e}")
        rows = []
    from_db = []
    for r in rows:
        if not r.get("active"):
            continue
        secret = decrypt_secret(r["secret"])
        if secret:
            from_db.append({"label": r.get("label") or r["email"], "user": r["email"],
                            "pass": secret, "noisy": r.get("noisy"),
                            "auth": r.get("auth_type") or "password"})
    if from_db:
        return from_db

    raw = os.environ.get("MAIL_ACCOUNTS", "").strip()
    if not raw:
        return []
    try:
        accounts = json.loads(raw)
    except Exception as e:
        logger.error(f"MAIL_ACCOUNTS: не разобрать JSON: {e}")
        return []
    out = []
    for a in accounts if isinstance(accounts, list) else []:
        if a.get("user") and a.get("pass"):
            out.append(a)
    return out


def _host_for(address):
    domain = address.split("@")[-1].lower()
    return IMAP_HOSTS.get(domain, "imap." + domain)


def _decode(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _body_snippet(msg, limit=400):
    """Короткий текстовый фрагмент письма (без вложений и html-мусора)."""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    payload = part.get_payload(decode=True) or b""
                    text = payload.decode(part.get_content_charset() or "utf-8", "ignore")
                    break
            else:
                text = ""
        else:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", "ignore")
    except Exception:
        text = ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _is_noise(sender, subject, snippet, noisy_box):
    """True — письмо можно выбросить не показывая. Важные признаки перевешивают."""
    blob = f"{subject} {snippet}"
    if IMPORTANT_RE.search(blob):
        return False
    if NOISE_RE.search(blob):
        return True
    if NOISE_SENDERS.search(sender or ""):
        # В шумных ящиках роботы — почти всегда рутина; в обычных пропускаем дальше.
        return bool(noisy_box)
    return False


# ===== Подключение через Google без паролей (OAuth, только чтение) =====

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def google_oauth_ready():
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def _post_form(url, fields):
    import urllib.request
    import urllib.parse
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _api_get(url, access_token):
    import urllib.request
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def oauth_authorize_url(redirect_uri, state):
    """Ссылка на окно «Разрешить» у Google."""
    import urllib.parse
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_SCOPE,
        "access_type": "offline",      # чтобы дали refresh_token
        "prompt": "consent",           # иначе при повторе refresh_token не вернётся
        "state": state,
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def oauth_exchange_code(code, redirect_uri):
    """Код из окна Google → refresh_token + адрес ящика."""
    tokens = _post_form(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })
    refresh = tokens.get("refresh_token")
    access = tokens.get("access_token")
    if not refresh:
        raise RuntimeError("Google не выдал refresh_token — отключи и подключи ящик заново.")
    address = _api_get(f"{GMAIL_API}/profile", access).get("emailAddress", "")
    return refresh, address


def oauth_access_token(refresh_token):
    return _post_form(GOOGLE_TOKEN_URL, {
        "refresh_token": refresh_token,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    })["access_token"]


def fetch_account_oauth(account, hours=24, limit=40):
    """Свежие письма через Gmail API. Права — только чтение."""
    user = account["user"]
    try:
        access = oauth_access_token(account["pass"])
        days = max(1, round(hours / 24))
        listing = _api_get(
            f"{GMAIL_API}/messages?q=in:inbox+newer_than:{days}d&maxResults={limit}", access)
        items = []
        for ref in listing.get("messages", []):
            raw = _api_get(f"{GMAIL_API}/messages/{ref['id']}?format=raw", access).get("raw", "")
            if not raw:
                continue
            msg = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            item = _item_from_msg(msg, account)
            if item:
                items.append(item)
        return items
    except Exception as e:
        logger.error(f"Gmail API {user}: {e}")
        return [{"box": account.get("label") or user, "error": str(e)[:200]}]


def _item_from_msg(msg, account):
    """Письмо → короткая карточка. None, если это шум."""
    sender = _decode(msg.get("From"))
    subject = _decode(msg.get("Subject"))
    snippet = _body_snippet(msg)
    if _is_noise(sender, subject, snippet, account.get("noisy")):
        return None
    return {
        "box": account.get("label") or account["user"],
        "from": sender[:120],
        "subject": subject[:200],
        "snippet": snippet,
        "date": _decode(msg.get("Date"))[:40],
    }


def fetch_account(account, hours=24, limit=40):
    """Свежие письма одного ящика. Возвращает список словарей (уже без явной рутины)."""
    user = account["user"]
    host = account.get("host") or _host_for(user)
    since = (datetime.now() - timedelta(hours=hours)).strftime("%d-%b-%Y")
    items = []
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, 993)
        conn.login(user, account["pass"])
        conn.select("INBOX", readonly=True)  # readonly — писем не «прочитываем»
        typ, data = conn.search(None, f'(SINCE "{since}")')
        if typ != "OK":
            return items
        ids = (data[0] or b"").split()[-limit:]
        for num in reversed(ids):
            typ, msg_data = conn.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            item = _item_from_msg(email.message_from_bytes(msg_data[0][1]), account)
            if item:
                items.append(item)
    except imaplib.IMAP4.error as e:
        logger.error(f"IMAP {user}: {e}")
        return [{"box": account.get("label") or user, "error": f"не удалось войти: {e}"}]
    except Exception as e:
        logger.error(f"Почта {user}: {e}")
        return [{"box": account.get("label") or user, "error": str(e)}]
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return items


def test_login(address, password):
    """Проверить вход в ящик. Возвращает (ок, понятное человеку сообщение)."""
    host = _host_for(address)
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(host, 993)
        conn.login(address, password)
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "ALL")
        n = len((data[0] or b"").split()) if typ == "OK" else 0
        return True, f"Подключено ✓ Писем в папке: {n}"
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "Invalid credentials" in msg or "AUTHENTICATIONFAILED" in msg:
            return False, ("Не принял пароль. Нужен именно «пароль приложения» "
                           "(16 символов из myaccount.google.com/apppasswords), а не обычный пароль.")
        return False, f"Почта отказала: {msg[:150]}"
    except Exception as e:
        return False, f"Не удалось подключиться к {host}: {str(e)[:150]}"
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass


def fetch_all(hours=24):
    """Письма со всех настроенных ящиков после отсева рутины правилами."""
    accounts = _load_accounts()
    if not accounts:
        return None, []
    out = []
    for a in accounts:
        reader = fetch_account_oauth if a.get("auth") == "oauth" else fetch_account
        out.extend(reader(a, hours=hours))
    return accounts, out
