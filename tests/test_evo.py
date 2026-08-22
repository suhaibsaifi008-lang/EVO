import json
import sys
import time

import pytest

from core import agent_loop, db, filebrain, gui_control, mail, skills, websmith


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield db


def wait_for(fn, timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.05)
    return None


class TestMail:
    def _configured(self, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "MAIL_ADDRESS", "boss@acme.com")
        monkeypatch.setattr(config, "MAIL_PASSWORD", "app-pass")

    def test_unconfigured_raises(self, temp_db, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "MAIL_ADDRESS", "")
        with pytest.raises(mail.MailNotConfigured):
            mail.send_email("a@b.com", "s", "b", confirm=True)

    def test_draft_flow(self, temp_db, monkeypatch):
        self._configured(monkeypatch)
        out = mail.draft_email("client@x.com", "Hello", "Body text")
        assert "DRAFT EMAIL" in out and "client@x.com" in out and "Body text" in out

    def test_send_requires_confirm(self, temp_db, monkeypatch):
        self._configured(monkeypatch)
        out = mail.send_email("client@x.com", "S", "B", confirm=False)
        assert "NOT been sent" in out

    def test_send_blocked_without_setting(self, temp_db, monkeypatch):
        self._configured(monkeypatch)
        out = mail.send_email("client@x.com", "S", "B", confirm=True)
        assert "DENIED" in out

    def test_tools_surface(self, temp_db):
        names = {t["name"] for t in __import__("core.tools", fromlist=["manifest"]).manifest()}
        assert {"draft_email", "send_email", "read_inbox"} <= names


class TestGuiControl:
    @pytest.fixture()
    def fake_pg(self, monkeypatch):
        class FakePG:
            def __init__(self):
                self.calls = []
                self.FAILSAFE = False
                self.PAUSE = 0

            def size(self):
                return (1920, 1080)

            def moveTo(self, x, y, duration=0):
                self.calls.append(("moveTo", x, y))

            def click(self, x=None, y=None, clicks=1, button="left"):
                self.calls.append(("click", x, y, clicks, button))

            def drag(self, dx, dy, duration=0):
                self.calls.append(("drag", dx, dy))

            def scroll(self, amount):
                self.calls.append(("scroll", amount))

            def press(self, key):
                self.calls.append(("press", key))

            def hotkey(self, *keys):
                self.calls.append(("hotkey", keys))

            def typewrite(self, text, interval=0):
                self.calls.append(("type", text))

        stub = FakePG()
        monkeypatch.setitem(sys.modules, "pyautogui", stub)
        return stub

    def test_denied_by_default(self, temp_db):
        with pytest.raises(gui_control.GUIDisabled):
            gui_control.click(10, 10)
        from core import tools

        out = tools.call("gui_click", {"x": 10, "y": 10})
        assert "DENIED" in out and "Setup" in out

    def test_click_after_enabling(self, temp_db, fake_pg):
        db.set_setting("gui_allowed", "1")
        out = gui_control.click(960, 540)
        assert "Clicked" in out
        assert any(c[0] == "click" for c in fake_pg.calls)

    def test_hotkey_mapping(self, temp_db, fake_pg):
        db.set_setting("gui_allowed", "1")
        gui_control.hotkey("ctrl+s")
        assert ("hotkey", ("ctrl", "s")) in fake_pg.calls

    def test_type_ascii(self, temp_db, fake_pg):
        db.set_setting("gui_allowed", "1")
        out = gui_control.type_text("hello world")
        assert "Typed" in out
        assert ("type", "hello world") in fake_pg.calls

    def test_vision_click_element(self, temp_db, fake_pg, monkeypatch):
        db.set_setting("gui_allowed", "1")
        monkeypatch.setattr("core.perception.screen_image_b64", lambda max_width=1280: "IMGDATA")
        monkeypatch.setattr(
            "core.llm.chat_vision",
            lambda prompt, image, temperature=0.4: '{"x": 500, "y": 250}',
        )
        out = gui_control.click_element("the Export button")
        assert "Clicked" in out and "(960, 270)" in out

    def test_focus_window_no_match(self, temp_db, fake_pg):
        db.set_setting("gui_allowed", "1")
        out = gui_control.focus_window("zzz-window-that-does-not-exist-xyz")
        assert "No visible window" in out


class TestFileBrain:
    def _make_docs(self, tmp_path):
        root = tmp_path / "docs"
        root.mkdir()
        (root / "notes.txt").write_text("The quantum flux capacitor needs calibration every Tuesday.", encoding="utf-8")
        (root / "readme.md").write_text("# Guide\n\nSourdough starter requires patience and flour.", encoding="utf-8")
        return root

    def test_index_and_search(self, temp_db, tmp_path):
        root = self._make_docs(tmp_path)
        stats = filebrain.index_folder(str(root))
        assert stats["files_indexed"] == 2 and stats["chunks_added"] >= 2
        hits = filebrain.search("flux capacitor calibration")
        assert hits and "flux" in hits[0]["snippet"].lower()

    def test_unchanged_files_skipped(self, temp_db, tmp_path):
        root = self._make_docs(tmp_path)
        filebrain.index_folder(str(root))
        again = filebrain.index_folder(str(root))
        assert again["files_indexed"] == 0

    def test_status(self, temp_db, tmp_path):
        root = self._make_docs(tmp_path)
        filebrain.index_folder(str(root))
        s = filebrain.status()
        assert s["files"] == 2 and s["chunks"] >= 2

    def test_chunking_overlap(self):
        text = "word " * 2000
        chunks = filebrain.chunk_text(text)
        assert len(chunks) >= 3 and len(chunks[0]) <= 900


class TestWatchers:
    def test_battery_low_fires_once(self, temp_db, monkeypatch):
        from core import watchers

        monkeypatch.setattr("core.pc.system_status", lambda: {"battery_percent": 12, "charging": False})
        w = {"kind": "battery_low", "target": "", "threshold": 20, "last_value": ""}
        fired, detail, stays, val = watchers.evaluate(w)
        assert fired and not stays and "12%" in detail

    def test_disk_high(self, temp_db, monkeypatch):
        from core import watchers

        monkeypatch.setattr(watchers, "disk_usage_percent", lambda drive="C": 96.0)
        w = {"kind": "disk_high", "target": "C", "threshold": 90, "last_value": ""}
        fired, detail, stays, _ = watchers.evaluate(w)
        assert fired and "96%" in detail

    def test_website_change_rearms(self, temp_db, monkeypatch):
        from core import watchers

        pages = iter(["Version A content here", "Version A content here", "Version B totally different"])
        monkeypatch.setattr("core.webtools.fetch_page", lambda url, max_chars=2500: next(pages))
        w = {"kind": "website_change", "target": "https://x.com", "threshold": 0, "last_value": ""}
        _, _, _, marker = watchers.evaluate(w)
        assert marker
        w2 = dict(w, last_value=marker)
        fired2, _, _, marker2 = watchers.evaluate(w2)
        assert not fired2
        w3 = dict(w2, last_value=marker2)
        fired3, detail3, stays3, _ = watchers.evaluate(w3)
        assert fired3 and stays3 and "changed" in detail3.lower()

    def test_crud_and_due_selection(self, temp_db):
        wid = db.add_watcher("battery_low", "", 25, interval_sec=3600)
        rows = db.list_watchers()
        assert rows and rows[0]["id"] == wid
        db.record_watcher(wid, "active", "", time.time())
        assert not db.due_watchers(time.time())
        db.record_watcher(wid, "active", "", time.time() - 7200)
        assert db.due_watchers(time.time())
        assert db.remove_watcher(wid) and not db.list_watchers()

    def test_engine_publishes_alert(self, temp_db, monkeypatch):
        from core import watchers as W

        db.add_watcher("battery_low", "", 50, interval_sec=60)
        monkeypatch.setattr("core.pc.system_status", lambda: {"battery_percent": 30, "charging": False})
        captured = []
        monkeypatch.setattr(W.dispatcher, "publish", lambda e: captured.append(e))
        engine = W.WatcherEngine(poll_seconds=999)
        fired = engine.check_due()
        assert fired == 1 and captured and "30%" in captured[0]["text"]
        row = db.list_watchers()[0]
        assert row["status"] == "triggered"


class TestTelegram:
    def _tl(self):
        from core import telegram_link

        return telegram_link

    def test_pairing_first_message(self, temp_db, monkeypatch):
        tl = self._tl()
        sent = []
        monkeypatch.setattr(tl, "_send", lambda cid, text: sent.append((cid, text)))
        handler_calls = []
        tl._handle_update({"message": {"chat": {"id": 777}, "text": "/start"}}, lambda t: handler_calls.append(t))
        assert db.get_setting("telegram_chat_id") == "777"
        assert "Paired" in sent[0][1]
        assert not handler_calls

    def test_unauthorized_ignored(self, temp_db, monkeypatch):
        tl = self._tl()
        db.set_setting("telegram_chat_id", "111")
        sent = []
        handled = []
        monkeypatch.setattr(tl, "_send", lambda cid, text: sent.append((cid, text)))
        tl._handle_update({"message": {"chat": {"id": 222}, "text": "hello"}}, lambda t: handled.append(t))
        assert not sent and not handled

    def test_allowed_chat_gets_reply(self, temp_db, monkeypatch):
        tl = self._tl()
        db.set_setting("telegram_chat_id", "111")
        sent = []
        monkeypatch.setattr(tl, "_send", lambda cid, text: sent.append((cid, text)))
        tl._handle_update({"message": {"chat": {"id": 111}, "text": "what time is it"}},
                          lambda t: {"reply": f"It is noon about '{t}'."})
        assert sent and "noon" in sent[0][1]


class TestLLMRouting:
    def test_primary_fail_falls_to_ollama(self, temp_db, monkeypatch):
        from core import config, llm

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        monkeypatch.setattr(config, "ollama_ready", lambda: True)
        responses = iter([ConnectionError("primary down"), {"choices": [{"message": {"content": "OLLAMA REPLY"}}]}])
        monkeypatch.setattr(llm, "_completion", lambda *a, **k: next(responses))
        assert llm.chat([{"role": "user", "content": "hi"}]) == "OLLAMA REPLY"

    def test_all_down_raises(self, temp_db, monkeypatch):
        from core import config, llm

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        monkeypatch.setattr(config, "ollama_ready", lambda: True)
        monkeypatch.setattr(llm, "_completion", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
        with pytest.raises(RuntimeError):
            llm.chat([{"role": "user", "content": "hi"}])

    def test_primary_success_short_circuits(self, temp_db, monkeypatch):
        from core import config, llm

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        seen = {}
        monkeypatch.setattr(llm, "_completion", lambda base, key, payload, timeout=60: seen.update(base=base) or {"choices": [{"message": {"content": "PRIMARY"}}]})
        assert llm.chat([{"role": "user", "content": "hi"}]) == "PRIMARY"
        assert seen["base"] == config.OPENAI_BASE_URL


class TestMissionResume:
    def test_budget_pause_then_resume(self, temp_db, monkeypatch):
        from core.projects import manager

        responses = iter([
            '{"action": {"tool": "remember_fact", "args": {"key": "step1", "value": "done"}}}',
            '{"finish": "All finished properly."}',
        ])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        pid = manager.start("tiny mission", max_steps=1)
        row = wait_for(lambda: (db.get_project(pid) or {}).get("status") in ("paused", "failed") and db.get_project(pid))
        assert row["status"] == "paused"
        resumed = manager.resume(pid)
        assert "resumed" in resumed.lower()
        final = wait_for(lambda: (db.get_project(pid) or {}).get("status") == "done" and db.get_project(pid))
        assert "finished properly" in final["result"]

    def test_resume_rejects_running_or_empty(self, temp_db):
        from core.projects import manager

        pid = db.create_project("ghost")
        assert "no saved progress" in manager.resume(pid).lower()
        db.save_project_state(pid, json.dumps([{"role": "user", "content": "x"}]))
        db.set_project_running(pid)
        assert "already running" in manager.resume(pid).lower()


class TestAuditLedger:
    def test_tool_call_logged(self, temp_db):
        from core import tools

        tools.call("remember_fact", {"key": "colour", "value": "teal"})
        rows = db.recent_audit(5)
        assert rows and rows[0]["tool"] == "remember_fact" and rows[0]["outcome"] == "ok"

    def test_error_outcome_logged(self, temp_db):
        from core import tools

        tools.call("save_code", {})
        rows = db.recent_audit(3)
        assert any(r["outcome"] != "ok" for r in rows)


class TestWebsmith:
    def _script(self, monkeypatch, pages):
        def page_html(title):
            return (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1'>"
                f"<title>{title}</title><link rel='stylesheet' href='style.css'></head>"
                "<body><nav aria-label='main'></nav><main>Content</main></body></html>"
            )

        css = ":root{--brand:#2563eb;} " + "body{margin:0;font-family:system-ui;} " * 8
        outputs = ["ACME BAKERY", css] + [page_html(p) for p in pages]
        it = iter(outputs)
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(it))

    def test_build_site_end_to_end(self, temp_db, tmp_path, monkeypatch):
        pages = ["index", "about", "contact"]
        monkeypatch.setattr(websmith, "SITES_DIR", tmp_path / "sites")
        websmith.SITES_DIR.mkdir(exist_ok=True)
        self._script(monkeypatch, pages)
        result = websmith.build_site("Website for ACME Bakery with about and contact pages", name="acme", pages=pages)
        folder = tmp_path / "sites" / "acme"
        assert result["brand"] == "ACME BAKERY"
        for fname in ("index.html", "about.html", "contact.html", "style.css", "favicon.svg", "README-deploy.txt"):
            assert (folder / fname).exists(), fname
        html = (folder / "index.html").read_text(encoding="utf-8")
        assert "viewport" in html and "style.css" in html

    def test_page_derivation_from_brief(self):
        pages = websmith._derive_pages("A pricing site with contact form and blog", None)
        assert "index" in pages and "pricing" in pages and "contact" in pages and "blog" in pages
