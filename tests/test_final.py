import json
import sys
import time

import pytest

from core import db


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield db


class TestTones:
    def test_detect_tone(self):
        from core import tts

        assert tts.detect_tone("WARNING: battery low") == "alert"
        assert tts.detect_tone("good morning sir") == "normal"

    def test_tone_changes_request(self, tmp_path, monkeypatch):
        from core import tts

        monkeypatch.setattr(tts, "TTS_DIR", tmp_path / "tts")
        tts.TTS_DIR.mkdir(exist_ok=True)
        captured = []

        class FakeCom:
            def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
                captured.append((rate, pitch))

            async def save(self, path):
                with open(path, "wb") as f:
                    f.write(b"\xff\xfb" + b"x" * 1024)

        import edge_tts

        monkeypatch.setattr(edge_tts, "Communicate", FakeCom)
        out1 = tts.synthesize("hello there", tone="calm")
        out2 = tts.synthesize("hello there", tone="alert")
        assert out1 != out2
        rates = {r for r, _ in captured}
        pitches = {p for _, p in captured}
        assert len(rates) == 2 and len(pitches) == 2


class TestFeedbackMemory:
    def test_from_now_on_stores(self, temp_db):
        from core.brain import Brain

        brain = Brain()
        brain.pending.clear()
        reply = brain.respond("From now on always address me as captain")["reply"]
        assert "Noted permanently" in reply
        rows = db.list_corrections()
        assert any("captain" in r["instruction"] for r in rows)

    def test_never_mind_not_stored(self, temp_db):
        from core.brain import Brain

        brain = Brain()
        brain.pending.clear()
        brain.respond("never mind")
        assert not db.list_corrections()

    def test_correction_injected_into_agent(self, temp_db):
        db.add_correction("", "always address the user as captain")
        from core import agent_loop, tools

        monkey_lines = lambda n, a: "-"
        import core.agent_loop as al

        original_call = tools.call
        import core.tools as t

        t.call = monkey_lines
        try:
            system = al.build_system("address me as captain please")
        finally:
            t.call = original_call
        assert "Standing instructions" in system and "captain" in system


class TestHabits:
    def test_classify_buckets(self):
        from core import habits

        assert habits.classify("remind me to call mum") == "schedule"
        assert habits.classify("open chrome") == "launch"
        assert habits.classify("research quantum computing") == "research"
        assert habits.classify("tell me a story about dragons") == "conversation"

    def test_record_and_top(self, temp_db):
        from core import habits

        habits.record("remind me x")
        habits.record("remind me y")
        habits.record("open chrome")
        tops = db.top_habits(3)
        assert tops[0]["category"] == "schedule" and tops[0]["count"] == 2

    def test_repeat_proposal_once(self, temp_db, monkeypatch):
        from core import habits

        captured = []
        monkeypatch.setattr(habits.dispatcher, "publish", lambda e: captured.append(e))
        phrase = "what is the weather in tokyo today"
        for i in range(3):
            habits.maybe_propose_skill(phrase)
        assert len(captured) == 1 and "skill" in captured[0]["text"].lower()
        habits.maybe_propose_skill(phrase)
        assert len(captured) == 1


class TestRouting:
    def test_simple_query_detection(self):
        from core.agent_loop import is_simple_query

        assert is_simple_query("what time is it")
        assert not is_simple_query("open chrome now")
        assert not is_simple_query("please remind me to stretch in ten minutes")

    def test_model_override_reaches_payload(self, temp_db, monkeypatch):
        from core import llm

        seen = {}
        monkeypatch.setattr(
            llm,
            "_completion",
            lambda base, key, payload, timeout=60: seen.update(model=payload.get("model"))
            or {"choices": [{"message": {"content": "ok"}}]},
        )
        llm.chat([{"role": "user", "content": "hi"}], model="fast-model-x")
        assert seen["model"] == "fast-model-x"


def _ics_sample() -> str:
    from datetime import datetime, timedelta

    d1 = (datetime.now() + timedelta(days=1)).strftime("%Y%m%dT090000Z")
    d2 = (datetime.now() + timedelta(days=5)).strftime("%Y%m%d")
    return (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        f"DTSTART:{d1}\n"
        f"DTEND:{d1[:8]}T093000Z\n"
        "SUMMARY:Standup meeting\n"
        "LOCATION:Room 4\n"
        "DESCRIPTION:Daily sync\n"
        "END:VEVENT\n"
        "BEGIN:VEVENT\n"
        f"DTSTART;VALUE=DATE:{d2}\n"
        "SUMMARY:Conference day\n"
        "END:VEVENT\n"
        "END:VCALENDAR"
    )


class FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCalendar:
    def test_unconfigured_raises(self, temp_db):
        from core import calendarx

        with pytest.raises(RuntimeError):
            calendarx.fetch_events()

    def test_parse_and_format(self, temp_db, monkeypatch):
        from core import calendarx

        db.set_setting("calendar_ical_url", "https://example.com/cal.ics")
        monkeypatch.setattr(calendarx.urllib.request, "urlopen", lambda req, timeout=20: FakeResp(_ics_sample().encode()))
        events = calendarx.fetch_events(days=30)
        titles = [e.get("title") for e in events]
        assert "Standup meeting" in titles and "Conference day" in titles
        formatted = calendarx.format_events(events)
        assert "Standup meeting" in formatted and "Room 4" in formatted

    def test_next_event_line(self, temp_db, monkeypatch):
        from core import calendarx

        db.set_setting("calendar_ical_url", "https://example.com/cal.ics")
        monkeypatch.setattr(calendarx.urllib.request, "urlopen", lambda req, timeout=20: FakeResp(_ics_sample().encode()))
        line = calendarx.next_event_line()
        assert "Standup" in line or line == ""


class TestYouTube:
    def test_summary_tool(self, temp_db, monkeypatch):
        fake_mod = types_module()

        class FakeAPI:
            def fetch(self, video_id):
                class Snip:
                    text = "quantum computing explained simply for beginners"

                return [Snip(), Snip()]

        fake_mod.YouTubeTranscriptApi = FakeAPI
        monkeypatch.setitem(sys.modules, "youtube_transcript_api", fake_mod)
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: "SUMMARY TEXT HERE")
        from core import tools

        out = tools.call("youtube_summary", {"url": "https://youtu.be/abc12345678"})
        assert "SUMMARY TEXT HERE" in out


def types_module():
    import types

    return types.ModuleType("youtube_transcript_api")


class TestSmartHome:
    def test_unconfigured(self, temp_db, monkeypatch):
        from core import config, smarthome

        monkeypatch.setattr(config, "HA_URL", "")
        assert "NOT CONFIGURED" in smarthome.call_service("light", "turn_on")

    def test_call_service(self, temp_db, monkeypatch):
        from core import config, smarthome

        monkeypatch.setattr(config, "HA_URL", "http://ha.local:8123")
        monkeypatch.setattr(config, "HA_TOKEN", "tok")
        seen = {}

        class Resp(FakeResp):
            def read(self):
                return b"[]"

        def fake_urlopen(req, timeout=15):
            seen["url"] = req.full_url
            seen["data"] = req.data
            return Resp(b"")

        monkeypatch.setattr(smarthome.urllib.request, "urlopen", fake_urlopen)
        out = smarthome.call_service("light", "turn_on", "light.desk")
        assert "Done" in out and "light.desk" in out
        assert "/api/services/light/turn_on" in seen["url"]
        assert json.loads(seen["data"])["entity_id"] == "light.desk"


class TestNotify:
    def test_no_topic_false(self, temp_db, monkeypatch):
        from core import config, notify

        monkeypatch.setattr(config, "NTFY_TOPIC", "")
        assert notify.push("t", "m") is False

    def test_push_true(self, temp_db, monkeypatch):
        from core import config, notify

        monkeypatch.setattr(config, "NTFY_TOPIC", "evo-test-topic")
        seen = {}
        monkeypatch.setattr(notify.urllib.request, "urlopen", lambda req, timeout=10: seen.update(url=req.full_url) or FakeResp(b""))
        assert notify.push("Hello", "Body") is True
        assert "ntfy.sh/evo-test-topic" in seen["url"]


class TestReports:
    def test_chart_png(self, temp_db, tmp_path):
        from core import reports

        monkey_target = tmp_path / "reports"
        reports.REPORTS_DIR = monkey_target
        reports.REPORTS_DIR.mkdir(exist_ok=True)
        out = reports.make_chart("Weekly Sales", "Mon:12; Tue:30; Wed:7", "bar")
        path = tmp_path / "reports" / "Weekly-Sales.png"
        assert path.exists() and path.stat().st_size > 1000
        assert "Chart saved" in out

    def test_pdf_document(self, temp_db, tmp_path):
        from core import reports

        reports.REPORTS_DIR = tmp_path / "reports2"
        reports.REPORTS_DIR.mkdir(exist_ok=True)
        out = reports.make_pdf("Quarter Report", "Intro paragraph here.\n\n- point one\n- point two\n\nConclusion.")
        path = tmp_path / "reports2" / "Quarter-Report.pdf"
        assert path.exists() and path.stat().st_size > 500
        assert path.read_bytes()[:5] == b"%PDF-"


class TestWelcomeBack:
    def test_transition_publishes_once(self, temp_db, monkeypatch):
        import time as _time

        from core.scheduler import Dispatcher

        d = Dispatcher(poll_seconds=999)
        captured = []
        monkeypatch.setattr(d, "publish", lambda e: captured.append(e))
        d._last_idle = 2000
        d._welcome_transition(30)
        assert len(captured) == 1 and "Welcome back" in captured[0]["text"]
        d._welcome_transition(30)
        assert len(captured) == 1
        # Simulate four hours passing: both the in-memory and persisted guards.
        d._last_welcome -= 5 * 3600
        temp_db.set_setting("last_welcome_ts", str(_time.time() - 5 * 3600))
        d._last_idle = 3000
        d._welcome_transition(45)
        assert len(captured) == 2


class TestBriefingV2:
    def test_includes_calendar(self, temp_db, monkeypatch):
        import core.calendarx as cal
        from core.briefing import compose

        monkeypatch.setattr(cal, "next_event_line", lambda: "Your next commitment is Standup at 09:00.")
        text = compose()
        assert "Standup" in text


class TestListenerHelpers:
    def test_rms_positive(self):
        from core.listener import _rms

        frame = (b"\x00\x40" * 100)
        assert _rms(frame) > 0
        assert _rms(b"\x00\x00" * 100) == 0

    def test_module_imports_clean(self):
        import core.listener  # noqa: F401

