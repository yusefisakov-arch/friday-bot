"""Тесты кнопочной постановки задач.

Главный инвариант ТЗ: время нигде не вводится с клавиатуры — на каждом экране
(кроме wait_title, где пишут суть задачи) всё делается кнопками. Проверяем, что
у каждой кнопки есть callback_data из пространства new: и нигде нет ForceReply.

Не требует ни Postgres, ни токена: psycopg2 подменяется заглушкой, экраны с
person_id=None не ходят в базу.
"""
import os
import sys
import types

os.environ["TELEGRAM_TOKEN"] = "test:token"
os.environ["DATABASE_URL"] = "postgresql://test/test"
os.environ["ALLOWED_USER_ID"] = "111"

_fake_pg = types.ModuleType("psycopg2")
_fake_pool = types.ModuleType("psycopg2.pool")
_fake_pool.ThreadedConnectionPool = lambda *a, **k: None
_fake_pg.pool = _fake_pool
sys.modules.setdefault("psycopg2", _fake_pg)
sys.modules.setdefault("psycopg2.pool", _fake_pool)

import crewmenu as CM

ALL_STEPS = [
    "wait_title", "pick_due_day", "pick_due_date", "pick_due_hour", "confirm",
    "pick_freq", "pick_weekday", "pick_days", "pick_start_hour",
    "pick_fix_due", "pick_fix_due_hour", "final",
]


def _draft(step):
    return {"step": step, "person_id": None, "title": "свести кассу",
            "due_at": None, "pick_date": __import__("datetime").date(2026, 9, 25),
            "week_shift": 0, "weekdays": "24", "hour": 9, "minute": 0,
            "due_hour": 10, "due_minute": 0}


def test_every_screen_is_buttons_only():
    """Ни одного экрана без кнопок, все кнопки — callback new:."""
    for step in ALL_STEPS:
        _text, markup = CM._screen(_draft(step))
        rows = markup.inline_keyboard
        assert rows, f"{step}: нет кнопок"
        for row in rows:
            for btn in row:
                assert btn.callback_data, f"{step}: кнопка «{btn.text}» без callback"
                assert btn.callback_data.startswith("new:"), \
                    f"{step}: чужой callback {btn.callback_data}"
                assert btn.url is None, f"{step}: внешняя ссылка на кнопке"


def test_no_keyboard_time_entry():
    """Часы выбираются из сетки, минут в вводе нет — только :00 и 23:59."""
    grid = CM.HOUR_GRID
    assert grid == [8, 9, 10, 11, 12, 14, 16, 18, 20, 22]
    # часовая клавиатура: все варианты оканчиваются на 00, кроме конца дня 2359
    kb = CM._hour_keyboard("hour").inline_keyboard
    codes = [b.callback_data.split(":")[2] for row in kb for b in row
             if b.callback_data.startswith("new:hour:")]
    for c in codes:
        assert c.endswith("00") or c == "2359", f"странный час: {c}"


def test_hhmm_parsing():
    assert CM._hhmm("0800") == (8, 0)
    assert CM._hhmm("2359") == (23, 59)
    assert CM._hhmm("1400") == (14, 0)


def test_weekdays_label():
    assert CM._weekdays_label("1234567") == "каждый день"
    assert CM._weekdays_label("12345") == "по будням"
    assert CM._weekdays_label("24") == "по Вт, Чт"


def test_back_targets_are_known_steps():
    for src, dst in CM.BACK.items():
        assert src in ALL_STEPS, f"BACK из неизвестного шага {src}"
        assert dst in ALL_STEPS, f"BACK ведёт в неизвестный шаг {dst}"


def test_date_list_has_seven_days_and_shift():
    kb = CM._screen(_draft("pick_due_date"))[1].inline_keyboard
    dates = [b for row in kb for b in row if b.callback_data.startswith("new:date:")]
    assert len(dates) == 7, "должно быть 7 дней"
    assert any(b.callback_data == "new:week:1" for row in kb for b in row), \
        "нет кнопки «Ещё неделя»"


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
