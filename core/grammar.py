"""Grammar-rescue recognizer: near-perfect command recognition.

Builds a tiny Vosk grammar (a strict phrase list) from the user's actual
apps, sites and common commands. A grammar recognizer only ever outputs
phrases from that list, so when it produces a match we KNOW what was said -
rescuing the fuzzy general transcript for exactly the words that matter.
"""
import json
import re
import threading

_cache = {"phrases": None, "ts": 0.0}
_lock = threading.Lock()

_COMMAND_TEMPLATES = [
    "open {x}",
    "close {x}",
    "launch {x}",
    "start {x}",
    "search {x}",
    "search for {x}",
    "play {x}",
]
# (kept for reference; phrases are materialized per-target in grammar_phrases)

_SITES_EXTRA = {
    "youtube", "google", "gmail", "maps", "drive", "github", "reddit",
    "instagram", "facebook", "twitter", "chatgpt", "gemini", "copilot",
    "netflix", "amazon", "flipkart", "linkedin", "wikipedia", "twitch",
    "notepad", "calculator", "paint", "explorer", "terminal", "settings",
    "camera", "task manager", "word", "excel", "powerpoint", "steam",
    "spotify", "discord", "whatsapp", "telegram", "signal", "vlc",
    "valorant", "brave browser", "google chrome", "microsoft edge",
    "vs code", "minecraft", "fortnite", "roblox", "among us",
}


def _collect_targets() -> list:
    targets = set(_SITES_EXTRA)
    try:
        from . import pc

        targets.update(pc.SITES.keys())
        targets.update(pc.APP_ALIASES.keys())
        for stem in pc._lnk_index():
            if 2 <= len(stem) <= 24:
                targets.add(stem.lower())
        for name in pc._uwp_index():
            clean = re.sub(r"[^a-z ]", " ", name.lower()).strip()
            if clean:
                targets.add(clean)
    except Exception:
        pass
    cleaned = []
    seen = set()
    for t in targets:
        t = re.sub(r"\s+", " ", str(t)).strip()[:40]
        if t and t not in seen and re.match(r"^[a-z0-9][a-z0-9 \-]*$", t):
            seen.add(t)
            cleaned.append(t)
    return sorted(cleaned)


def grammar_phrases(max_phrases: int = 1500) -> list:
    """The strict phrase list for the rescuer recognizer. Cached 10 min.

    Priority order matters: static commands, then 'open X'/'close X' for
    every target, then bare target names. Never include raw templates.
    """
    with _lock:
        now = __import__("time").time()
        if _cache["phrases"] and now - _cache["ts"] < 600:
            return _cache["phrases"]
    phrases = [
        "what time is it", "what is the time", "volume up", "volume down",
        "mute", "take a screenshot", "screenshot", "goodbye",
        "stop listening", "search for {t}",
    ]
    targets = _collect_targets()
    for target in targets:
        phrases.append(f"open {target}")
        phrases.append(f"close {target}")
    phrases.extend(targets)
    # de-dup preserving priority order, cap size
    out = []
    seen = set()
    for p in phrases:
        if "{t}" in p:
            continue  # filled below per-target via search templates
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    search_phrases = [f"search for {t}" for t in targets]
    for sp in search_phrases:
        if sp not in seen:
            seen.add(sp)
            out.append(sp)
    out = out[:max_phrases]
    with _lock:
        _cache["phrases"] = out
        _cache["ts"] = now
    return out


def grammar_json() -> str:
    return json.dumps(grammar_phrases(), ensure_ascii=False)


def looks_like_command(text: str) -> bool:
    """Cheap check whether a general transcript resembles a known command."""
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return False
    for phrase in grammar_phrases():
        if t == phrase or t.startswith(phrase) or phrase.startswith(t):
            return True
    return False


_COMMAND_VERBS = ("open ", "close ", "launch ", "start ", "search", "play ")


def _strip_polite(text: str) -> str:
    t = text.strip()
    for prefix in ("please ", "can you ", "could you ", "hey ", "evo ", "jarvis ", "the ", "my "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return t


def trust_grammar(grammar_text: str, general_text: str) -> bool:
    """Decide whether the grammar rescuer should override the general model.

    Trust rules:
      - Command-verb phrases ('open youtube') on SHORT utterances: trusted.
      - When grammar and general transcript closely agree: trusted.
      - Long sentences from the general model mean real conversation ->
        never override (a strict grammar forced onto speech makes nonsense).
      - Bare vocabulary words alone ('wikipedia') are NOT enough - too easy
        to false-trigger on unrelated audio.
    """
    from difflib import SequenceMatcher

    g = re.sub(r"\s+", " ", (grammar_text or "").lower()).strip()
    gen = re.sub(r"\s+", " ", (general_text or "").lower()).strip()
    if not g or not gen:
        return False
    # Long conversational input wins unless it clearly started as a command.
    gen_clean = _strip_polite(gen)
    long_conversation = len(gen_clean.split()) >= 6
    verb_command = (
        any(g.startswith(v) for v in _COMMAND_VERBS)
        and len(g.split()) <= 5
        and (not long_conversation or gen_clean.startswith(_COMMAND_VERBS))
    )
    if verb_command:
        return True
    if long_conversation:
        return False
    return SequenceMatcher(None, gen, g).ratio() >= 0.6


def rescue_command(text: str) -> str | None:
    """Return the canonical command phrase that best matches a garbled
    transcript, or None when nothing is close enough."""
    from difflib import SequenceMatcher

    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    if not t:
        return None
    best, best_ratio = None, 0.0
    for phrase in grammar_phrases():
        ratio = SequenceMatcher(None, t, phrase).ratio()
        if ratio > best_ratio:
            best, best_ratio = phrase, ratio
    return best if best_ratio >= 0.75 else None
