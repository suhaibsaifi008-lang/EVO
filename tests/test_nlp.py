from datetime import datetime

from core.brain import try_math
from core.nlp import parse_clock_time, parse_duration


def test_duration_minutes():
    assert parse_duration("in 10 minutes") == __import__("datetime").timedelta(minutes=10)


def test_duration_hours_and_words():
    from datetime import timedelta

    assert parse_duration("set a timer for 2 hours") == timedelta(hours=2)
    assert parse_duration("remind me in half an hour") == timedelta(minutes=30)


def test_clock_time_pm():
    now = datetime(2026, 1, 1, 10, 0)
    due = parse_clock_time("remind me at 5pm", now)
    assert (due.hour, due.minute) == (17, 0)
    assert due > now


def test_clock_time_tomorrow():
    from datetime import timedelta

    now = datetime(2026, 1, 1, 22, 0)
    due = parse_clock_time("alarm at 7am tomorrow", now)
    assert (due.hour, due.minute) == (7, 0)
    assert due.date() == (now + timedelta(days=1)).date()


def test_no_time_found():
    assert parse_clock_time("hello world") is None
    assert parse_duration("nothing here") is None


def test_math_word_operators():
    assert "12" in try_math("what is 5 plus 7")
    assert "20" in try_math("what is 4 times 5")
    assert "3" in try_math("what is 12 / 4")


def test_math_percent_of():
    assert "30" in try_math("what is 20% of 150")


def test_math_rejects_non_arithmetic():
    assert try_math("what is the meaning of life") is None
