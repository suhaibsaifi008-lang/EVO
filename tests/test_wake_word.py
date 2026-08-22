import pytest

from core import db
from core.listener import is_exit_phrase, match_wake_phrase, normalize_text
from core.agent_loop import SYSTEM_TEMPLATE


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture(autouse=True)
def default_phrases(monkeypatch):
    monkeypatch.setattr("core.listener.WAKE_PHRASES", ["wake up evo", "wake up e.v.o"])


class TestNormalize:
    def test_strips_punctuation_and_case(self):
        assert normalize_text("Wake UP, E.V.O!") == "wake up e v o"

    def test_empty(self):
        assert normalize_text("") == ""
        assert normalize_text(None) == ""


class TestMatchWakePhrase:
    def test_exact_phrase_alone(self):
        assert match_wake_phrase("wake up evo") == ""

    def test_phrase_with_command_in_same_breath(self):
        rest = match_wake_phrase("wake up evo what's the weather")
        assert rest is not None
        assert "weather" in rest

    def test_embedded_in_longer_utterance(self):
        assert match_wake_phrase("hey could you wake up evo please") is not None

    def test_misheard_variant_still_triggers(self):
        # Vosk may transcribe 'evo' as 'eva'/'evil' - fuzzy window must catch it.
        assert match_wake_phrase("wake up eva open chrome") is not None
        assert match_wake_phrase("woke up evo") is not None

    def test_merged_speech_still_triggers(self):
        # Vosk often merges words: "wakeup evo" (2 tokens) must still wake.
        rest = match_wake_phrase("wakeup evo")
        assert rest == ""

    def test_unrelated_speech_does_not_trigger(self):
        assert match_wake_phrase("what time is it") is None
        assert match_wake_phrase("play some music please") is None
        assert match_wake_phrase("") is None

    def test_custom_phrase_list(self):
        assert match_wake_phrase("yo assistant status", phrases=["yo assistant"]) == "status"


class TestExitPhrases:
    def test_exit_phrases_match(self):
        assert is_exit_phrase("evo stop listening now")
        assert is_exit_phrase("goodbye")
        assert is_exit_phrase("that will be all for today")

    def test_normal_commands_are_not_exits(self):
        assert not is_exit_phrase("open chrome")
        assert not is_exit_phrase("what's the weather")


class TestAgentPromptActsInsteadOfLecturing:
    def test_prompt_forbids_reciting_tools(self):
        lowered = SYSTEM_TEMPLATE.lower()
        assert "never recite" in lowered
        assert "never mention internal steps" in lowered or "act, then report" in lowered


class TestDispatcherNoReplay:
    def test_new_subscriber_does_not_receive_backlog(self, temp_db):
        from core.scheduler import Dispatcher

        disp = Dispatcher(poll_seconds=3600)
        disp.publish({"type": "welcome", "kind": "welcome", "text": "Welcome back."})
        q = disp.subscribe()
        assert len(q) == 0  # would have replayed forever before the fix

    def test_live_events_still_delivered(self, temp_db):
        from core.scheduler import Dispatcher

        disp = Dispatcher(poll_seconds=3600)
        q = disp.subscribe()
        disp.publish({"type": "note", "text": "live"})
        assert len(q) == 1 and q[0]["type"] == "note"

    def test_duplicate_announcements_suppressed(self, temp_db):
        from core.scheduler import Dispatcher

        disp = Dispatcher(poll_seconds=3600)
        q = disp.subscribe()
        for _ in range(3):
            disp.publish({"type": "welcome", "kind": "welcome", "text": "Welcome back, sir."})
        assert len(q) == 1  # identical proactive announcements deduped
        disp.publish({"type": "welcome", "kind": "welcome", "text": "Welcome back, madam."})
        assert len(q) == 2  # different text still passes

    def test_chat_replies_never_deduped(self, temp_db):
        from core.scheduler import Dispatcher

        disp = Dispatcher(poll_seconds=3600)
        q = disp.subscribe()
        for _ in range(3):
            disp.publish({"type": "voice_exchange", "text": "It is 10:00."})
        assert len(q) == 3  # spoken replies to the user must always go through

    def test_welcome_guard_survives_restart(self, temp_db):
        """A server restart must not re-greet: the guard lives in the DB."""
        from core.scheduler import Dispatcher

        def cycle():
            d = Dispatcher(poll_seconds=3600)  # fresh instance == restart
            d._welcome_transition(2000)  # away
            d._welcome_transition(30)  # back -> would greet
            return d

        first = cycle()
        assert float(first._last_welcome) > 0  # greeted this time
        second = cycle()
        assert second._last_welcome == 0.0  # suppressed by persisted guard


class TestWakePhrasesEndpoint:
    def test_endpoint_lists_phrases(self, temp_db):
        from fastapi.testclient import TestClient

        from main import app

        with TestClient(app) as client:
            r = client.get("/api/wake-phrases")
            assert r.status_code == 200
            phrases = r.json()["phrases"]
            assert isinstance(phrases, list) and phrases
            assert any("evo" in p for p in phrases)


class TestRootPage:
    def test_console_page_serves(self, temp_db):
        """Regression: index() once raised NameError -> full-page 500."""
        from fastapi.testclient import TestClient

        from main import app

        with TestClient(app) as client:
            r = client.get("/")
            assert r.status_code == 200
            assert "EVO" in r.text
            assert "app.js" in r.text
