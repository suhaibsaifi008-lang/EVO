"""ConversationManager — EVO's conversational brain-layer.

Responsibilities (architecture, not prompt hacks):
  1. CONTEXT PACKING   recent turns verbatim + rolling summary of older turns
                       + relevant memories/knowledge + live machine state.
  2. SUMMARIZATION     long conversations are compressed by the fast model;
                       decisions/questions/preferences survive in the summary.
  3. MEMORY POLICY     durable facts are extracted sparingly and stored as an
                       upsert (keyed), so updates replace stale facts instead
                       of accumulating contradictions. Sensitive content is
                       only stored when explicitly requested.
  4. STATE             current topic / last action outcome / unresolved
                       question - injected so "that", "it", "do it again"
                       resolve naturally.

Voice, text, Telegram all go through Brain.respond*, which uses this manager,
so every surface shares one conversation state.
"""
import json
import re
import threading
import time

from . import config, db

_LOCK = threading.RLock()  # re-entrant: state helpers nest (record_turn -> set_topic)

# How many of the newest messages are always sent verbatim.
RECENT_VERBATIM = 14
# Once this many unsummarized messages pile up, older ones get compressed.
SUMMARIZE_THRESHOLD = 34
# Run memory extraction at most this often (seconds) unless explicit.
MEMORY_MIN_INTERVAL = 90.0
SENSITIVE_RE = re.compile(r"password|api[_ ]?key|token|secret|otp|pin\b|credit|cvv", re.I)

_state = {
    "summary": "",
    "summary_upto": 0,       # message-id covered by the summary
    "topic": "",
    "last_action": "",
    "pending_question": "",
    "last_extraction": 0.0,
}


def _load_persisted() -> None:
    with _LOCK:
        if _state["summary_upto"]:
            return
        try:
            _state["summary"] = db.get_setting("convo_summary", "") or ""
            _state["summary_upto"] = int(float(db.get_setting("convo_summary_upto", "0") or 0))
        except Exception:
            pass


def note_action(brief: str) -> None:
    with _LOCK:
        _state["last_action"] = brief[:200]


def set_topic(topic: str) -> None:
    with _LOCK:
        _state["topic"] = (topic or "")[:120]


def reset() -> None:
    with _LOCK:
        _state.update({"summary": "", "summary_upto": 0, "topic": "",
                       "last_action": "", "pending_question": ""})
        db.set_setting("convo_summary", "")
        db.set_setting("convo_summary_upto", "0")


# ------------------------------------------------------------ context packing


def build_messages(user_text: str) -> list[dict]:
    """Assemble the LLM message list: identity+state block, rolling summary,
    then the newest turns verbatim. This is what lets 'that', 'it' and
    'what about tomorrow?' work without repeating context."""
    _load_persisted()
    rows = db.recent_messages(RECENT_VERBATIM * 2)
    history = rows[-(RECENT_VERBATIM + 1):]  # excludes the user text we append last
    blocks: list[str] = []
    if _state["summary"]:
        blocks.append(
            "Earlier conversation (compressed):\n" + _state["summary"]
        )
    if _state["last_action"]:
        blocks.append(f"Last action you performed: {_state['last_action']}")
    if _state["topic"]:
        blocks.append(f"Current topic: {_state['topic']}")
    try:
        from . import world_state

        blocks.append("Right now: " + world_state.context_line())
    except Exception:
        pass
    messages: list[dict] = []
    if blocks:
        messages.append({"role": "system", "content": "\n".join(blocks)})
    for r in history[-RECENT_VERBATIM:]:
        role = "user" if r["role"] == "user" else "assistant"
        content = (r.get("content") or "").strip()
        if not content or content.startswith("["):
            continue
        messages.append({"role": role, "content": content[:1500]})
    messages.append({"role": "user", "content": user_text})
    return messages


# ------------------------------------------------------------ summarization


def maybe_summarize(force: bool = False) -> bool:
    """Compress old turns into the rolling summary using the fast model."""
    _load_persisted()
    rows = db.recent_messages(SUMMARIZE_THRESHOLD * 2)
    if len(rows) < SUMMARIZE_THRESHOLD and not force:
        return False
    cutoff = rows[len(rows) // 2]["id"] if rows else 0
    older = [r for r in rows if r["id"] <= _state["summary_upto"] or r["id"] <= cutoff]
    older = [r for r in older if not (r.get("content") or "").startswith("[")][-24:]
    if len(older) < 8 and not force:
        return False
    transcript = "\n".join(
        f"{'User' if r['role'] == 'user' else 'EVO'}: {(r['content'] or '')[:400]}"
        for r in older
    )
    from .llm import chat

    try:
        notes = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Compress this conversation excerpt into compact notes that let "
                        "an assistant continue seamlessly later. Keep: decisions made, "
                        "unresolved questions, user preferences/facts revealed, tasks and "
                        "their status, names/numbers/URLs. Bullet fragments only. Max 180 words."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            role="fast",
            temperature=0.2,
            timeout=45,
        )
    except Exception:
        return False
    with _LOCK:
        merged = (_state["summary"] + "\n" + notes).strip() if _state["summary"] else notes
        _state["summary"] = merged[-2400:]
        _state["summary_upto"] = max(_state["summary_upto"], max((r["id"] for r in older), default=0))
        db.set_setting("convo_summary", _state["summary"])
        db.set_setting("convo_summary_upto", str(_state["summary_upto"]))
    return True


# ------------------------------------------------------------ memory policy


def extract_and_store_memory(user_text: str, reply: str) -> dict | None:
    """Pull durable facts out of a turn under strict rules; upsert keyed."""
    combined = f"{user_text}\n{reply}"
    explicit = bool(re.search(r"\bremember\b|\bkeep in mind\b", user_text, re.I))
    if SENSITIVE_RE.search(combined) and not explicit:
        return None
    from .llm import chat

    known = "; ".join(f"{m['key']}={m['value']}" for m in db.all_memories()[:20]) or "none"
    try:
        raw = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Decide what durable personal facts from this exchange should be "
                        "stored long-term. Store ONLY: stable preferences, personal config "
                        "(city, job, devices), ongoing projects, important decisions, or "
                        "explicitly requested memories. Never store temporary chatter. "
                        'Reply ONLY JSON: {"remember": bool, "items": [{"key": "...", "value": "..."}]} '
                        "Reuse an existing key to UPDATE a fact."
                    ),
                },
                {"role": "user", "content": f"Known memories: {known}\n\nExchange:\nUser: {user_text[:800]}\nEVO: {reply[:500]}"},
            ],
            role="fast",
            temperature=0.0,
            timeout=30,
        )
    except Exception:
        return None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not data.get("remember"):
        return None
    stored = []
    for item in (data.get("items") or [])[:4]:
        key = str(item.get("key", "")).strip()[:60]
        value = str(item.get("value", "")).strip()[:300]
        if key and value:
            db.remember(key, value)
            stored.append(key)
    return {"stored": stored} if stored else None


def learn_from_turn(user_text: str, reply: str) -> None:
    """Fire-and-forget post-turn housekeeping (called on a worker thread)."""
    explicit = bool(re.search(r"\bremember\b", user_text, re.I))
    now = time.time()
    with _LOCK:
        due = explicit or (now - _state["last_extraction"] > MEMORY_MIN_INTERVAL)
        if due:
            _state["last_extraction"] = now
    if not due:
        return
    result = extract_and_store_memory(user_text, reply)
    if result:
        try:
            from .scheduler import dispatcher

            dispatcher.publish({
                "type": "note", "kind": "memory",
                "text": "Noted: " + ", ".join(result["stored"]),
                "spoken": False,
            })
        except Exception:
            pass
    try:
        maybe_summarize()
    except Exception:
        pass


def topic_guess(user_text: str) -> str:
    words = [w for w in re.findall(r"[a-zA-Z]{4,}", user_text)][:6]
    return " ".join(words)[:120]


def record_turn(user_text: str, reply: str) -> None:
    """Update lightweight state after each completed turn."""
    if not user_text:
        return
    lowered = user_text.lower().strip()
    if len(lowered.split()) >= 3 and not lowered.startswith(("what", "who", "when", "how")):
        pass  # topic stays sticky; only refresh on substantial new input
    with _LOCK:
        if not _state["topic"] or re.search(r"\b(instead|now talk about|new topic|switch)\b", lowered):
            set_topic(topic_guess(user_text))
