import time

import pytest

from core import db
from core.brain import Brain


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    yield db


@pytest.fixture()
def brain(temp_db):
    return Brain()


class TestReminders:
    def test_set_timer(self, brain, temp_db):
        reply = brain.respond("set a timer for 10 minutes").get("reply", "")
        items = temp_db.list_reminders()
        assert len(items) == 1 and items[0]["kind"] == "timer"
        assert "Timer set" in reply or "timer" in reply.lower()

    def test_reminder_in_minutes(self, brain, temp_db):
        brain.respond("remind me to call mum in 15 minutes")
        items = temp_db.list_reminders()
        assert len(items) == 1
        assert items[0]["message"] == "call mum"
        delta = items[0]["due_at"] - time.time()
        assert 14 * 60 < delta <= 16 * 60

    def test_list_and_cancel(self, brain, temp_db):
        brain.respond("remind me to stretch in 5 minutes")
        listed = brain.respond("what are my reminders")["reply"]
        assert "stretch" in listed
        rid = temp_db.list_reminders()[0]["id"]
        cancelled = brain.respond(f"cancel reminder {rid}")["reply"]
        assert cancelled == "Cancelled."
        assert not temp_db.list_reminders()

    def test_due_fires_once(self, brain, temp_db):
        temp_db.add_reminder(kind="timer", message="", due_at=time.time() - 10)
        first = temp_db.due_reminders(time.time())
        second = temp_db.due_reminders(time.time())
        assert len(first) == 1 and not second


class TestMemory:
    def test_remember_recall(self, brain):
        brain.respond("remember that my favourite colour is blue")
        assert "blue" in brain.respond("what is my favourite colour")["reply"]

    def test_forget(self, brain):
        brain.respond("remember that my pin is 1234")
        assert "Forgotten" in brain.respond("forget my pin")["reply"]
        assert "nothing" in brain.respond("what is my pin")["reply"].lower()

    def test_list_memories(self, brain):
        brain.respond("remember my name is Tony")
        out = brain.respond("what do you remember about me")["reply"]
        assert "Tony" in out


class TestDailyBriefing:
    def test_schedule_daily_briefing(self, brain):
        reply = brain.respond("brief me every day at 8 am")["reply"]
        assert "08:00" in reply
        from core import db as d

        assert d.get_setting("briefing_enabled") == "1"
        assert d.get_setting("briefing_time") == "08:00"

    def test_pm_time(self, brain):
        brain.respond("give me a daily briefing at 9 pm every day")
        from core import db as d

        assert d.get_setting("briefing_time") == "21:00"

    def test_disable_briefing(self, brain):
        brain.respond("brief me every day at 7am")
        assert "off" in brain.respond("disable my daily briefing")["reply"].lower()
        from core import db as d

        assert d.get_setting("briefing_enabled") == "0"


class TestDispatcherBriefing:
    def test_fires_once_per_day(self, temp_db):
        from datetime import datetime

        from core.scheduler import Dispatcher

        temp_db.set_setting("briefing_enabled", "1")
        temp_db.set_setting("briefing_time", "00:00")
        disp = Dispatcher(poll_seconds=3600)
        q = disp.subscribe()
        monkey = __import__("pytest").MonkeyPatch()
        monkey.setattr("core.briefing.fetch_weather", lambda city="": None)
        monkey.setattr("core.pc.system_status", lambda: {"cpu_percent": 1})
        try:
            disp._check_briefing()
            disp._check_briefing()
        finally:
            monkey.undo()
        assert len(q) == 1
        assert "briefing" in q[0]["type"]


class TestChains:
    def test_plan_my_morning(self, brain, monkeypatch):
        monkeypatch.setattr("core.briefing.fetch_weather", lambda city="": None)
        monkeypatch.setattr("core.pc.system_status", lambda: {"cpu_percent": 5, "battery_percent": None})
        opened = []
        monkeypatch.setattr("core.pc.open_target", lambda t: opened.append(t))
        reply = brain.respond("plan my morning")["reply"]
        assert "It is" in reply
        assert "edge" in opened

    def test_focus_mode(self, brain, monkeypatch):
        monkeypatch.setattr("core.pc.volume", lambda a: None)
        monkeypatch.setattr("core.pc.open_target", lambda t: t)
        reply = brain.respond("enter focus mode")["reply"]
        assert "focus mode" in reply.lower()

    def test_goodnight(self, brain):
        assert "Good night" in brain.respond("goodnight jarvis")["reply"]

    def test_diagnostics(self, brain, monkeypatch):
        monkeypatch.setattr(
            "core.pc.system_status",
            lambda: {"cpu_percent": 10, "ram_used_gb": 4, "ram_total_gb": 16, "battery_percent": 50},
        )
        reply = brain.respond("run diagnostics")["reply"]
        assert "CPU" in reply and "Battery" in reply


class TestConversationMemory:
    def test_clear_context(self, brain):
        brain.history.append({"role": "user", "content": "hi"})
        brain.respond("clear the conversation")
        assert not brain.history


class TestControl:
    def test_open_unknown_app(self, brain):
        # Unknown apps now fall back to the web automatically (bias to action).
        reply = brain.respond("open zzzznotrealapp")["reply"].lower()
        assert "isn't an installed app" in reply or "opened it on the web" in reply

    def test_shutdown_requires_confirm(self, brain, monkeypatch):
        fired = []
        monkeypatch.setattr("core.pc.power", lambda a: fired.append(a))
        r1 = brain.respond("shut down the computer")
        assert "confirm" in r1["reply"].lower() and not fired
        brain.respond("cancel")
        assert not fired

    def test_power_confirm_flow(self, brain, monkeypatch):
        fired = []
        monkeypatch.setattr("core.pc.power", lambda a: fired.append(a))
        brain.respond("restart the computer")
        brain.respond("confirm")
        assert fired == ["restart"]

    def test_identity(self, brain):
        assert "assistant" in brain.respond("who are you")["reply"].lower()

    def test_capabilities(self, brain):
        assert "screenshot" in brain.respond("what can you do")["reply"].lower()

    def test_greeting(self, brain):
        assert "online" in brain.respond("hello jarvis")["reply"].lower()
