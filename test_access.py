"""Тесты контроля доступа: whitelist и замок на личку.

Запуск: python3 test_access.py   (или pytest test_access.py)

Не требует ни Postgres, ни токена: пул БД подменяется заглушкой до импорта
core, переменные окружения выставляются здесь же. Цель — чтобы случайная
правка не открыла бота посторонним незаметно.
"""
import asyncio
import os
import sys
import types

# --- окружение до импорта core: владелец 111, доверенные 222 и 333 ---
os.environ["TELEGRAM_TOKEN"] = "test:token"
os.environ["DATABASE_URL"] = "postgresql://test/test"
os.environ["ALLOWED_USER_ID"] = "111"
os.environ["ALLOWED_USER_IDS"] = "222, 333"

# core создаёт пул соединений прямо при импорте. psycopg2 может быть не
# установлен (и не нужен для проверки доступа) — подменяем его заглушкой.
_fake_pg = types.ModuleType("psycopg2")
_fake_pool = types.ModuleType("psycopg2.pool")
_fake_pool.ThreadedConnectionPool = lambda *a, **k: None
_fake_pg.pool = _fake_pool
sys.modules.setdefault("psycopg2", _fake_pg)
sys.modules.setdefault("psycopg2.pool", _fake_pool)

import core
import bot
from telegram.ext import ApplicationHandlerStop
from types import SimpleNamespace as NS


def _update(chat_type, uid):
    user = NS(id=uid) if uid is not None else None
    return NS(effective_user=user, effective_chat=NS(type=chat_type))


def _gate_blocks(chat_type, uid):
    """True, если замок отсёк апдейт (бот промолчит)."""
    try:
        asyncio.run(bot.gate(_update(chat_type, uid), None))
        return False
    except ApplicationHandlerStop:
        return True


def test_owner_only_is_owner():
    assert core.is_owner(111)
    assert not core.is_owner(222)   # доверенный — не владелец
    assert not core.is_owner(999)


def test_whitelist_is_allowed():
    for uid in (111, 222, 333):
        assert core.is_allowed(uid), f"{uid} должен иметь доступ"
    assert not core.is_allowed(999), "посторонний не должен иметь доступ"
    assert not core.is_allowed(0)


def test_parse_ids_ignores_garbage():
    assert core._parse_ids("@777 ; abc, 888") == {777, 888}
    assert core._parse_ids("") == set()
    assert core._parse_ids(None) == set()


def test_model_for_is_owner_model():
    assert core.model_for(111) == core.MODEL_OWNER


def test_gate_blocks_stranger_in_private():
    assert _gate_blocks("private", 999) is True


def test_gate_blocks_anonymous_in_private():
    assert _gate_blocks("private", None) is True


def test_gate_allows_owner_and_trusted_in_private():
    assert _gate_blocks("private", 111) is False
    assert _gate_blocks("private", 222) is False


def test_gate_lets_group_through():
    # в группе сотрудники должны мочь жать кнопки — замок не мешает
    assert _gate_blocks("group", 999) is False
    assert _gate_blocks("supergroup", 999) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} прошло")
    raise SystemExit(1 if failed else 0)
