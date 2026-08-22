import re
import urllib.request
from datetime import datetime, timedelta, timezone

from . import db


def _unfold(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line.rstrip("\r"))
    return lines


def _parse_dt(value: str):
    value = value.strip()
    m = re.match(r"^(\d{8})T(\d{6})(Z)?$", value)
    if m:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        if m.group(3):
            dt = dt.replace(tzinfo=timezone.utc).astimezone()
        return dt.replace(tzinfo=None)
    m = re.match(r"^(\d{8})$", value)
    if m:
        return datetime.strptime(m.group(1), "%Y%m%d")
    return None


def configured() -> bool:
    return bool(db.get_setting("calendar_ical_url", "").strip())


def fetch_events(days: int = 7) -> list[dict]:
    url = db.get_setting("calendar_ical_url", "").strip()
    if not url:
        raise RuntimeError("No calendar feed configured. Paste your secret iCal URL in Setup.")
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "ignore")

    events: list[dict] = []
    current: dict | None = None
    for line in _unfold(raw):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
        elif line.startswith("END:VEVENT"):
            if current and current.get("start"):
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            key, _, val = line.partition(":")
            base_key = key.split(";")[0].upper()
            val = val.strip()
            if base_key == "DTSTART":
                current["start"] = _parse_dt(val)
            elif base_key == "DTEND":
                current["end"] = _parse_dt(val)
            elif base_key == "SUMMARY":
                current["title"] = val[:160]
            elif base_key == "LOCATION":
                current["location"] = val[:120]

    now = datetime.now()
    horizon = now + timedelta(days=max(1, min(int(days), 30)))
    upcoming = [e for e in events if e.get("start") and now - timedelta(hours=2) <= e["start"] <= horizon]
    upcoming.sort(key=lambda e: e["start"])
    return upcoming


def format_events(events: list[dict], limit: int = 8) -> str:
    if not events:
        return "Nothing on the calendar for that window."
    parts = []
    today = datetime.now().date()
    for e in events[:limit]:
        start = e["start"]
        when = start.strftime("%H:%M") if start.date() == today else start.strftime("%a %d %b %H:%M")
        where = f" @ {e['location']}" if e.get("location") else ""
        parts.append(f"{when} — {e.get('title', '(untitled)')}{where}")
    return "; ".join(parts)


def next_event_line() -> str:
    try:
        events = fetch_events(days=2)
    except Exception:
        return ""
    if not events:
        return ""
    first = events[0]
    when = first["start"].strftime("%H:%M") if first["start"].date() == datetime.now().date() else first["start"].strftime("%a %H:%M")
    return f"Your next commitment is {first.get('title', 'an event')} at {when}."
