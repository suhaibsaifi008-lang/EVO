import sys

import pytest

from core import coding, db, webtools
from core.brain import Brain


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(coding, "WORKSPACE", tmp_path / "ws")
    coding.WORKSPACE.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(coding, "safe_path", None) if False else None
    import core.coding as c

    c.WORKSPACE = tmp_path / "ws"
    c.WORKSPACE.mkdir(exist_ok=True)
    db.init_db()
    yield db


class TestWebTools:
    def test_html_to_text(self):
        page = "<html><head><style>x{}</style></head><body><h1>Hello</h1><p>World &amp; more</p><script>bad()</script></body></html>"
        text = webtools.html_to_text(page)
        assert "Hello" in text and "World & more" in text
        assert "bad()" not in text and "<p>" not in text

    def test_extract_links(self):
        html = '<a href="https://a.com">Site A</a><a href="https://b.com/x">B page</a>'
        links = webtools.extract_links(html)
        assert ("Site A", "https://a.com") in links

    def test_search_failure_raises(self, monkeypatch):
        def boom(url):
            raise OSError("offline")

        monkeypatch.setattr(webtools, "_get", boom)
        with pytest.raises(RuntimeError):
            webtools.search_web("anything")


class TestCoding:
    def test_escape_rejected(self, temp_db):
        with pytest.raises(PermissionError):
            coding.safe_path("../../etc/passwd")
        with pytest.raises(ValueError):
            coding.safe_path("")

    def test_write_read_list(self, temp_db):
        coding.write_file("hello.py", "print('hi')")
        assert "print('hi')" == coding.read_file("hello.py")
        files = coding.list_files()
        assert files[0]["name"] == "hello.py"

    def test_run_success(self, temp_db):
        result = coding.run_python(code="print(2+3)")
        assert result["exit"] == 0 and "5" in result["stdout"]

    def test_run_failure_captures_stderr(self, temp_db):
        result = coding.run_python(code="raise ValueError('boom')")
        assert result["exit"] != 0 and "boom" in result["stderr"]


class TestSwarm:
    def test_hire_and_merge(self, temp_db, monkeypatch):
        import core.swarm as swarm

        calls = []

        def fake_chat(messages, temperature=0.4):
            calls.append(messages)
            if str(messages[0]["content"]).startswith("You are worker"):
                return f"WORK{len(calls)} OUTPUT"
            return "MERGED FINAL"

        monkeypatch.setattr(swarm, "chat", fake_chat)
        out = swarm.hire_workers("write a haiku", n=5)
        assert len(out["workers"]) == 5
        assert out["final"] == "MERGED FINAL"
        assert sum(1 for m in calls if str(m[0]["content"]).startswith("You are worker")) == 5


class TestAgentBrain:
    @pytest.fixture()
    def brain(self, temp_db):
        b = Brain()
        b.pending.clear()
        return b

    def _force_llm_on(self, monkeypatch):
        from core import config

        monkeypatch.setattr(config, "llm_enabled", lambda: True)

    def test_codegen_asks_permission_then_runs(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)

        def fake_chat(messages, temperature=0.4):
            return '```python\nprint("Hello from JARVIS")\n```'

        monkeypatch.setattr("core.llm.chat", fake_chat)
        r1 = brain.respond("write a python script that prints hello world")
        assert "confirm" in r1["reply"].lower()
        assert any(p["kind"] == "run_code" for p in brain.pending.values())

        r2 = brain.respond("confirm")
        assert "ran cleanly" in r2["reply"].lower()
        assert "Hello from JARVIS" in r2["reply"]

    def test_debug_loop(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)
        responses = iter([
            '```python\nraise ValueError("broken")\n```',
            '```python\nprint("fixed output")\n```',
            '```python\nprint("fixed output")\n```',
        ])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))

        brain.respond("create a program that demonstrates an error")
        brain.respond("confirm")
        r = brain.respond("debug")
        assert "ran cleanly" in r["reply"].lower()
        assert "fixed output" in r["reply"]

    def test_workspace_listing(self, brain, temp_db):
        coding.write_file("demo.py", "x=1")
        assert "demo.py" in brain.respond("what's in my workspace")["reply"]

    def test_learn_from_url(self, brain, temp_db, monkeypatch):
        monkeypatch.setattr(webtools, "fetch_page", lambda u, max_chars=4000: "AI Times\nartificial intelligence is transforming everything")
        r = brain.respond("learn from https://aitimes.com/intro")["reply"]
        assert "Studied and stored" in r
        hits = temp_db.recall_knowledge("aitimes")
        assert hits and "artificial intelligence" in hits[0]["content"]

    def test_research_topic(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)
        monkeypatch.setattr(webtools, "search_web", lambda q, max_results=5: [
            {"title": "R1", "url": "https://x.com/1"}, {"title": "R2", "url": "https://x.com/2"}])
        monkeypatch.setattr(webtools, "fetch_page", lambda u, max_chars=1400: "Page Title\nquantum computing facts here")
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: "Quantum findings summary.")
        r = brain.respond("research quantum computing")["reply"]
        assert "Research complete" in r
        assert temp_db.recall_knowledge("quantum")

    def test_read_url(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)
        monkeypatch.setattr(webtools, "fetch_page", lambda u, max_chars=5000: "Docs Page\nAll about widgets")
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: "Widgets explained briefly.")
        r = brain.respond("read https://docs.example.com/widgets")["reply"]
        assert "Docs Page" in r and "Widgets explained" in r

    def test_knowledge_miss_suggests_learning(self, brain, temp_db):
        r = brain.respond("what do you know about underwater basket weaving")["reply"]
        assert "Nothing" in r or "nothing" in r.lower()

    def test_hire_workers_intent(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)
        captured = {}

        def fake_hire(task, n=3):
            captured.update(task=task, n=n)
            return {"final": "TEAM RESULT", "workers": ["a", "b", "c"]}

        monkeypatch.setattr("core.swarm.hire_workers", fake_hire)
        r = brain.respond("hire 3 AIs to design a logo concept")["reply"]
        assert captured["n"] == 3 and "logo" in captured["task"]
        assert "TEAM RESULT" in r

    def test_delegate_default_three(self, brain, temp_db, monkeypatch):
        self._force_llm_on(monkeypatch)
        captured = {}
        monkeypatch.setattr("core.swarm.hire_workers", lambda t, n=3: captured.update(n=n) or {"final": "ok", "workers": []})
        brain.respond("delegate task: summarise my notes")
        assert captured["n"] == 3
