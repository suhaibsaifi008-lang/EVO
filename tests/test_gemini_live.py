"""Tests for the Gemini Live bridge — all offline-safe (no network calls)."""
import queue

import pytest

from core import db


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture()
def clean_env(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_GEMINI_KEY", raising=False)
    monkeypatch.setenv("JARVIS_VOICE_ENGINE", "auto")


class TestKeyResolution:
    def test_no_key_means_disabled(self, clean_env):
        from core import gemini_live as gl

        assert gl.gemini_key() == ""
        assert gl.live_enabled() is False

    def test_key_from_gemini_api_key(self, clean_env, monkeypatch):
        from core import gemini_live as gl

        monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
        assert gl.gemini_key() == "test-key-123"
        assert gl.live_enabled() is True

    def test_legacy_jarvis_var(self, clean_env, monkeypatch):
        from core import gemini_live as gl

        monkeypatch.setenv("JARVIS_GEMINI_KEY", "legacy-key")
        assert gl.gemini_key() == "legacy-key"

    def test_engine_override_forces_offline(self, clean_env, monkeypatch):
        from core import gemini_live as gl

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("JARVIS_VOICE_ENGINE", "vosk")
        assert gl.live_enabled() is False


class TestExitDetection:
    def test_exit_phrases(self):
        from core.gemini_live import is_exit_text

        assert is_exit_text("okay goodbye then")
        assert is_exit_text("EVO stop listening now")
        assert not is_exit_text("what time is it")


class TestSpeakerBuffer:
    def test_play_and_interrupt_without_stream(self):
        """Playback buffer works even before an audio device opens."""
        from core.gemini_live import OUTPUT_RATE, Speaker

        s = Speaker()
        s.play(b"\x01\x00" * 1000)
        with_attr = getattr(s, "_buf")
        assert len(with_attr) == 2000
        s.interrupt()
        assert len(s._buf) == 0

    def test_backlog_guard_drops_tail(self):
        from core.gemini_live import OUTPUT_RATE, Speaker

        s = Speaker()
        huge = b"\x00\x00" * OUTPUT_RATE * 30  # 30 seconds
        s.play(huge)
        assert len(s._buf) <= OUTPUT_RATE * 2 * 20 + OUTPUT_RATE * 2  # capped ~20s


class TestSessionLifecycle:
    def test_start_fails_gracefully_without_key(self, clean_env):
        from core import gemini_live as gl

        sess = gl.LiveVoiceSession(queue.Queue())
        assert sess.start() is False
        assert "gemini unavailable" in sess.last_error.lower() or "api" in sess.last_error.lower()
        assert sess.stopped is False  # not marked stopped; caller just falls back

    def test_stop_sets_flag(self, clean_env, monkeypatch):
        from core import gemini_live as gl

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        sess = gl.LiveVoiceSession(queue.Queue())
        sess.stop()
        assert sess.stopped is True

    def test_feed_before_start_is_harmless(self, clean_env):
        from core import gemini_live as gl

        q = queue.Queue()
        sess = gl.LiveVoiceSession(q)
        sess.feed(b"\x00\x00")  # no _audio_in yet: must not raise
        assert q.empty()


class TestHealthReportsEngine:
    def test_health_includes_voice_engine(self, temp_db, monkeypatch):
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        r = client.get("/api/health")
        assert r.status_code == 200
        assert "voice_engine" in r.json()
        assert r.json()["voice_engine"] in ("gemini-live", "vosk-local")
