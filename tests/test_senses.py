import sys

import pytest

from core import agent_loop, db, perception, tools


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield db


class TestTTS:
    def test_synthesize_creates_cached_file(self, tmp_path, monkeypatch):
        from core import tts

        monkeypatch.setattr(tts, "TTS_DIR", tmp_path / "tts")
        tts.TTS_DIR.mkdir(exist_ok=True)

        class FakeCom:
            def __init__(self, text, voice, rate="+0%", pitch="+0Hz"):
                assert text and voice
                self.target = None

            async def save(self, path):
                with open(path, "wb") as f:
                    f.write(b"\xff\xfb" + b"x" * 1024)

        import edge_tts

        monkeypatch.setattr(edge_tts, "Communicate", FakeCom)
        out1 = tts.synthesize("systems online", "en-GB-RyanNeural")
        out2 = tts.synthesize("systems online", "en-GB-RyanNeural")
        assert out1 == out2 and out1.stat().st_size > 512

    def test_empty_text_rejected(self, tmp_path, monkeypatch):
        from core import tts

        monkeypatch.setattr(tts, "TTS_DIR", tmp_path / "tts2")
        tts.TTS_DIR.mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            tts.synthesize("   ")

    def test_voices_catalog(self):
        voices = tts_list = __import__("core.tts", fromlist=["list_voices"]).list_voices()
        assert "en-GB-RyanNeural" in voices


class TestPerception:
    def test_active_window_returns_text(self):
        value = perception.active_window()
        assert isinstance(value, str) and len(value) > 0

    def test_visible_windows(self):
        rows = perception.visible_windows(limit=5)
        assert isinstance(rows, list)

    def test_network_status(self):
        status = perception.network_status()
        assert "Network:" in status

    def test_screen_capture(self):
        b64 = perception.screen_image_b64(max_width=640)
        assert len(b64) > 500

    def test_describe_screen_requires_llm(self, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "llm_enabled", lambda: False)
        out = perception.describe_screen("what is this")
        assert "offline" in out.lower()

    def test_describe_screen_with_vision(self, temp_db, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        seen = {}
        monkeypatch.setattr(perception, "screen_image_b64", lambda max_width=1280: "FAKEB64")
        monkeypatch.setattr(
            "core.llm.chat_vision",
            lambda prompt, image, temperature=0.4: seen.update(prompt=prompt, image=image) or "A code editor.",
        )
        out = perception.describe_screen("which app is open")
        assert out == "A code editor." and seen["image"] == "FAKEB64"


class TestPerceptionTools:
    def test_new_tools_registered(self):
        names = {t["name"] for t in tools.manifest()}
        assert {"see_screen", "active_window", "list_windows", "network_status"} <= names

    def test_active_window_tool(self, temp_db):
        out = tools.call("active_window", {})
        assert len(out) > 0 and "ERROR" not in out

    def test_see_screen_via_tool(self, temp_db, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        monkeypatch.setattr("core.perception.describe_screen", lambda q="": "Spreadsheet with sales data.")
        out = tools.call("see_screen", {"question": "what data is shown"})
        assert "Spreadsheet" in out


class TestAmbientContext:
    def test_build_system_includes_viewing_line(self, temp_db, monkeypatch):
        monkeypatch.setattr(tools, "call", lambda name, args: "-")
        system = agent_loop.build_system()
        assert "currently viewing" in system
        assert "see_screen" in system

    def test_ambient_disabled_hides_window(self, temp_db, monkeypatch):
        db.set_setting("ambient_perception", "0")

        def boom(name, args):
            raise AssertionError("should not query windows")

        monkeypatch.setattr(tools, "call", boom)
        monkeypatch.setattr("core.perception.active_window", boom)
        import core.agent_loop as al

        original = al.build_system
        try:
            # build_system calls tools.call for schedule/files before ambient; patch selectively
            def safe_call(name, args):
                if name in ("list_reminders", "list_files"):
                    return "-"
                return boom(name, args)

            monkeypatch.setattr(tools, "call", safe_call)
            system = original()
            assert "viewing: unknown" in system
        finally:
            pass
