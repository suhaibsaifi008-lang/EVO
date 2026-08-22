import re
from datetime import datetime, timedelta
from typing import Optional

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty five": 45, "forty-five": 45,
    "half an hour": 0.5,
}

UNIT_SECONDS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
}

DURATION_RE = re.compile(
    r"(?:(\d+(?:\.\d+)?)|one|an)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b",
    re.IGNORECASE,
)
CLOCK_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\b", re.IGNORECASE
)
BARE_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
AMPM_TIME_RE = re.compile(r"\b(\d{1,2})\s?(am|pm)\b", re.IGNORECASE)


def _num(token: str) -> float:
    return float(token)


def parse_duration(text: str) -> Optional[timedelta]:
    lowered = text.lower().replace(" and a half", ".5").replace("half a minute", "30 seconds")
    total = 0.0
    found = False
    for match in DURATION_RE.finditer(lowered):
        found = True
        raw_num = match.group(1)
        unit = match.group(2).lower()
        value = _num(raw_num) if raw_num else (0.5 if unit.startswith("hour") else 1.0)
        if not raw_num and unit.startswith("hour"):
            value = 0.5
        total += value * UNIT_SECONDS[unit]
    if not found:
        m = re.search(r"(\d+)\s*seconds?", lowered)
        if m:
            total += int(m.group(1))
            found = True
    return timedelta(seconds=total) if found and total > 0 else None


def parse_clock_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now()
    lowered = text.lower()

    day_offset = 0
    if "tomorrow" in lowered:
        day_offset = 1
    else:
        for name, idx in WEEKDAYS.items():
            if name in lowered:
                delta = (idx - now.weekday()) % 7
                day_offset = 7 if delta == 0 and "next" in lowered else delta
                break

    hour = minute = None
    pm = None

    m = CLOCK_RE.search(lowered)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if m.group(3):
            pm = "p" in m.group(3).replace(".", "")
    else:
        m2 = BARE_TIME_RE.search(lowered)
        if m2:
            hour, minute = int(m2.group(1)), int(m2.group(2))
        else:
            m3 = AMPM_TIME_RE.search(lowered)
            if m3:
                hour = int(m3.group(1))
                minute = 0
                pm = m3.group(2).lower() == "pm"
    if hour is None:
        return None

    if pm is True and hour < 12:
        hour += 12
    elif pm is False and hour == 12:
        hour = 0

    due = (now + timedelta(days=day_offset)).replace(
        hour=min(hour, 23), minute=min(minute, 59), second=0, microsecond=0
    )
    if due <= now:
        due += timedelta(days=1 if day_offset == 0 else 7)
    return due


def extract_after(text: str, keywords: tuple[str, ...]) -> str:
    for kw in keywords:
        pattern = re.escape(kw)
        m = re.search(pattern + r"[:\s]+(.+)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""
