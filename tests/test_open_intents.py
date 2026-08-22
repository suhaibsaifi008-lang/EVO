import pytest

import core.pc as pc
from core import db
from core.brain import Brain


@pytest.fixture()
def brain():
    return Brain()


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def _no_browsers(monkeypatch):
    monkeypatch.setattr(pc, "_resolve_browser", lambda name: None)


class TestOpenTarget:
    def test_site_fallback_when_app_missing(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        assert pc.open_target("copilot") == "https://copilot.microsoft.com"
        assert opened == ["https://copilot.microsoft.com"]

    def test_youtube_is_a_site(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        pc.open_target("youtube")
        assert opened == ["https://www.youtube.com"]

    def test_filler_words_stripped(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        pc.open_target("the youtube app")
        assert opened == ["https://www.youtube.com"]

    def test_unknown_app_still_raises(self, monkeypatch):
        _no_browsers(monkeypatch)
        with pytest.raises(FileNotFoundError):
            pc.open_target("zzz_not_real_zzz")

    def test_url_passthrough(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        pc.open_target("example.com")
        assert opened == ["https://example.com"]

    def test_open_in_browser_search_phrase(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        url = pc.open_in_browser("best scholarships for abroad studies")
        assert "bing.com/search?q=" in url
        assert opened

    def test_open_in_browser_with_browser(self, monkeypatch):
        launched = []
        monkeypatch.setattr(
            pc, "_resolve_browser", lambda name: "C:/Apps/brave.exe" if "brave" in name else None
        )
        monkeypatch.setattr(pc.subprocess, "Popen", lambda cmd, **k: launched.append(cmd) or object())
        result = pc.open_in_browser("youtube", "brave")
        assert "Brave" in result
        assert launched and launched[0][0] == "C:/Apps/brave.exe"
        assert "youtube.com" in launched[0][2]


class TestOpenIntents:
    def test_open_new_tab_and_search(self, brain, monkeypatch):
        calls = []
        monkeypatch.setattr(pc, "open_in_browser", lambda q, b="": calls.append((q, b)) or q)
        result = brain.respond("open a new tab in brave and search for youtube")
        assert calls and calls[0][0] == "youtube"

    def test_open_x_in_brave(self, brain, temp_db, monkeypatch):
        calls = []
        monkeypatch.setattr(pc, "open_in_browser", lambda q, b="": calls.append((q, b)) or f"{q}|{b}")
        result = brain.respond("open youtube in brave browser")
        assert calls and calls[0][0] == "youtube" and calls[0][1].startswith("brave")
        assert "brave" in result["reply"].lower()

    def test_open_copilot_replies_web(self, brain, temp_db, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        result = brain.respond("open copilot")
        assert "copilot" in result["reply"].lower()

    def test_open_calculator_unchanged(self, brain, temp_db, monkeypatch):
        seen = []
        monkeypatch.setattr(pc.os, "startfile", lambda p: seen.append(p))
        result = brain.respond("open calculator")
        assert any("calc" in str(p).lower() for p in seen)
        assert "calculator" in result["reply"].lower()


class TestResearchFallback:
    def test_extractive_summary_when_llm_down(self, brain, temp_db, monkeypatch):
        from core import config as cfg

        fake_results = [
            {"title": "Scholarships guide", "url": "https://x.test/a", "snippet": "Funding options exist."},
            {"title": "More awards", "url": "https://x.test/b", "snippet": "Deadlines vary by country."},
        ]
        import core.webtools as wt
        from core.brain_helpers import fetch_weather  # noqa: F401

        monkeypatch.setattr(wt, "search_web", lambda q, max_results=5: fake_results)
        monkeypatch.setattr(wt, "fetch_page", lambda url, max_chars=100: (_ for _ in ()).throw(RuntimeError("down")))
        monkeypatch.setattr(cfg, "any_brain_available", lambda: False)
        result = brain.respond("research about the best scholarships available for abroad studies")
        reply = result["reply"]
        assert "Research complete" in reply
        assert "could not summarise" not in reply
