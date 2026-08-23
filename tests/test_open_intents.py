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
    monkeypatch.setattr(pc, "default_browser_exe", lambda: None)


def _no_discovery(monkeypatch):
    """Keep app resolution hermetic: no real Start Menu scan / Store app lookup."""
    monkeypatch.setattr(pc, "_lnk_index", lambda: {})
    monkeypatch.setattr(pc, "_uwp_index", lambda: {})


@pytest.fixture(autouse=True)
def hermetic_discovery(monkeypatch):
    _no_discovery(monkeypatch)
    _no_browsers(monkeypatch)


class TestOpenTarget:
    def test_site_fallback_when_app_missing(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        assert pc.open_target("copilot") == "https://copilot.microsoft.com"
        assert opened == ["https://copilot.microsoft.com"]

    def test_store_app_launched_when_installed(self, monkeypatch):
        seen = []
        monkeypatch.setattr(pc.os, "startfile", lambda p: seen.append(p))
        monkeypatch.setattr(
            pc, "_uwp_index",
            lambda: {"microsoft copilot": "Microsoft.Copilot_8wekyb3d8bbwe!App"},
        )
        assert pc.open_target("copilot") == "copilot"
        assert any("Microsoft.Copilot" in str(p) for p in seen)

    def test_start_menu_shortcut_launch(self, monkeypatch):
        seen = []
        monkeypatch.setattr(pc.os, "startfile", lambda p: seen.append(p))
        monkeypatch.setattr(pc, "_lnk_index", lambda: {"valorant": r"C:\Riot Games\Valorant.lnk"})
        assert pc.open_target("valorant") == "valorant"
        assert seen == [r"C:\Riot Games\Valorant.lnk"]

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

    def test_unknown_app_still_raises(self):
        with pytest.raises(FileNotFoundError):
            pc.open_target("zzz_not_real_zzz")

    def test_url_passthrough(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        pc.open_target("example.com")
        assert opened == ["https://example.com"]

    def test_open_in_browser_search_phrase_uses_default_engine_fallback(self, monkeypatch):
        opened = []
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        url = pc.open_in_browser("best scholarships for abroad studies")
        # Never Bing: fall back to DuckDuckGo when no browser exe can be resolved.
        assert "bing.com" not in url
        assert "duckduckgo.com/?q=" in url
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
        assert "youtube.com" in launched[0][1]

    def test_browser_reuses_window_no_new_window_flag(self, monkeypatch):
        launched = []
        monkeypatch.setattr(
            pc, "_resolve_browser", lambda name: "C:/Apps/brave.exe"
        )
        monkeypatch.setattr(pc.subprocess, "Popen", lambda cmd, **k: launched.append(cmd) or object())
        pc.open_in_browser("youtube", "brave")
        assert "--new-window" not in launched[0]  # tabs reuse the running window
        assert "youtube.com" in launched[0][1]


class TestOpenIntentParsing:
    def test_open_youtube_in_the_brave_browser(self, brain, temp_db, monkeypatch):
        """'in THE brave browser' - articles must not break browser extraction."""
        calls = []
        monkeypatch.setattr(pc, "open_in_browser", lambda q, b="": calls.append((q, b)) or q)
        brain.respond("open youtube in the brave browser")
        assert calls and calls[0][0] == "youtube" and calls[0][1].startswith("brave")

    def test_unknown_target_opens_web_without_asking(self, brain, temp_db, monkeypatch):
        opened = []
        monkeypatch.setattr(pc, "_lnk_index", lambda: {})
        monkeypatch.setattr(pc, "_uwp_index", lambda: {})
        monkeypatch.setattr(pc, "default_browser_exe", lambda: None)
        monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u))
        reply = brain.respond("open zzznotrealapp")["reply"].lower()
        assert opened, "should fall back to a web search automatically"
        assert "isn't an installed app" in reply or "opened it on the web" in reply

    def test_gui_toggle_by_voice(self, brain, temp_db):
        from core import db as d

        d.set_setting("gui_allowed", "0")
        r1 = brain.respond("enable mouse control")["reply"].lower()
        assert d.get_setting("gui_allowed") == "1"
        assert "enabled" in r1
        brain.respond("turn off mouse control")
        assert d.get_setting("gui_allowed") == "0"


class TestVocabCorrection:
    def test_brand_names_fixed(self):
        from core.vocab import correct_terms

        assert "youtube" in correct_terms("open utube").lower()
        assert "calculator" in correct_terms("open calculation").lower()

    def test_clean_text_untouched(self):
        from core.vocab import correct_terms

        assert correct_terms("what is the time") == "what is the time"

    def test_wikipedia_is_a_known_site_now(self):
        import core.pc as pc

        assert "wikipedia" in pc.SITES

    def test_search_phrase_passed_raw_to_browser_engine(self, monkeypatch):
        launched = []
        monkeypatch.setattr(
            pc, "_resolve_browser", lambda name: "C:/Apps/brave.exe" if "brave" in name else None
        )
        monkeypatch.setattr(pc.subprocess, "Popen", lambda cmd, **k: launched.append(cmd) or object())
        pc.open_in_browser("best budget mechanical keyboards", "brave")
        # Plain text goes to the browser untouched -> its OWN default search engine.
        assert launched[0][1] == "best budget mechanical keyboards"
        assert "bing.com" not in launched[0][1]


class TestOpenIntents:
    def test_open_works_even_when_agent_fails(self, brain, temp_db, monkeypatch):
        """Deterministic-first: PC commands must not depend on the LLM."""
        from core import config as cfg

        monkeypatch.setattr(cfg, "agent_enabled", lambda: True)

        import core.agent_loop as agent_loop

        def boom(*a, **k):
            raise RuntimeError("llm down")

        monkeypatch.setattr(agent_loop, "run", boom)
        seen = []
        monkeypatch.setattr(pc.os, "startfile", lambda p: seen.append(p))
        result = brain.respond("open calculator")
        assert any("calc" in str(p).lower() for p in seen)
        assert "calculator" in result["reply"].lower()

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
