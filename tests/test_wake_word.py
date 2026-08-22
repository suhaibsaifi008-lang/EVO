import pytest

from core import db
from core.listener import match_wake_phrase, normalize_text


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

    def test_unrelated_speech_does_not_trigger(self):
        assert match_wake_phrase("what time is it") is None
        assert match_wake_phrase("play some music please") is None
        assert match_wake_phrase("") is None

    def test_custom_phrase_list(self):
        assert match_wake_phrase("yo assistant status", phrases=["yo assistant"]) == "status"


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
