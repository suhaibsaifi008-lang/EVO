"""Vocabulary-aware correction for offline ASR output.

Vosk mangles brand names ("youtube" -> "utube", "brave" -> "breve"). We know
EXACTLY which words matter - installed apps, known sites, command words - so
we fuzzy-map transcribed tokens onto that vocabulary before intent parsing.
"""
import re
from difflib import SequenceMatcher

_cache = {"vocab": None, "ts": 0.0}

# Common words the fuzzy fixer must NEVER touch - a wrong "correction" here
# corrupts ordinary sentences ("time" -> "timer").
SAFE_WORDS = {
    "what", "when", "where", "which", "who", "how", "why", "open", "close",
    "play", "stop", "start", "make", "take", "give", "want", "need", "know",
    "time", "times", "today", "tomorrow", "date", "day", "week", "month",
    "year", "hour", "hours", "minute", "minutes", "second", "seconds",
    "that", "this", "there", "then", "than", "them", "they", "with",
    "from", "have", "having", "about", "above", "after", "again", "against",
    "before", "being", "below", "between", "both", "down", "during", "each",
    "few", "more", "most", "other", "some", "such", "only", "over", "same",
    "very", "will", "just", "should", "now",
}

CORE_WORDS = {
    "youtube", "calculator", "notepad", "browser", "chrome", "edge",
    "firefox", "brave", "weather", "volume", "music", "screenshot",
    "remind", "timer", "alarm", "wikipedia", "whatsapp", "telegram",
    "spotify", "discord", "steam", "valorant", "copilot", "explorer",
    "terminal", "settings", "camera", "maps", "gmail", "reddit",
    "instagram", "netflix", "amazon", "flipkart", "linkedin",
    "shutdown", "restart", "mute", "play", "pause", "next", "previous",
}

# Phrases ASR commonly produces for these targets.
PHRASE_ALIASES = {
    "you tube": "youtube",
    "utube": "youtube",
    "u tube": "youtube",
    "youtub": "youtube",
    "you chose": "youtube",
    "brave browser": "brave browser",
    "grave browser": "brave browser",
    "bravebrowser": "brave browser",
    "calculation": "calculator",
    "calculater": "calculator",
    "wiki pedia": "wikipedia",
    "wikipeadia": "wikipedia",
    "what's app": "whatsapp",
    "whats app": "whatsapp",
    "tele gram": "telegram",
}


def _build_vocab() -> set:
    now = __import__("time").time()
    if _cache["vocab"] and now - _cache["ts"] < 300:
        return _cache["vocab"]
    vocab = set(CORE_WORDS)
    try:
        from . import pc

        vocab.update(pc.SITES.keys())
        vocab.update(pc.APP_ALIASES.keys())
        for stem in pc._lnk_index():
            if 3 <= len(stem) <= 24 and stem.isalpha():
                vocab.add(stem)
        for name in pc._uwp_index():
            clean = re.sub(r"[^a-z ]", "", name).strip()
            if clean:
                vocab.add(clean)
    except Exception:
        pass
    _cache["vocab"] = vocab
    _cache["ts"] = now
    return vocab


def _best_match(token: str, vocab: set) -> str | None:
    best, best_ratio = None, 0.0
    ln = len(token)
    for word in vocab:
        if abs(len(word) - ln) > 2:
            continue
        ratio = SequenceMatcher(None, token, word).ratio()
        if ratio > best_ratio:
            best, best_ratio = word, ratio
    return best if best_ratio >= 0.86 else None


def correct_terms(text: str) -> str:
    """Phrase aliases first, then conservative per-token fuzzy correction.

    Tokens that are common English words (SAFE_WORDS) or already in-vocab are
    never altered; only genuinely garbled brand/app words get mapped.
    """
    if not text or not text.strip():
        return text or ""
    out = text
    lowered = out.lower()
    for bad, good in PHRASE_ALIASES.items():
        if bad in lowered:
            pattern = re.compile(re.escape(bad), re.IGNORECASE)
            out = pattern.sub(good, out)
            lowered = out.lower()

    tokens = re.split(r"(\s+)", out)
    vocab = _build_vocab()
    fixed = []
    for tok in tokens:
        stripped = tok.strip()
        low = stripped.lower()
        if (
            not stripped or not low.isalpha() or len(low) < 5
            or low in vocab or low in SAFE_WORDS
        ):
            fixed.append(tok)
            continue
        match = _best_match(low, vocab)
        fixed.append(match if match else tok)
    return "".join(fixed)
