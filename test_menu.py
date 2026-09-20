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
import crew
import crewbot
from datetime import timedelta
from core import now_local

ALL_STEPS = [
    "wait_title", "confirm", "pd_date", "pd_hour", "pd_min",
    "pick_freq", "pick_weekday", "pick_days", "pick_monthday",
    "tod_hour", "tod_min", "pick_fix_due", "final",
]


def _draft(step):
    return {"step": step, "person_id": None, "title": "свести кассу",
            "due_at": None, "send_at": None, "editing": "due",
            "week_shift": 0, "weekdays": "24", "monthday": 15,
            "hour": 9, "minute": 0, "due_hour": 10, "due_minute": 0}


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


def test_hour_grid_is_full_0_23():
    """Часы выбираются тапом, полная сетка 0–23."""
    for step, prefix in (("pd_hour", "phour"), ("tod_hour", "thour")):
        kb = CM._screen(_draft(step))[1].inline_keyboard
        codes = {b.callback_data for row in kb for b in row}
        for h in range(24):
            assert f"new:{prefix}:{h}" in codes, f"{step}: нет часа {h}"


def test_min_grid_quarters():
    """Минуты — :00/:15/:30/:45 тапом."""
    for step, prefix in (("pd_min", "pmin"), ("tod_min", "tmin")):
        kb = CM._screen(_draft(step))[1].inline_keyboard
        codes = {b.callback_data for row in kb for b in row}
        for m in (0, 15, 30, 45):
            assert f"new:{prefix}:{m}" in codes, f"{step}: нет минут {m}"


def test_confirm_has_send_and_due():
    """Хаб разовой: можно задать и отправку, и срок."""
    kb = CM._screen(_draft("confirm"))[1].inline_keyboard
    codes = {b.callback_data for row in kb for b in row}
    for need in ("new:setsend", "new:setdue", "new:once", "new:fix"):
        assert need in codes, f"нет {need}"


def test_typed_time_is_parsed():
    """Срок, написанный словами, распознаётся и вырезается из названия."""
    due, title = crew.parse_due_explicit("свести кассу к 17:00")
    assert due is not None, "«к 17:00» должно распознаться"
    assert "17" not in title and "касс" in title.lower()

    due2, _ = crew.parse_due_explicit("помыть склад через 2 часа")
    assert due2 is not None, "«через 2 часа» должно распознаться"


def test_no_time_means_buttons():
    """Без указания срока — None, дальше выбор кнопками."""
    due, title = crew.parse_due_explicit("просто убрать склад")
    assert due is None
    assert title == "просто убрать склад"


def test_report_deadline_future_due():
    """Есть срок задачи в будущем — отчёт ждём к нему."""
    due = now_local() + timedelta(hours=3)
    assert crewbot._report_deadline({"due_at": due}) == due


def test_report_deadline_end_of_day_when_no_due():
    """Срока нет — отчёт ждём к концу дня."""
    d = crewbot._report_deadline({"due_at": None})
    assert (d.hour, d.minute) == (23, 59)


def test_title_no_dangling_filler():
    """«...общие зоны выполнить до 21:30» → в названии нет хвоста «выполнить/до»."""
    due, title = crew.parse_due_explicit("вечерний обход общие зоны выполнить до 21:30")
    assert due is not None
    low = title.lower()
    assert not low.endswith("выполнить") and not low.endswith("до")
    assert "обход" in low


def test_crewmenu_short_title():
    assert CM._short("короткое") == "короткое"
    assert CM._short("строка1\nстрока2") == "строка1 строка2"
    assert CM._short("я" * 80).endswith("…")


def test_extend_base_future_due():
    """Перенос считается от срока задачи, если он ещё не прошёл."""
    due = now_local() + timedelta(hours=5)
    assert crewbot._extend_base({"due_at": due}) == due


def test_extend_base_past_uses_now():
    """Срок прошёл — считаем от текущего момента."""
    due = now_local() - timedelta(hours=5)
    base = crewbot._extend_base({"due_at": due})
    assert abs((base - now_local()).total_seconds()) < 5


def test_short_title():
    """Напоминание берёт только первую строку и обрезает длинное."""
    assert crewbot._short("свести кассу") == "свести кассу"
    assert crewbot._short("первая строка\nвторая строка") == "первая строка"
    long = "а" * 80
    s = crewbot._short(long)
    assert len(s) <= 51 and s.endswith("…")


def test_weekdays_label():
    assert CM._weekdays_label("1234567") == "каждый день"
    assert CM._weekdays_label("12345") == "по будням"
    assert CM._weekdays_label("24") == "по Вт, Чт"


def test_month_schedule_label():
    assert CM._sched_label({"monthday": 15}) == "15 числа каждый месяц"
    assert CM._sched_label({"monthday": 99}) == "в последний день месяца"
    assert CM._sched_label({"weekdays": "12345", "monthday": None}) == "по будням"


def test_monthday_grid_has_days_and_last():
    kb = CM._screen(_draft("pick_monthday"))[1].inline_keyboard
    codes = {b.callback_data for row in kb for b in row}
    assert "new:mday:1" in codes and "new:mday:28" in codes
    assert "new:mday:99" in codes


def test_back_targets_are_known_steps():
    for src, dst in CM.BACK.items():
        assert src in ALL_STEPS, f"BACK из неизвестного шага {src}"
        assert dst in ALL_STEPS, f"BACK ведёт в неизвестный шаг {dst}"


def test_pd_date_grid_has_days_and_week_nav():
    kb = CM._screen(_draft("pd_date"))[1].inline_keyboard
    dates = [b for row in kb for b in row if b.callback_data.startswith("new:pdate:")]
    assert len(dates) >= 7, "минимум 7 дней"
    assert any(b.callback_data == "new:pweek:1" for row in kb for b in row), \
        "нет листания недель"


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
