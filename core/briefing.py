from datetime import datetime

from . import config, db, pc
from .brain_helpers import fetch_weather


def compose() -> str:
    now = datetime.now()
    lines = [now.strftime("It is %H:%M on %A, %d %B.")]
    try:
        from .calendarx import next_event_line

        cal = next_event_line()
        if cal:
            lines.append(cal)
    except Exception:
        pass
    pending = db.list_reminders()
    if pending:
        nxt = pending[0]
        due = datetime.fromtimestamp(nxt["due_at"])
        when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%A %H:%M")
        lines.append(f"You have {len(pending)} pending item(s), next at {when}: {nxt['message']}.")
    else:
        lines.append("Your schedule is clear.")
    city = db.get_setting("city", "")
    weather = fetch_weather(city)
    if weather:
        lines.append(f"Weather: {weather}.")
    try:
        s = pc.system_status()
        bat = f" Battery at {int(s['battery_percent'])} percent." if s.get("battery_percent") is not None else ""
        lines.append(f"All systems nominal.{bat}")
    except Exception:
        pass
    return " ".join(lines)


def assistant_name() -> str:
    return config.ASSISTANT_NAME
