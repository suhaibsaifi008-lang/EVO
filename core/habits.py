import re

from . import db
from .scheduler import dispatcher

CATEGORIES = {
    "schedule": (r"\bremind|timer|alarm|wake me|briefing\b",),
    "launch": (r"\bopen\b|\blaunch\b|\brun .*(app|program)\b",),
    "research": (r"\bsearch|research|look up|google\b|\blearn about\b|\bwhat do you know\b",),
    "coding": (r"\bwrite .* (script|code|program)|\brun the script|\bdebug\b|\bworkspace\b",),
    "system": (r"\bscreenshot|battery|cpu|ram|status|diagnostics|volume|lock\b",),
    "media": (r"\bplay|pause|next track|previous track|music|youtube\b",),
    "email": (r"\bemail|inbox|mail\b",),
    "memory": (r"\bremember|forget|what is my\b",),
    "projects": (r"\bproject\b|\bresume project\b",),
    "web_build": (r"\bwebsite|build me a site\b",),
    "documents": (r"\bindex_folder|my documents|my files\b",),
}


def classify(text: str) -> str:
    lowered = (text or "").lower()
    for category, patterns in CATEGORIES.items():
        for pattern in patterns:
            if re.search(pattern, lowered):
                return category
    return "conversation"


def record(text: str) -> str:
    category = classify(text)
    try:
        db.record_habit(category)
    except Exception:
        pass
    return category


def maybe_propose_skill(text: str) -> None:
    try:
        hit = db.track_repeat(text)
    except Exception:
        return
    if not hit:
        return
    try:
        dispatcher.publish({
            "type": "skill_proposal",
            "kind": "habit",
            "text": (
                f"You have asked '{hit['sample'][:80]}' {hit['count']} times. "
                f"Say 'make that a skill' and I will automate it permanently."
            ),
        })
    except Exception:
        pass


def summary_line() -> str:
    try:
        tops = db.top_habits(3)
    except Exception:
        return ""
    if not tops:
        return ""
    return ", ".join(f"{t['category']} ({t['count']}x)" for t in tops)
