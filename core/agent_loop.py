import json
import re
import time
from datetime import datetime
from typing import Callable

from . import config, db, tools


MAX_STEPS = 6

# Personality: adaptive, not scripted. Kept short on purpose - the
# architecture (context packing, tools, verification) does the heavy lifting.
SYSTEM_TEMPLATE = (
    "You are {name}, a genuinely intelligent assistant running locally on the user's Windows PC. "
    "Address the user as '{address}' when it fits naturally; otherwise just talk normally.\n"
    "Style:\n"
    "- Match the user's tone: serious stays serious, casual stays casual, excited gets energy.\n"
    "- Concise by default; go deep only when depth was asked for or clearly needed.\n"
    "- Confident with known facts, honest about uncertainty. Never fake success or knowledge.\n"
    "- Never recite menus of options or tool names; act, then report naturally.\n"
    "- No repetitive 'sir', no forced jokes, no 'As an AI' disclaimers.\n"
    "Tools:\n"
    "- To perform an action or fetch live data, reply ONLY: {{\"tool\": \"name\", \"args\": {{...}}}}\n"
    "- You will get the result, then continue. After tools finish, give ONE final spoken answer that\n"
    "  interprets the results in plain words - never dump raw output, never mention internal steps.\n"
    "- For plain conversation, no tool is needed: reply ONLY {{\"say\": \"<your reply>\"}}\n"
    "- Follow-ups like 'that one', 'do it', 'why?', 'what about tomorrow?' refer to recent context -\n"
    "  resolve them yourself instead of asking the user to repeat.\n"
    "- Ask a clarification ONLY when genuinely ambiguous; otherwise make the obvious assumption.\n"
    "Context:\n"
    "- Now: {now}\n"
    "- User facts: {memories}\n"
    "- Knowledge topics: {knowledge}\n"
    "- Workspace files: {files}"
)


def parse_json_object(raw: str) -> dict | None:
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj if isinstance(obj, dict) else None
            except ValueError:
                continue
    return None


def sanitize_final(text: str) -> str:
    """Never leak internal protocol into the conversation."""
    out = text.strip()
    out = re.sub(r"^\s*TOOL RESULT.*$", "", out, flags=re.MULTILINE)
    # A stray JSON object that failed to parse earlier must not reach the user.
    candidate = parse_json_object(out)
    if candidate is not None and ("tool" in candidate or "say" in candidate):
        inner = str(candidate.get("say", "")).strip()
        out = inner or re.sub(r"\{.*\}", "", out, flags=re.DOTALL).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()[:2000]


def build_system(user_text: str = "") -> str:
    from .conversation import build_messages

    memories = "; ".join(f"{m['key']}={m['value']}" for m in db.all_memories()[:15]) or "none"
    knowledge = ", ".join(k["topic"] for k in db.all_knowledge()[:12]) or "none"
    files = tools_call("list_files", {})[:200]
    corrections = db.matching_corrections(user_text or "")
    corrections_line = (
        "\n- Standing instructions from the user (OBEY these): "
        + " | ".join(c["instruction"] for c in corrections)
    ) if corrections else ""
    strategies_line = ""
    try:
        strat = db.relevant_strategies(user_text or "", limit=3)
        if strat:
            strategies_line = (
                "\n- Learned strategies (prefer these when the listed tool fails): "
                + "; ".join(f"if {s['fail_tool']} fails, use {s['win_tool']}" for s in strat)
            )
    except Exception:
        pass
    screen_summary = ""
    raw_summary = db.get_setting("last_screen_summary", "")
    if raw_summary and "|" in raw_summary:
        ts_raw, text = raw_summary.split("|", 1)
        try:
            if time.time() - float(ts_raw) < 1200:
                screen_summary = f"\n- Your screen, moments ago: {text.strip()}"
        except ValueError:
            pass
    ambient_on = db.get_setting("ambient_perception", "1") == "1"
    viewing = "unknown"
    if ambient_on:
        try:
            from .perception import active_window

            viewing = active_window()
        except Exception:
            pass
    return SYSTEM_TEMPLATE.format(
        name=config.ASSISTANT_NAME,
        address=config.USER_ADDRESS,
        now=datetime.now().strftime("%A %d %B %Y %H:%M"),
        memories=memories,
        knowledge=knowledge,
        files=files,
    ) + (
        f"\n- User is currently viewing: {viewing}"
        f"\n- You have vision: use see_screen when asked about their screen."
        + corrections_line
        + strategies_line
        + screen_summary
    )


def tools_call(name: str, args: dict) -> str:
    return tools.call(name, args)


def _deep_review(answer: str, user_text: str) -> str:
    """Deep Mode: one ruthless editor pass on substantive answers."""
    from .llm import chat

    try:
        review = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a ruthless editor reviewing an AI butler's spoken reply. "
                        "Fix factual errors, fill critical omissions, cut fluff. "
                        'Return ONLY JSON: {"say": "<improved reply>"}'
                    ),
                },
                {"role": "user", "content": f"User asked: {user_text}\n\nDraft reply: {answer}"},
            ],
            temperature=0.2,
        )
        better = parse_json_object(review)
        if better and str(better.get("say", "")).strip():
            return sanitize_final(str(better["say"]))[:1200]
    except Exception:
        pass
    return answer


_TOOL_HINTS = re.compile(
    r"\b(open|search|research|remind|timer|alarm|wake|write|run|code|debug|index|email|mail|click|watch|"
    r"build|hire|project|screenshot|volume|play|weather|calendar|youtube|learn|remember|skill|website|documents)\b",
    re.IGNORECASE,
)


def is_simple_query(text: str) -> bool:
    words = (text or "").split()
    return len(words) <= 5 and not _TOOL_HINTS.search(text or "")


def run_events(
    user_text: str,
    history: list[dict],
    on_event: Callable[[dict], None] | None = None,
    max_steps: int = MAX_STEPS,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Agentic turn with event callbacks for the UI.

    Events: {"type": "thinking"} | {"type": "tool", "name", "brief"}
            | {"type": "delta", "text"} | {"type": "done", "text"} | {"type": "error", "text"}
    The FINAL answer is streamed token-wise; tool steps stay quiet and are
    surfaced as compact status chips.
    """
    from .llm import chat_stream

    def emit(ev: dict) -> None:
        if on_event:
            try:
                on_event(ev)
            except Exception:
                pass

    messages: list[dict] = [{"role": "system", "content": build_system(user_text)}]
    # Conversation context pack: rolling summary + recent verbatim turns.
    try:
        from .conversation import build_messages

        context_msgs = [m for m in build_messages("") if m["role"] == "system"]
        for m in context_msgs[1:]:
            messages.append(m)
    except Exception:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})
    tool_manifest = json.dumps(tools.manifest())
    model_override = (config.FAST_MODEL or "") if is_simple_query(user_text) else ""
    last_error_tool = None

    for _ in range(max_steps):
        if cancelled and cancelled():
            emit({"type": "error", "text": "cancelled"})
            return ""
        prompt_messages = [
            {
                "role": "system",
                "content": messages[0]["content"]
                + "\nAvailable TOOLS (use exact names):\n"
                + tool_manifest,
            }
        ] + messages[1:]
        emit({"type": "thinking"})
        raw_parts: list[str] = []
        emit_mode = None  # decided by the first non-space character
        try:
            for delta in chat_stream(prompt_messages, temperature=0.4, model=model_override):
                raw_parts.append(delta)
                if emit_mode is None:
                    stripped = "".join(raw_parts).lstrip()
                    if not stripped:
                        continue
                    # Tool-call turns arrive as JSON: never leak them as text.
                    emit_mode = not stripped.startswith(("{", "```"))
                if emit_mode:
                    emit({"type": "delta", "text": delta})
        except Exception as exc:
            emit({"type": "error", "text": str(exc)[:160]})
            return f"My language core is unreachable ({str(exc)[:120]}). Local skills still work."
        raw = "".join(raw_parts).strip()
        data = parse_json_object(raw)
        if data is None:
            answer = sanitize_final(raw)[:1200] or "I drew a blank there."
            if db.get_setting("deep_mode", "0") == "1" and len(answer) > 20 and not (cancelled and cancelled()):
                emit({"type": "thinking"})
                answer = _deep_review(answer, user_text)
            if cancelled and cancelled():
                emit({"type": "error", "text": "cancelled"})
                return ""
            emit({"type": "done", "text": answer})
            return answer
        if "say" in data:
            answer = sanitize_final(str(data["say"]))[:1200]
            if db.get_setting("deep_mode", "0") == "1" and len(answer) > 20 and not (cancelled and cancelled()):
                emit({"type": "thinking"})
                answer = _deep_review(answer, user_text)
            if cancelled and cancelled():
                emit({"type": "error", "text": "cancelled"})
                return ""
            if answer:
                emit({"type": "done", "text": answer})
            return answer
        name = str(data.get("tool", "")).strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        brief = ", ".join(f"{k}={str(v)[:40]}" for k, v in list(args.items())[:2]) or "-"
        emit({"type": "tool", "name": name, "brief": brief})
        observation = tools.call(name, args)
        failed_now = observation.startswith(("TOOL_ERROR", "ERROR:", "DENIED"))
        if failed_now:
            last_error_tool = name
        elif last_error_tool and name != last_error_tool:
            try:
                db.bump_strategy(last_error_tool, name)
            except Exception:
                pass
            last_error_tool = None
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{observation[:2200]}"})

    answer = "That task needed more steps than I'm allowed in one go - I stopped to avoid surprises."
    emit({"type": "done", "text": answer})
    return answer


def run(user_text: str, history: list[dict], on_step: Callable[[str], None] | None = None,
        max_steps: int = MAX_STEPS) -> str:
    """Blocking variant used by voice/telegram (same pipeline, no deltas)."""
    events: list[str] = []

    def on_event(ev: dict) -> None:
        if ev["type"] in ("thinking", "tool"):
            label = "thinking..." if ev["type"] == "thinking" else f"{ev.get('name')}..."
            events.append(label)
            if on_step:
                try:
                    on_step(f"[{label}]")
                except Exception:
                    pass
        elif ev["type"] == "done":
            events.append(ev.get("text", ""))

    return run_events(user_text, history, on_event=on_event, max_steps=max_steps)
