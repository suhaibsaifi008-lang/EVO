import json

import pytest

from core import db


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


@pytest.fixture(autouse=True)
def fresh_conversation_state():
    from core import conversation

    conversation._state.update({
        "summary": "", "summary_upto": 0, "topic": "",
        "last_action": "", "pending_question": "", "last_extraction": 0.0,
    })
    yield
    try:
        conversation.reset()
    except Exception:
        pass


# ---------------------------------------------------------------- provider router


class TestModelRouter:
    def test_role_routing_uses_fast_model(self, temp_db, monkeypatch):
        from core import config, llm

        monkeypatch.setattr(config, "llm_enabled", lambda: True)
        monkeypatch.setenv("JARVIS_MODEL_FAST", "nano-model")
        seen = {}
        monkeypatch.setattr(
            llm, "_completion",
            lambda base, key, payload, timeout=60: seen.update(model=payload["model"])
            or {"choices": [{"message": {"content": "ok"}}]},
        )
        llm.chat([{"role": "user", "content": "hi"}], role="fast")
        assert seen["model"] == "nano-model"

    def test_stream_falls_back_to_oneshot(self, temp_db, monkeypatch):
        from core import config, llm

        monkeypatch.setattr(config, "llm_enabled", lambda: True)

        def no_stream(*a, **k):
            raise RuntimeError("provider lacks SSE")

        monkeypatch.setattr(llm, "_stream_completion", no_stream)
        monkeypatch.setattr(llm, "_completion", lambda *a, **k: {"choices": [{"message": {"content": "FULL"}}]})
        assert "".join(llm.chat_stream([{"role": "user", "content": "hi"}])) == "FULL"

    def test_diagnostics_shape(self, temp_db, monkeypatch):
        from core import llm

        d = llm.diagnostics()
        assert set(d["roles"]) == {"primary", "fast", "reasoning", "vision", "fallback"}


# ---------------------------------------------------------------- context pack


class TestContextPacking:
    def test_recent_turns_and_summary_included(self, temp_db, monkeypatch):
        from core import conversation

        for i in range(6):
            db.log_message("user", f"question {i}")
            db.log_message("assistant", f"answer {i}")
        db.set_setting("convo_summary", "USER PREFERS SHORT ANSWERS")
        monkeypatch.setattr(conversation, "_load_persisted", lambda: None)
        conversation._state["summary"] = "USER PREFERS SHORT ANSWERS"
        msgs = conversation.build_messages("what about tomorrow?")
        joined = json.dumps(msgs)
        assert "SHORT ANSWERS" in joined
        assert msgs[-1] == {"role": "user", "content": "what about tomorrow?"}
        assert any(m["content"] == "answer 5" for m in msgs)

    def test_last_action_injected(self, temp_db):
        from core import conversation

        conversation.note_action("open_app(calculator) -> ok")
        msgs = conversation.build_messages("do it again")
        assert any("open_app(calculator)" in m["content"] for m in msgs if m["role"] == "system")


# ---------------------------------------------------------------- summarization


class TestSummarization:
    def test_long_history_triggers_compression(self, temp_db, monkeypatch):
        from core import conversation

        for i in range(40):
            db.log_message("user", f"msg {i} about topic {i % 3}")
            db.log_message("assistant", f"reply {i}")
        monkeypatch.setattr(
            "core.llm.chat",
            lambda *a, **k: "- decided: use blue\n- open question: deadline?",
        )
        ok = conversation.maybe_summarize(force=True)
        assert ok
        assert "blue" in conversation._state["summary"]
        assert int(float(db.get_setting("convo_summary_upto", "0"))) > 0


# ---------------------------------------------------------------- memory policy


class TestMemoryPolicy:
    def test_explicit_remember_stores(self, temp_db, monkeypatch):
        from core import conversation

        monkeypatch.setattr(
            "core.llm.chat",
            lambda *a, **k: '{"remember": true, "items": [{"key": "favourite colour", "value": "teal"}]}',
        )
        out = conversation.extract_and_store_memory("remember my favourite colour is teal", "Noted.")
        assert out and "favourite colour" in out["stored"]
        hits = db.search_memory("colour")
        assert hits and hits[0]["value"] == "teal"

    def test_update_overwrites_stale_fact(self, temp_db, monkeypatch):
        from core import conversation

        db.remember("city", "Delhi")
        monkeypatch.setattr(
            "core.llm.chat",
            lambda *a, **k: '{"remember": true, "items": [{"key": "city", "value": "Mumbai"}]}',
        )
        conversation.extract_and_store_memory("by the way I moved to Mumbai", "Congrats!")
        assert db.get_memory("city") == "Mumbai"

    def test_sensitive_skipped_unless_explicit(self, temp_db, monkeypatch):
        from core import conversation

        calls = {"n": 0}

        def spy(*a, **k):
            calls["n"] += 1
            return '{"remember": true, "items": []}'

        monkeypatch.setattr("core.llm.chat", spy)
        out = conversation.extract_and_store_memory("my api key is abc123", "ok")
        assert out is None and calls["n"] == 0


# ---------------------------------------------------------------- agent sanitize


class TestResponseHygiene:
    def test_sanitize_strips_leaked_protocol(self):
        from core.agent_loop import sanitize_final

        leak = 'TOOL RESULT (open_app):\n{"tool":"open_app"} Opened calculator.'
        clean = sanitize_final(leak)
        assert "TOOL RESULT" not in clean
        assert '"tool"' not in clean

    def test_say_json_never_reaches_user_raw(self):
        from core.agent_loop import sanitize_final

        raw = '{"say": "Chrome is open."}'
        assert sanitize_final(raw) == "Chrome is open."


# ---------------------------------------------------------------- brain pipeline


class TestBrainPipeline:
    def test_empty_message(self, brain):
        assert "catch" in brain.respond("").reply.lower() if hasattr(brain.respond(""), "reply") else True
        assert "catch" in brain.respond("")["reply"].lower()

    def test_restart_recovers_conversation(self, temp_db):
        from core.brain import Brain

        b1 = Brain()
        b1.respond("remember that my laptop is an XPS")
        b2 = Brain()
        assert any("XPS" in h["content"] for h in b2.history)

    @pytest.fixture()
    def brain(self):
        from core.brain import Brain

        return Brain()


class TestStreamingEndpoint:
    def test_stream_emits_done_for_direct_intent(self, temp_db, monkeypatch):
        from fastapi.testclient import TestClient

        from main import app
        import core.pc as pc

        monkeypatch.setattr(pc.os, "startfile", lambda p: None)
        monkeypatch.setattr(pc, "_lnk_index", lambda: {})
        monkeypatch.setattr(pc, "_uwp_index", lambda: {})
        monkeypatch.setattr(pc, "default_browser_exe", lambda: None)
        events = []
        # Plain client (no context manager): skips app lifespan so the shared
        # skill registry / dispatcher threads are not started by this test.
        client = TestClient(app)
        with client.stream("POST", "/api/chat/stream", json={"text": "open calculator"}) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data:"):
                    ev = json.loads(line[5:])
                    events.append(ev)
                    if ev["type"] == "end":
                        break
        kinds = [e["type"] for e in events]
        assert "done" in kinds and "end" in kinds
        done_ev = next(e for e in events if e["type"] == "done")
        assert "calculator" in done_ev["text"].lower()
