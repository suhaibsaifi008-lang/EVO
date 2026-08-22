import time

import pytest

from core import agent_loop, db, projects, tools


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield db


def wait_for(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.05)
    return None


class TestToolRegistry:
    def test_manifest_covers_core_skills(self):
        names = {t["name"] for t in tools.manifest()}
        expected = {
            "open_app", "web_search", "read_page", "screenshot", "system_status",
            "remember_fact", "recall_memory", "learn_topic", "recall_knowledge",
            "add_reminder", "list_reminders", "cancel_reminder",
            "save_code", "run_code", "read_file", "list_files",
            "hire_workers", "create_project", "daily_briefing",
            "schedule_daily_briefing", "get_weather", "current_datetime",
        }
        assert expected <= names

    def test_unknown_tool(self):
        assert "unknown tool" in tools.call("definitely_not_a_tool", {})

    def test_missing_required_arg(self, temp_db):
        out = tools.call("remember_fact", {"key": "colour"})
        assert "missing required argument 'value'" in out

    def test_remember_and_recall(self, temp_db):
        assert "Stored" in tools.call("remember_fact", {"key": "colour", "value": "red"})
        hits = tools.call("recall_memory", {"query": "colour"})
        assert "red" in hits

    def test_save_and_run_gating(self, temp_db, tmp_path, monkeypatch):
        from core import coding

        monkeypatch.setattr(coding, "WORKSPACE", tmp_path / "ws")
        coding.WORKSPACE.mkdir(exist_ok=True)
        saved = tools.call("save_code", {"filename": "demo.py", "code": "print('sandbox ok')"})
        assert "Saved demo.py" in saved

        denied = tools.call("run_code", {"filename": "demo.py"})
        assert "DENIED" in denied

        db.set_setting("auto_approve_code", "1")
        ran = tools.call("run_code", {"filename": "demo.py"})
        assert "Ran cleanly" in ran and "sandbox ok" in ran

    def test_add_reminder_natural_time(self, temp_db):
        out = tools.call("add_reminder", {"kind": "reminder", "message": "stretch", "due_description": "in 15 minutes"})
        assert "#1" in out and "stretch" in out
        items = db.list_reminders()
        assert len(items) == 1 and items[0]["kind"] == "reminder"

    def test_cancel_scope_all(self, temp_db):
        tools.call("add_reminder", {"message": "a", "due_description": "in 5 minutes"})
        tools.call("add_reminder", {"message": "b", "due_description": "in 6 minutes"})
        out = tools.call("cancel_reminder", {"scope": "all"})
        assert "Cancelled 2" in out and not db.list_reminders()

    def test_parse_brief_time(self):
        assert tools.parse_brief_time("8 am") == (8, 0)
        assert tools.parse_brief_time("9 pm") == (21, 0)
        assert tools.parse_brief_time("17:30") == (17, 30)
        assert tools.parse_brief_time("nonsense") is None

    def test_schedule_daily_briefing(self, temp_db):
        out = tools.call("schedule_daily_briefing", {"time_description": "7 am", "enable": True})
        assert "enabled" in out and "07:00" in out
        assert db.get_setting("briefing_enabled") == "1"
        assert db.get_setting("briefing_time") == "07:00"

    def test_web_search_tool(self, temp_db, monkeypatch):
        fake = [{"title": "Result One", "url": "https://x.com/1", "snippet": "snip"}]
        monkeypatch.setattr(tools, "search_web", lambda q, max_results=5: fake)
        out = tools.call("web_search", {"query": "anything"})
        assert "Result One" in out and "https://x.com/1" in out


class TestJsonParsing:
    def test_plain(self):
        assert agent_loop.parse_json_object('{"say":"hi"}') == {"say": "hi"}

    def test_fenced(self):
        raw = '```json\n{"tool": "system_status", "args": {}}\n```'
        data = agent_loop.parse_json_object(raw)
        assert data["tool"] == "system_status"

    def test_embedded_in_prose(self):
        raw = 'Sure! {"say": "done"} hope that helps'
        assert agent_loop.parse_json_object(raw) == {"say": "done"}

    def test_invalid_returns_none(self):
        assert agent_loop.parse_json_object("no json here at all") is None


class TestAgentLoop:
    def _scripted(self, monkeypatch, responses, observations=None):
        calls = []
        it = iter(responses)
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(it))

        def fake_call(name, args):
            calls.append((name, args))
            return (observations or {}).get(name, "OBSERVATION-DATA")

        monkeypatch.setattr(tools, "call", fake_call)
        return calls

    def test_tool_then_answer(self, temp_db, monkeypatch):
        responses = [
            '{"tool": "system_status", "args": {}}',
            '{"say": "Battery is at 50 percent."}',
        ]
        calls = self._scripted(monkeypatch, responses)
        reply = agent_loop.run("how is my battery", [])
        assert reply == "Battery is at 50 percent."
        assert calls.count(("system_status", {})) == 1

    def test_unknown_tool_recovers(self, temp_db, monkeypatch):
        responses = [
            '{"tool": "made_up_tool", "args": {}}',
            '{"say": "I could not do that, sorry."}',
        ]
        self._scripted(monkeypatch, responses, {"made_up_tool": "ERROR: unknown tool 'made_up_tool'"})
        reply = agent_loop.run("do the impossible", [])
        assert "could not" in reply

    def test_plain_reply_passthrough(self, temp_db, monkeypatch):
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: "Just chatting today.")
        assert agent_loop.run("hello there", []) == "Just chatting today."

    def test_step_limit(self, temp_db, monkeypatch):
        responses = [f'{{"tool": "web_search", "args": {{"query": "q{i}"}}}}' for i in range(10)]
        self._scripted(monkeypatch, responses)
        reply = agent_loop.run("endless task", [], max_steps=3)
        assert "more steps than" in reply.lower()


class TestProjects:
    def test_immediate_finish(self, temp_db, monkeypatch):
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: '{"finish": "Report written to workspace."}')
        pid = projects.manager.start("write a haiku about rain")
        row = wait_for(lambda: (db.get_project(pid) or {}).get("status") == "done" and db.get_project(pid))
        assert row["result"] == "Report written to workspace."

    def test_step_flow_with_tools(self, temp_db, monkeypatch):
        responses = iter([
            '{"action": {"tool": "web_search", "args": {"query": "ev batteries"}}}',
            '{"finish": "Research complete."}',
        ])
        used = []
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        monkeypatch.setattr(tools, "call", lambda name, args: used.append(name) or "SEARCH RESULTS")

        pid = projects.manager.start("research ev batteries")
        wait_for(lambda: (db.get_project(pid) or {}).get("status") == "done")
        assert used == ["web_search"]
        row = db.get_project(pid)
        log_text = str(row["log"])
        assert "web_search" in log_text and "Accepted goal" in log_text

    def test_disallowed_tool_denied(self, temp_db, monkeypatch):
        responses = iter([
            '{"action": {"tool": "lock_pc", "args": {}}}',
            '{"finish": "Cannot lock from projects."}',
        ])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        pid = projects.manager.start("try something forbidden")
        wait_for(lambda: (db.get_project(pid) or {}).get("status") == "done")
        assert "not allowed" in str(db.get_project(pid)["log"])

    def test_stop_before_start_marks_stopped(self, temp_db):
        pid = db.create_project("ghost goal")
        assert projects.manager.stop(pid) is True
        assert db.get_project(pid)["status"] == "stopped"
        assert projects.manager.stop(pid) is False

    def test_llm_failure_marks_failed(self, temp_db, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("router offline")

        monkeypatch.setattr("core.llm.chat", boom)
        pid = projects.manager.start("impossible project")
        row = wait_for(lambda: (db.get_project(pid) or {}).get("status") == "failed" and db.get_project(pid))
        assert "router offline" in row["result"]


class TestAgentBrainIntegration:
    @pytest.fixture()
    def brain(self, temp_db, monkeypatch):
        from core import config
        from core.brain import Brain

        monkeypatch.setattr(config, "AGENT_MODE", True)
        return Brain()

    def test_agent_handles_unmatched_request(self, brain, monkeypatch):
        responses = iter([
            '{"say": "I would love to discuss warp drives, sir."}',
        ])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        r = brain.respond("tell me about warp drives")
        assert "warp drives" in r["reply"]

    def test_confirmation_still_precedes_agent(self, brain, monkeypatch):
        fired = []
        monkeypatch.setattr("core.pc.power", lambda a: fired.append(a))
        brain.respond("shut down the computer")
        r = brain.respond("confirm")
        assert fired == ["shutdown"]
        assert "Shutting down" in r["reply"]

    def test_fastpath_time_beats_agent(self, brain, monkeypatch):
        def explode(*a, **k):
            raise AssertionError("agent loop must not be called")

        monkeypatch.setattr("core.llm.chat", explode)
        assert ":" in brain.respond("what time is it")["reply"]

    def test_history_shared_with_regex_fallback(self, brain, monkeypatch):
        brain.history.extend([{"role": "user", "content": "earlier"}])
        monkeypatch.setattr(
            "core.config.llm_enabled", lambda: False, raising=True
        )
        r = brain.respond("zzz unmatched gibberish xyzzy")
        assert "reply" in r
