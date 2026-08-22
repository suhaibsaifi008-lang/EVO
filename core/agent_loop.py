import json
import re
import time
from datetime import datetime
from typing import Callable

from . import config, db, tools


MAX_STEPS = 6

SYSTEM_TEMPLATE = (
    "You are {name}, an autonomous AI butler running locally on the user's Windows PC. "
    "Address the user as '{address}'. Style: precise, dry wit, never verbose â€” spoken replies under 80 words.\n"
    "You control this PC through TOOLS. To use one, reply with ONLY a JSON object:\n"
    '{{"tool": "tool_name", "args": {{...}}}}\n'
    "You will receive its result, then continue. When you have everything you need (or for plain conversation), "
    'reply with ONLY: {{"say": "<your spoken reply>"}}\n'
    "Rules:\n"
    "- One tool call per reply. Never invent tools. Never output anything except the single JSON object.\n"
    "- Prefer tools over guessing facts. Combine multiple facts into ONE final say.\n"
    "- Tools named skill_* are permanent abilities the user taught you â€” prefer them when relevant.\n"
    "- For genuinely hard analysis, strategy or math questions, use deep_thought instead of answering shallowly.\n"
    "- If a tool returns DENIED or TOOL_ERROR, explain gracefully instead of retrying twice.\n"
    "- Destructive power actions (shutdown/restart) are NOT available; if asked, say they need explicit setup.\n"
    "Context:\n"
    "- Now: {now}\n"
    "- User facts: {memories}\n"
    "- Knowledge topics: {knowledge}\n"
    "- Schedule: {reminders}\n"
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


def build_system(user_text: str = "") -> str:
    memories = "; ".join(f"{m['key']}={m['value']}" for m in db.all_memories()[:15]) or "none"
    knowledge = ", ".join(k["topic"] for k in db.all_knowledge()[:12]) or "none"
    schedule = tools.call("list_reminders", {})[:300]
    files = tools.call("list_files", {})[:200]
    relevant_rows = db.relevant_knowledge(user_text or "")
    relevant = "; ".join(f"{r['topic']}: {r['snippet']}" for r in relevant_rows)[:600]
    corrections = db.matching_corrections(user_text or "")
    corrections_line = (
        "\n- Standing instructions from the user (OBEY these): "
        + " | ".join(c["instruction"] for c in corrections)
    ) if corrections else ""
    world_line = ""
    try:
        from . import world_state

        world_line = "\n- World state: " + world_state.context_line()
    except Exception:
        world_line = ""
    strategies_line = ""
    try:
        strat = db.relevant_strategies(user_text or "", limit=3)
        if strat:
            strategies_line = (
                "\n- Learned strategies (prefer these when the listed tool fails): "
                + "; ".join(f"if {s['fail_tool']} fails, use {s['win_tool']}" for s in strat)
            )
    except Exception:
        strategies_line = ""
    try:
        from .habits import summary_line

        habits_line = f"\n- User's frequent request areas: {summary_line()}" if summary_line() else ""
    except Exception:
        habits_line = ""
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
        reminders=schedule,
        files=files,
    ) + (
        f"\n- User is currently viewing: {viewing}"
        "\n- You have vision: use see_screen when asked about their screen."
        + (f"\n- Relevant studies: {relevant}" if relevant else "")
        + corrections_line
        + habits_line
        + screen_summary
        + world_line
        + strategies_line
    )


_TOOL_HINTS = re.compile(
    r"\b(open|search|research|remind|timer|alarm|wake|write|run|code|debug|index|email|mail|click|watch|"
    r"build|hire|project|screenshot|volume|play|weather|calendar|youtube|learn|remember|skill|website|documents)\b",
    re.IGNORECASE,
)


def is_simple_query(text: str) -> bool:
    words = (text or "").split()
    return len(words) <= 5 and not _TOOL_HINTS.search(text or "")


def run(
    user_text: str,
    history: list[dict],
    on_step: Callable[[str], None] | None = None,
    max_steps: int = MAX_STEPS,
) -> str:
    from .llm import chat

    messages: list[dict] = [{"role": "system", "content": build_system(user_text)}]
    messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})
    tool_manifest = json.dumps(tools.manifest())
    deep_mode = db.get_setting("deep_mode", "0") == "1"
    model_override = (config.FAST_MODEL or "") if is_simple_query(user_text) else ""
    last_error_tool = None

    for _ in range(max_steps):
        prompt_messages = [
            {
                "role": "system",
                "content": messages[0]["content"]
                + "\nAvailable TOOLS (use exact names):\n"
                + tool_manifest,
            }
        ] + messages[1:]
        raw = chat(prompt_messages, temperature=0.4, model=model_override)
        data = parse_json_object(raw)
        if data is None:
            return raw.strip()[:900] or "I drew a blank there."
        if "say" in data:
            answer = str(data["say"]).strip()[:1200]
            if deep_mode and len(answer) > 20:
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
                        answer = str(better["say"]).strip()[:1200]
                except Exception:
                    pass
            return answer
        name = str(data.get("tool", "")).strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
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
        if on_step:
            try:
                on_step(f"[{name}] {observation[:140]}")
            except Exception:
                pass
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{observation[:2500]}"})

    return "That task needed more steps than I am allowed in one go â€” I have stopped to avoid surprises."
