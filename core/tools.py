import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import coding, config, db, gui_control, pc, swarm
from .brain_helpers import fetch_weather
from .nlp import parse_clock_time, parse_duration
from .webtools import fetch_page, search_web


@dataclass
class Tool:
    name: str
    description: str
    args: dict
    fn: Callable[..., str]


_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, args: dict | None = None):
    args = args or {}

    def deco(fn: Callable[..., str]) -> None:
        _REGISTRY[name] = Tool(name=name, description=description, args=args, fn=fn)

    return deco


def manifest() -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "args": {k: v for k, v in t.args.items()},
        }
        for t in _REGISTRY.values()
    ]


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def call(name: str, args: dict) -> str:
    t = _REGISTRY.get(name)
    if not t:
        return f"ERROR: unknown tool '{name}'"
    args = args or {}
    for arg_name, spec in t.args.items():
        if spec.get("required") and arg_name not in args:
            _audit(name, args, "error", f"missing required argument '{arg_name}'")
            return f"ERROR: missing required argument '{arg_name}'"
    try:
        result = str(t.fn(**args))
        _audit(name, args, "ok", result[:400])
        return result
    except Exception as exc:
        _audit(name, args, "error", str(exc)[:400])
        return f"TOOL_ERROR from {name}: {exc}"


def _audit(tool: str, args: dict, outcome: str, detail: str) -> None:
    try:
        from . import db as _db

        _db.log_audit(tool, json.dumps(args, ensure_ascii=False, default=str)[:240], outcome, detail)
    except Exception:
        pass


# ---------- PC control ----------


@tool("open_app", "Open a desktop application by name (e.g. chrome, notepad, spotify).", {"target": {"type": "string", "desc": "app name or website URL"}})
def _open_app(target: str) -> str:
    opened = pc.open_target(target)
    return f"Opened {opened}."


@tool("web_search", "Search the live internet; returns titles, URLs and snippets.", {"query": {"type": "string", "required": True}, "max_results": {"type": "integer", "default": 5}})
def _web_search(query: str, max_results: int = 5) -> str:
    results = search_web(query, min(int(max_results), 8))
    lines = [f"{i+1}. {r['title']}\n   {r['url']}\n   {r.get('snippet','')[:200]}" for i, r in enumerate(results)]
    return "\n".join(lines)


@tool("read_page", "Fetch a web page and return its readable text.", {"url": {"type": "string", "required": True}})
def _read_page(url: str) -> str:
    return fetch_page(url, max_chars=3500)


@tool("screenshot", "Capture the screen.")
def _screenshot() -> str:
    path = pc.screenshot()
    return f"Screenshot saved to {path} and folder opened."


@tool("system_status", "CPU load, RAM usage, battery and uptime of this PC.")
def _status() -> str:
    s = pc.system_status()
    parts = []
    if s.get("cpu_percent") is not None:
        parts.append(f"CPU {s['cpu_percent']}%")
    if s.get("ram_total_gb"):
        parts.append(f"RAM {s['ram_used_gb']}/{s['ram_total_gb']}GB")
    if s.get("battery_percent") is not None:
        parts.append(f"Battery {int(s['battery_percent'])}% ({'charging' if s.get('charging') else 'on battery'})")
    if s.get("uptime_hours") is not None:
        parts.append(f"Uptime {round(s['uptime_hours'])}h")
    return "; ".join(parts)


@tool("control_volume", "Raise/lower system volume or toggle mute.", {"action": {"type": "string", "enum": ["up", "down", "mute"], "required": True}, "steps": {"type": "integer", "default": 1}})
def _volume(action: str, steps: int = 1) -> str:
    steps = max(1, min(int(steps), 15))
    for _ in range(steps):
        pc.volume(action)
    return f"Volume action '{action}' x{steps} done."


@tool("press_media_key", "Control media playback.", {"action": {"type": "string", "enum": ["play", "pause", "next", "previous", "stop"], "required": True}})
def _media(action: str) -> str:
    pc.media_key(action)
    return f"Media key '{action}' sent."


@tool("lock_pc", "Lock the workstation immediately.")
def _lock() -> str:
    pc.lock_pc()
    return "Workstation locked."


# ---------- memory & knowledge ----------


@tool("remember_fact", "Persist a lasting fact about the user.", {"key": {"type": "string", "required": True}, "value": {"type": "string", "required": True}})
def _remember(key: str, value: str) -> str:
    db.remember(key.strip(), value.strip())
    return f"Stored: {key.strip()} = {value.strip()}"


@tool("recall_memory", "Look up stored personal facts.", {"query": {"type": "string", "required": True}})
def _recall(query: str) -> str:
    hits = db.search_memory(query)
    if not hits:
        return "No personal facts match."
    return "; ".join(f"{h['key']}={h['value']}" for h in hits[:10])


@tool("learn_topic", "Study a topic or URL and store it in the permanent knowledge base.", {"topic_or_url": {"type": "string", "required": True}})
def _learn(topic_or_url: str) -> str:
    import urllib.parse

    target = topic_or_url.strip()
    if re_match_url(target):
        content = fetch_page(target, max_chars=4000)
        topic = urllib.parse.urlparse(target if "//" in target else "https://" + target).netloc
        source = target
    elif config.llm_enabled():
        from .llm import chat

        content = chat(
            [
                {"role": "system", "content": "Explain the topic precisely but concisely (max 150 words). Facts only."},
                {"role": "user", "content": target},
            ],
            temperature=0.3,
        )
        topic, source = target, "language-core"
    else:
        results = search_web(target, max_results=3)
        content = "\n".join(f"{r['title']} - {r['url']}" for r in results)
        topic, source = target, "web-search"
    db.learn(topic, content[:4000], source)
    return f"Learned and stored '{topic}'. Preview: {content[:200].replace(chr(10), ' ')}"


def re_match_url(text: str) -> bool:
    import re as _re

    return bool(_re.match(r"^(https?://|www\.)\S+$", text.strip()))


@tool("recall_knowledge", "Query the permanent knowledge base of things JARVIS has studied.", {"query": {"type": "string", "required": True}})
def _knowledge(query: str) -> str:
    hits = db.recall_knowledge(query)
    if not hits:
        return f"Nothing studied about '{query}' yet."
    return " | ".join(f"{h['topic']}: {h['content'][:250]}" for h in hits)


# ---------- scheduling ----------


def _parse_due(desc: str):
    desc = (desc or "").strip()
    delta = parse_duration(desc)
    if delta:
        return datetime.now() + delta
    return parse_clock_time(desc)


@tool("add_reminder", "Schedule a reminder, timer or alarm. due_description examples: 'in 20 minutes', 'tomorrow at 7am', 'at 17:30'.", {
    "kind": {"type": "string", "enum": ["reminder", "timer", "alarm"]},
    "message": {"type": "string", "required": True},
    "due_description": {"type": "string", "required": True},
})
def _add_reminder(kind: str = "reminder", message: str = "", due_description: str = "") -> str:
    kind = kind if kind in ("reminder", "timer", "alarm") else "reminder"
    if kind == "timer" and not message.strip():
        message = "Timer"
    due = _parse_due(due_description)
    if not due:
        return "ERROR: could not understand the time. Use e.g. 'in 15 minutes' or 'at 9 pm'."
    rid = db.add_reminder(kind=kind, message=message.strip(), due_at=due.timestamp())
    mins = int((due - datetime.now()).total_seconds() // 60)
    return f"{kind.title()} #{rid} scheduled for {due.strftime('%H:%M')} (~{mins} min away): {message.strip()}"


@tool("list_reminders", "List active reminders, timers and alarms.")
def _reminders() -> str:
    items = db.list_reminders()
    now = datetime.now()
    if not items:
        return "Nothing scheduled."
    out = []
    for it in items[:10]:
        due = datetime.fromtimestamp(it["due_at"])
        when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%a %d %b %H:%M")
        out.append(f"#{it['id']} [{it['kind']}] {it['message']} @ {when}")
    return "; ".join(out)


@tool("cancel_reminder", "Cancel one item by id, or all with scope=all.", {"reminder_id": {"type": "integer"}, "scope": {"type": "string", "enum": ["all"]}})
def _cancel(reminder_id: int = None, scope: str = "") -> str:
    if scope == "all":
        n = db.cancel_all_reminders()
        return f"Cancelled {n} item(s)."
    if reminder_id is None:
        return "ERROR: provide reminder_id or scope='all'."
    ok = db.cancel_reminder(int(reminder_id))
    return "Cancelled." if ok else f"No reminder #{reminder_id}."


# ---------- coding sandbox ----------


@tool("save_code", "Save python code into the workspace.", {"filename": {"type": "string", "required": True}, "code": {"type": "string", "required": True}})
def _save_code(filename: str, code: str) -> str:
    filename = filename.strip().replace("\\", "/")
    if "/" in filename or ".." in filename:
        return "ERROR: plain filenames only."
    if not filename.endswith(".py"):
        filename += ".py"
    coding.write_file(filename, code)
    return f"Saved {filename} ({code.count(chr(10)) + 1} lines)."


@tool("run_code", "Execute a workspace python file. Requires user autonomy permission.", {"filename": {"type": "string", "required": True}, "timeout_seconds": {"type": "integer", "default": 25}})
def _run_code(filename: str, timeout_seconds: int = 25) -> str:
    if db.get_setting("auto_approve_code", "0") != "1":
        return (
            "DENIED: autonomous execution is disabled. Do NOT retry. Tell the user they can enable "
            "'Let JARVIS run its own code' in the Setup tab, then ask you again."
        )
    result = coding.run_python(filename=filename.strip(), timeout=max(5, min(int(timeout_seconds), 120)))
    if result["exit"] == 0 and not result["stderr"]:
        out = result["stdout"].strip()
        return f"Ran cleanly.{(' Output: ' + out[-800:]) if out else ''}"
    return f"FAILED exit={result['exit']}. stderr: {(result['stderr'] or '')[-600:]}"


@tool("read_file", "Read a file from the workspace.", {"filename": {"type": "string", "required": True}})
def _read_file(filename: str) -> str:
    content = coding.read_file(filename.strip())
    return content[:3000]


@tool("list_files", "List workspace files.")
def _files() -> str:
    files = coding.list_files()
    if not files:
        return "Workspace empty."
    return ", ".join(f"{f['name']}({f['size']}b)" for f in files[:20])


@tool("delete_file", "Delete a workspace file.", {"filename": {"type": "string", "required": True}})
def _delete_file(filename: str) -> str:
    return "Deleted." if coding.delete_file(filename.strip()) else "Not found."


# ---------- AI workers & projects ----------


@tool("hire_workers", "Spawn parallel AI workers on one task and merge their answers. Slow (up to ~2 min).", {"task": {"type": "string", "required": True}, "count": {"type": "integer", "default": 3}})
def _hire(task: str, count: int = 3) -> str:
    result = swarm.hire_workers(task, n=int(count))
    return f"Merged result: {result['final']}"


@tool("create_project", "Start a long-running background project. It plans, works step-by-step (can search web, read pages, write files) and announces when finished. Progress is checkpointed; stopped projects can be resumed.", {"goal": {"type": "string", "required": True}, "max_steps": {"type": "integer", "default": 40}})
def _project(goal: str, max_steps: int = 40) -> str:
    from .projects import manager

    pid = manager.start(goal.strip(), max_steps=max(1, min(int(max_steps), 200)))
    return f"Project #{pid} started in background with a {max(1, min(int(max_steps), 200))}-step budget. Its progress will be announced."


@tool("resume_project", "Resume a paused/stopped/failed background project from its last checkpoint.", {"project_id": {"type": "integer", "required": True}})
def _resume_project(project_id: int) -> str:
    from .projects import manager

    return manager.resume(int(project_id))


@tool("list_projects", "Show recent background projects and their status.")
def _projects() -> str:
    rows = db.list_projects()
    if not rows:
        return "No projects yet."
    return "; ".join(f"#{r['id']} [{r['status']}] {r['goal'][:60]}" for r in rows[:8])


# ---------- environment ----------


@tool("daily_briefing", "Compose the full morning briefing right now.")
def _briefing() -> str:
    from .briefing import compose

    return compose()


@tool("schedule_daily_briefing", "Enable the proactive daily spoken briefing at a given time.", {"time_description": {"type": "string", "required": True}, "enable": {"type": "boolean", "default": True}})
def _schedule_briefing(time_description: str, enable: bool = True) -> str:
    m = parse_brief_time(time_description)
    if not m:
        return "ERROR: give time like '08:00' or '8 am'."
    hh, mm = m
    db.set_setting("briefing_enabled", "1" if enable else "0")
    db.set_setting("briefing_time", f"{hh:02d}:{mm:02d}")
    return f"Daily briefing {'enabled' if enable else 'disabled'} at {hh:02d}:{mm:02d}."


def parse_brief_time(desc: str):
    import re as _re

    m = _re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", (desc or "").lower())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    mer = m.group(3)
    if mer == "pm" and hh < 12:
        hh += 12
    if mer == "am" and hh == 12:
        hh = 0
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return hh, mm
    return None


@tool("get_weather", "Current weather for a city (or the saved city).", {"city": {"type": "string"}})
def _weather(city: str = "") -> str:
    report = fetch_weather(city.strip() or db.get_setting("city", ""))
    return report or "Weather service unreachable."


@tool("set_city", "Save the user's city for weather reports.", {"city": {"type": "string", "required": True}})
def _city(city: str) -> str:
    db.set_setting("city", city.strip())
    return f"Weather city set to {city.strip()}."


@tool("current_datetime", "Exact local date and time right now.")
def _now() -> str:
    return datetime.now().strftime("%A %d %B %Y, %H:%M:%S")


# ---------- perception ----------


@tool("see_screen", "Look at the user's screen with vision and describe what is displayed.", {"question": {"type": "string", "desc": "specific thing to look for"}})
def _see_screen(question: str = "") -> str:
    from .perception import describe_screen

    return describe_screen(question.strip()[:300])


@tool("active_window", "The app and window title the user is currently focused on.")
def _active_window() -> str:
    from .perception import active_window

    return active_window()


@tool("list_windows", "Titles of visible desktop windows right now.")
def _windows() -> str:
    from .perception import visible_windows

    rows = visible_windows()
    return "; ".join(rows) if rows else "No visible windows."


@tool("network_status", "Internet connectivity and local/public IP addresses.")
def _net() -> str:
    from .perception import network_status

    return network_status()


# ---------- skill forge ----------

SKILL_CONTRACT = (
    "Script receives its arguments as ONE JSON string in sys.argv[1] "
    "(e.g. ['python', script, '{\"city\": \"London\"}']), does its work, "
    "prints the result to stdout as plain text, exits non-zero on failure."
)


@tool(
    "save_skill",
    f"TEACH a permanent new ability. Save a python script as a reusable tool named skill_<name> that you can call forever after. {SKILL_CONTRACT}",
    {
        "name": {"type": "string", "required": True},
        "description": {"type": "string", "required": True},
        "code": {"type": "string", "required": True},
        "args_schema": {"type": "object", "desc": "argname -> description"},
        "example_args": {"type": "object", "desc": "optional sample args to test-run immediately"},
    },
)
def _save_skill(name: str, description: str, code: str, args_schema: dict | None = None, example_args: dict | None = None) -> str:
    from . import skills

    return skills.save_skill(name, description, code, args_schema, example_args)


@tool("list_skills", "List learned skills and their descriptions.")
def _skills_list() -> str:
    from . import skills

    rows = skills.list_skills()
    if not rows:
        return "No learned skills yet."
    return "; ".join(f"{r['name']}: {r['description'][:80]}" for r in rows)


@tool("delete_skill", "Forget a learned skill permanently.", {"name": {"type": "string", "required": True}})
def _skill_delete(name: str) -> str:
    from . import skills

    clean = skills._safe_name(name)
    found = any(r["name"] == clean for r in skills.list_skills())
    skills.delete_skill(clean)
    return f"Skill '{clean}' deleted." if found else f"No skill named '{clean}'."


# ---------- watchers ----------

WATCHER_KINDS = (
    "battery_low (target='', threshold=battery percent floor), "
    "disk_high (target=drive letter e.g. C, threshold=percent full), "
    "website_change (target=url), "
    "news_keyword (target=search phrase)"
)


@tool("add_watcher", f"Create a background sentinel that checks periodically and ANNOUNCES aloud when it triggers. Kinds: {WATCHER_KINDS}.", {
    "kind": {"type": "string", "required": True},
    "target": {"type": "string"},
    "threshold": {"type": "number"},
    "interval_minutes": {"type": "number", "default": 15},
})
def _add_watcher(kind: str, target: str = "", threshold: float = 0, interval_minutes: float = 15) -> str:
    from . import watchers

    kind = kind.strip().lower()
    if kind not in ("battery_low", "disk_high", "website_change", "news_keyword"):
        return f"ERROR: unknown kind '{kind}'. Use one of: {WATCHER_KINDS}"
    wid = db.add_watcher(kind, target.strip(), float(threshold or 0), int(float(interval_minutes) * 60))
    return f"Watcher #{wid} armed: {kind} {('on ' + target) if target else ''} every {interval_minutes} min."


@tool("list_watchers", "Show all active and triggered sentinels.")
def _watchers_list() -> str:
    rows = db.list_watchers()
    if not rows:
        return "No watchers configured."
    parts = []
    for r in rows[:12]:
        last_note = f" — last: {r['note']}" if r.get("note") else ""
        parts.append(f"#{r['id']} [{r['status']}] {r['kind']} {r['target']}{last_note}")
    return "; ".join(parts)


@tool("remove_watcher", "Disarm and delete a sentinel.", {"watcher_id": {"type": "integer", "required": True}})
def _remove_watcher(watcher_id: int) -> str:
    return "Removed." if db.remove_watcher(int(watcher_id)) else f"No watcher #{watcher_id}."


# ---------- GUI control (gated) ----------


def _gui(fn, *args, **kwargs) -> str:
    from . import gui_control

    try:
        return fn(*args, **kwargs)
    except gui_control.GUIDisabled as exc:
        return f"DENIED: {exc}"
    except Exception as exc:
        return f"GUI_ERROR: {exc}"


@tool("gui_screen_size", "Screen resolution in pixels.")
def _gui_size() -> str:
    return str(_gui(gui_control.screen_size))


@tool("gui_move", "Move the mouse cursor to absolute pixel coordinates.", {"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True}})
def _gui_move(x: int, y: int) -> str:
    return _gui(gui_control.move, x, y)


@tool("gui_click", "Click at pixel coordinates.", {"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True}, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "clicks": {"type": "integer", "default": 1}})
def _gui_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    return _gui(gui_control.click, x, y, button=button, clicks=clicks)


@tool("gui_double_click", "Double-click at pixel coordinates.", {"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True}})
def _gui_dclick(x: int, y: int) -> str:
    return _gui(gui_control.double_click, x, y)


@tool("gui_right_click", "Right-click at pixel coordinates.", {"x": {"type": "integer", "required": True}, "y": {"type": "integer", "required": True}})
def _gui_rclick(x: int, y: int) -> str:
    return _gui(gui_control.right_click, x, y)


@tool("gui_drag", "Drag from one point to another.", {"x1": {"type": "integer", "required": True}, "y1": {"type": "integer", "required": True}, "x2": {"type": "integer", "required": True}, "y2": {"type": "integer", "required": True}})
def _gui_drag(x1: int, y1: int, x2: int, y2: int) -> str:
    return _gui(gui_control.drag, x1, y1, x2, y2)


@tool("gui_scroll", "Scroll the wheel; positive = up.", {"amount": {"type": "integer", "required": True}})
def _gui_scroll(amount: int) -> str:
    return _gui(gui_control.scroll, amount)


@tool("gui_type_text", "Type text into the focused window.", {"text": {"type": "string", "required": True}})
def _gui_type(text: str) -> str:
    return _gui(gui_control.type_text, text)


@tool("gui_press_key", "Press a single key (enter, tab, esc...).", {"key": {"type": "string", "required": True}})
def _gui_key(key: str) -> str:
    return _gui(gui_control.press_key, key)


@tool("gui_hotkey", "Press a key combo like 'ctrl+s' or 'alt+f4'.", {"combo": {"type": "string", "required": True}})
def _gui_hotkey(combo: str) -> str:
    return _gui(gui_control.hotkey, combo)


@tool("gui_focus_window", "Bring a window to front by part of its title.", {"title": {"type": "string", "required": True}})
def _gui_focus(title: str) -> str:
    return _gui(gui_control.focus_window, title)


@tool("gui_click_element", "VISION-GUIDED CLICK: find any on-screen element by describing it ('the Export button', 'the search box') and click its center. Use this instead of guessing pixels.", {"description": {"type": "string", "required": True}})
def _gui_click_element(description: str) -> str:
    return _gui(gui_control.click_element, description)


# ---------- calendar / media / smart home / push ----------


@tool("calendar_upcoming", "List upcoming events from the user's synced calendar (iCal feed).", {"days": {"type": "integer", "default": 7}})
def _calendar(days: int = 7) -> str:
    from . import calendarx

    try:
        return calendarx.format_events(calendarx.fetch_events(days=days))
    except Exception as exc:
        return f"Calendar unavailable: {exc}"


@tool("youtube_summary", "Fetch a YouTube video's transcript and summarise it (lectures, talks, reviews).", {"url": {"type": "string", "required": True}})
def _yt_summary(url: str) -> str:
    import re as _re

    m = _re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", url or "")
    if not m:
        return "ERROR: give a full YouTube URL."
    video_id = m.group(1)
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        try:
            fetched = YouTubeTranscriptApi().fetch(video_id)
            text = " ".join(s.text for s in fetched)
        except AttributeError:
            data = YouTubeTranscriptApi.get_transcript(video_id)
            text = " ".join(seg["text"] for seg in data)
    except Exception as exc:
        return f"Could not fetch transcript: {exc}"
    text = text[:14000]
    try:
        from .llm import chat

        summary = chat(
            [
                {"role": "system", "content": "Summarise this video transcript in max 120 spoken words. Key points first."},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
    except Exception as exc:
        summary = f"(transcript fetched but summarisation failed: {exc}) {text[:300]}"
    return f"Video summary: {summary}"


@tool("notify_phone", "Send a push notification to the user's phone via ntfy.", {"title": {"type": "string", "required": True}, "message": {"type": "string", "required": True}})
def _notify_phone(title: str, message: str) -> str:
    from . import notify

    ok = notify.push(title[:80], message[:500])
    return "Pushed to your phone." if ok else "Push unavailable — set JARVIS_NTFY_TOPIC in .env."


@tool("ha_call", "Trigger a Home Assistant service (smart home) e.g. domain='light', service='turn_on', entity_id='light.desk'.", {"domain": {"type": "string", "required": True}, "service": {"type": "string", "required": True}, "entity_id": {"type": "string"}, "data_json": {"type": "string"}})
def _ha_call(domain: str, service: str, entity_id: str = "", data_json: str = "") -> str:
    from . import smarthome

    return smarthome.call_service(domain, service, entity_id, data_json)


@tool("ha_state", "Read a Home Assistant entity's current state.", {"entity_id": {"type": "string", "required": True}})
def _ha_state(entity_id: str) -> str:
    from . import smarthome

    return smarthome.get_state(entity_id)


# ---------- charts & documents ----------


@tool("make_chart", "Generate a bar/line chart PNG from 'label:value; label:value' pairs and open it.", {"title": {"type": "string", "required": True}, "series": {"type": "string", "required": True}, "kind": {"type": "string", "enum": ["bar", "line"]}})
def _chart(title: str, series: str, kind: str = "bar") -> str:
    from . import reports

    return reports.make_chart(title, series, kind)


@tool("make_pdf", "Create a formatted PDF document from text (blank line = new paragraph, lines starting '- ' become bullets) and open it.", {"title": {"type": "string", "required": True}, "content": {"type": "string", "required": True}, "filename": {"type": "string"}})
def _pdf(title: str, content: str, filename: str = "") -> str:
    from . import reports

    return reports.make_pdf(title, content, filename)


# ---------- feedback memory ----------


@tool("remember_correction", "Store a PERMANENT behavioral instruction ('always X', 'never Y', 'when I say Z do W'). These are obeyed in every future conversation.", {"instruction": {"type": "string", "required": True}, "applies_to": {"type": "string", "desc": "optional topic keywords this applies to"}})
def _remember_correction(instruction: str, applies_to: str = "") -> str:
    db.add_correction(applies_to.strip(), instruction.strip())
    return f"Standing instruction stored: {instruction.strip()}"


@tool("list_corrections", "Show all standing behavioral instructions.")
def _corrections_list() -> str:
    rows = db.list_corrections()
    if not rows:
        return "No standing instructions yet."
    return "; ".join(f"#{r['id']}: {r['instruction']}" for r in rows[:15])


# ---------- file RAG ----------


@tool("index_folder", "Index a folder of documents (txt/md/py/json/csv/html/pdf) into EVO's private knowledge so questions about YOUR files can be answered.", {"path": {"type": "string", "required": True}})
def _index_folder(path: str) -> str:
    from . import filebrain

    try:
        stats = filebrain.index_folder(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    total = filebrain.status()
    return f"Indexed {stats['files_indexed']} file(s), {stats['chunks_added']} new chunks ({stats['skipped']} skipped). Library now: {total['files']} files, {total['chunks']} chunks."


@tool("rag_search", "Search the indexed personal documents for relevant passages.", {"query": {"type": "string", "required": True}})
def _rag_search(query: str) -> str:
    from . import filebrain

    results = filebrain.search(query)
    if not results and db.doc_stats()["files"] == 0:
        return "No documents indexed yet. Ask me to index_folder first."
    return filebrain.format_results(results)


@tool("rag_status", "How many files/chunks are in the personal document library.")
def _rag_status() -> str:
    from . import filebrain

    s = filebrain.status()
    return f"{s['files']} file(s), {s['chunks']} chunk(s)" + (f", last indexed {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['last_indexed']))}" if s["last_indexed"] else "")


# ---------- website builder ----------


@tool("build_website", "Build a complete professional multi-page static website (HTML/CSS) from a brief. Generates responsive, dark-mode-aware pages with real copy. Returns the folder; publish instructions included.", {
    "brief": {"type": "string", "required": True},
    "name": {"type": "string", "desc": "short folder/brand slug"},
    "pages": {"type": "string", "desc": "comma-separated page slugs e.g. 'index,about,pricing' (optional)"},
})
def _build_website(brief: str, name: str = "site", pages: str = "") -> str:
    from . import websmith

    page_list = [p.strip() for p in pages.split(",") if p.strip()] if pages else None
    try:
        result = websmith.build_site(brief, name=name or "site", pages=page_list)
    except Exception as exc:
        return f"BUILD FAILED: {exc}"
    return f"{result['summary']} Files: {', '.join(result['files'])}"


# ---------- email ----------


@tool("draft_email", "Compose an email and show the draft. Nothing is sent.", {"to": {"type": "string", "required": True}, "subject": {"type": "string", "required": True}, "body": {"type": "string", "required": True}})
def _mail_draft(to: str, subject: str, body: str) -> str:
    from . import mail

    return mail.draft_email(to, subject, body)


@tool("send_email", "Send an email. ALWAYS draft first and get explicit user approval; only then call with confirm=true.", {"to": {"type": "string", "required": True}, "subject": {"type": "string", "required": True}, "body": {"type": "string", "required": True}, "confirm": {"type": "boolean", "default": False}})
def _mail_send(to: str, subject: str, body: str, confirm: bool = False) -> str:
    from . import mail

    try:
        return mail.send_email(to, subject, body, confirm=bool(confirm))
    except mail.MailNotConfigured as exc:
        return f"NOT CONFIGURED: {exc}"


@tool("read_inbox", "Read the latest inbox messages (or unread only).", {"limit": {"type": "integer", "default": 6}, "unread_only": {"type": "boolean", "default": False}})
def _inbox(limit: int = 6, unread_only: bool = False) -> str:
    from . import mail

    try:
        return mail.read_inbox(limit=limit, unread_only=bool(unread_only))
    except mail.MailNotConfigured as exc:
        return f"NOT CONFIGURED: {exc}"


@tool(
    "deep_thought",
    "For genuinely HARD questions (analysis, strategy, tricky math, decisions): run three specialist reasoning passes in parallel — Analyst, Skeptic, Engineer — then merge them. Much sharper than answering directly. Use sparingly.",
    {"question": {"type": "string", "required": True}},
)
def _deep_thought(question: str) -> str:
    from concurrent.futures import ThreadPoolExecutor

    from .llm import chat

    roles = {
        "Analyst": "You are a rigorous analyst. Decompose the problem, examine evidence, quantify where possible.",
        "Skeptic": "You are a fierce skeptic. Attack assumptions, find flaws, identify what could be wrong or missing.",
        "Engineer": "You are a pragmatic engineer. Focus on what is actionable, feasible and concrete.",
    }
    question = question.strip()[:2000]

    def ask(role_prompt: str) -> str:
        try:
            return chat(
                [
                    {"role": "system", "content": role_prompt + " Be concise: max 120 words."},
                    {"role": "user", "content": question},
                ],
                temperature=0.5,
            )
        except Exception as exc:
            return f"[{role_prompt.split('.')[0]} unavailable: {exc}]"

    with ThreadPoolExecutor(max_workers=3) as pool:
        outputs = list(pool.map(ask, roles.values()))

    labeled = "\n\n".join(f"--- {role} ---\n{out}" for role, out in zip(roles.keys(), outputs))
    try:
        merged = chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Three specialists analyzed one question. Merge their views into one superior answer. "
                        "Resolve disagreements explicitly. Max 150 words."
                    ),
                },
                {"role": "user", "content": f"Question: {question}\n\n{labeled}"},
            ],
            temperature=0.3,
        )
    except Exception as exc:
        merged = f"{labeled}\n\n(Synthesis failed: {exc})"
    return merged
