import json
import time

import pytest

from core import agent_loop, db, skills, tools

ECHO_SKILL = """
import json, sys
args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
print("echo:" + str(args.get("text", "")))
"""

WEATHERISH_SKILL = """
import sys, json
args = json.loads(sys.argv[1])
print(f"forecast for {args['city']}: sunny, 24C")
"""


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    yield db


class TestSkillForge:
    def test_save_and_hot_register(self, temp_db, monkeypatch):
        from core import tools as t

        before = {x["name"] for x in t.manifest()}
        out = tools.call("save_skill", {
            "name": "Echo Test", "description": "Echoes text back",
            "code": ECHO_SKILL, "args_schema": {"text": "text to echo"},
        })
        assert "saved" in out.lower()
        after = {x["name"] for x in t.manifest()}
        assert "skill_echo_test" in after and "skill_echo_test" not in before

        result = tools.call("skill_echo_test", {"text": "hello world"})
        assert result == "echo:hello world"

    def test_example_args_test_run(self, temp_db, tmp_path, monkeypatch):
        from core import coding

        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "sk")
        skills.SKILLS_DIR.mkdir(exist_ok=True)
        out = tools.call("save_skill", {
            "name": "weatherish", "description": "fake forecast",
            "code": WEATHERISH_SKILL, "example_args": {"city": "Delhi"},
        })
        assert "Test run OK" in out and "Delhi" in out

    def test_bad_syntax_rejected(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "sk2")
        skills.SKILLS_DIR.mkdir(exist_ok=True)
        out = tools.call("save_skill", {"name": "broken", "description": "d", "code": "def oops(:\n"})
        assert "syntax error" in out.lower()
        assert not list(tmp_path.joinpath("sk2").glob("*.py"))

    def test_failing_example_args_reported(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "sk3")
        skills.SKILLS_DIR.mkdir(exist_ok=True)
        bad = "import sys\nraise SystemExit(3)"
        out = tools.call("save_skill", {"name": "fails", "description": "d", "code": bad, "example_args": {}})
        assert "TEST FAILED" in out

    def test_list_and_delete(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "sk4")
        skills.SKILLS_DIR.mkdir(exist_ok=True)
        tools.call("save_skill", {"name": "tempone", "description": "t", "code": ECHO_SKILL})
        listing = tools.call("list_skills", {})
        assert "tempone" in listing
        assert "deleted" in tools.call("delete_skill", {"name": "tempone"}).lower()
        assert {x["name"] for x in tools.manifest()} & {"skill_tempone"} == set()

    def test_survives_restart_via_register_all(self, temp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "sk5")
        skills.SKILLS_DIR.mkdir(exist_ok=True)
        tools.call("save_skill", {"name": "phoenix", "description": "rises again", "code": ECHO_SKILL})
        from core import tools as t

        t._REGISTRY.pop("skill_phoenix", None)
        count = skills.register_all()
        assert count >= 1
        assert "rises again" in tools.call("list_skills", {})

    def test_name_sanitised(self, temp_db):
        with pytest.raises(Exception):
            skills._safe_name("")
        assert skills._safe_name("My Cool Skill!") == "my_cool_skill"


class TestPersistentMemory:
    def test_message_roundtrip_order(self, temp_db):
        db.log_message("user", "first")
        db.log_message("assistant", "reply one")
        rows = db.recent_messages(10)
        assert rows[0]["content"] == "first" and rows[-1]["role"] == "assistant"

    def test_brain_preloads_history(self, temp_db):
        from core.brain import Brain

        db.log_message("user", "remember the word pineapple")
        brain = Brain()
        assert any("pineapple" in h["content"] for h in brain.history)

    def test_respond_logs_exchange(self, temp_db):
        from core.brain import Brain

        brain = Brain()
        brain.pending.clear()
        brain.respond("what time is it")
        rows = db.recent_messages(5)
        roles = [r["role"] for r in rows]
        assert "user" in roles and "assistant" in roles

    def test_clear_chat_wipes_stored(self, temp_db):
        from core.brain import Brain

        brain = Brain()
        db.log_message("user", "secret stuff")
        brain.respond("clear the conversation")
        assert not [m for m in db.recent_messages(50) if "secret" in m["content"]]

    def test_relevant_knowledge_ranking(self, temp_db):
        db.learn("black holes", "A black hole bends spacetime; light cannot escape.")
        db.learn("sourdough", "Sourdough starter needs flour, water and days of patience.")
        hits = db.relevant_knowledge("tell me about black holes again")
        assert hits and hits[0]["topic"] == "black holes"
        assert not db.relevant_knowledge("zzzqqq")

    def test_agent_context_injects_relevant_knowledge(self, temp_db, monkeypatch):
        db.learn("quantum computing", "qubits exist.")
        seen = {}
        monkeypatch.setattr(tools, "call", lambda n, a: "-")
        original = agent_loop.build_system
        system = original("explain quantum computing please")
        seen["system"] = system
        assert "Relevant studies" in system or "quantum" in system


class TestDeepThought:
    def _script_chat(self, monkeypatch, outputs):
        it = iter(outputs)
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(it))

    def test_ensemble_merges_three_roles(self, temp_db, monkeypatch):
        self._script_chat(monkeypatch, ["ANALYST VIEW", "SKEPTIC VIEW", "ENGINEER VIEW", "MERGED WISDOM"])
        out = tools.call("deep_thought", {"question": "should we expand to mars?"})
        assert out == "MERGED WISDOM"

    def test_role_failure_tolerated(self, temp_db, monkeypatch):
        calls = iter(["A", None])

        def fake(messages, temperature=0.6):
            r = next(calls)
            if r is None:
                raise RuntimeError("provider down")
            return f"OK-{len(str(messages))}"

        monkeypatch.setattr("core.llm.chat", fake)
        out = tools.call("deep_thought", {"question": "q"})
        assert isinstance(out, str) and out


class TestDeepModeCritique:
    def test_critique_improves_answer(self, temp_db, monkeypatch):
        responses = iter([
            '{"say": "The earth is flat and round-ish."}',
            '{"say": "The earth is an oblate spheroid."}',
        ])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        db.set_setting("deep_mode", "1")
        reply = agent_loop.run("what shape is the earth", [])
        assert "oblate spheroid" in reply

    def test_no_critique_when_off(self, temp_db, monkeypatch):
        responses = iter(['{"say": "quick answer."}'])
        monkeypatch.setattr("core.llm.chat", lambda *a, **k: next(responses))
        db.set_setting("deep_mode", "0")
        assert agent_loop.run("hi", []) == "quick answer."

    def test_critique_failure_keeps_original(self, temp_db, monkeypatch):
        state = {"n": 0}

        def flaky(*a, **k):
            state["n"] += 1
            if state["n"] == 1:
                return '{"say": "original good answer that is long enough to pass."}'
            raise RuntimeError("critic down")

        monkeypatch.setattr("core.llm.chat", flaky)
        db.set_setting("deep_mode", "1")
        reply = agent_loop.run("question needing length", [])
        assert reply.startswith("original good answer")


class TestSettingsDeepMode:
    def test_setting_roundtrip(self, temp_db):
        db.set_setting("deep_mode", "1")
        assert db.get_setting("deep_mode") == "1"
        db.set_setting("deep_mode", "0")
        assert db.get_setting("deep_mode") == "0"
