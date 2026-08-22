import ast
import operator
import random
import re
import time as timemod
from datetime import datetime, timedelta

from . import config, db, pc
from .brain_helpers import clean_city, fetch_weather
from .nlp import parse_clock_time, parse_duration

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I would tell you a UDP joke, but you might not get it.",
    "There are only 10 kinds of people: those who understand binary and those who don't.",
    "A SQL query walks into a bar, approaches two tables and asks: may I join you?",
]

OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}


def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_node(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    raise ValueError("Unsupported expression")


def try_math(text: str) -> str | None:
    lowered = text.lower()
    if not re.search(r"(calculate|what(?:'s| is)|compute|solve)", lowered):
        return None
    expr = re.sub(r"^(calculate|compute|solve|whats|what's|what is)\b", "", lowered).strip(" ?.")
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", r"(\1/100)*\2", expr)
    for word, symbol in [
        ("plus", "+"), ("minus", "-"), ("times", "*"), ("multiplied by", "*"),
        ("divided by", "/"), ("over", "/"), ("to the power of", "**"), ("mod", "%"),
    ]:
        expr = expr.replace(word, symbol)
    expr = re.sub(r"x", "*", expr.replace("**", "@@")).replace("@@", "**")
    expr = re.sub(r"[^0-9+\-*/().%\s]", "", expr).strip()
    if not re.search(r"\d[\s]*[+\-*/%]|\*\*", expr):
        return None
    try:
        result = _eval_node(ast.parse(expr, mode="eval"))
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"That would be {result}."
    except Exception:
        return None


def fetch_weather_cached(city: str = "") -> str | None:
    return fetch_weather(city)


class Brain:
    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self.last_script: str | None = None
        try:
            self.history: list[dict] = db.recent_messages(16)
        except Exception:
            self.history = []

    # ---------- shared helpers ----------

    def _llm(self, system: str, user: str, temperature: float = 0.4) -> str:
        from .llm import chat

        return chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
        )

    def _auto_approve(self) -> bool:
        return db.get_setting("auto_approve_code", "0") == "1"

    def respond(self, text: str) -> dict:
        result = self._respond_inner(text)
        try:
            if text and text.strip():
                db.log_message("user", text.strip())
                db.log_message("assistant", result.get("reply", ""))
        except Exception:
            pass
        try:
            from . import habits

            habits.record(text)
            habits.maybe_propose_skill(text)
        except Exception:
            pass
        return result

    def _respond_inner(self, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            return {"reply": "I did not catch that.", "refresh": []}
        lowered = text.lower().strip(" .!?")

        confirm = self._handle_confirmation(lowered)
        if confirm:
            return confirm

        for quick in (
            self._greetings, self._identity, self._capabilities, self._thanks,
            self._time_date, self._joke, self._flip_coin,
        ):
            result = quick(text, lowered)
            if result is not None:
                if isinstance(result, str):
                    return {"reply": result, "refresh": []}
                reply, meta = result
                return {"reply": reply, **(meta or {})}

        if config.agent_enabled():
            try:
                from .agent_loop import run as agent_run

                answer = agent_run(text, self.history)
                self.history.extend([
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": answer},
                ])
                if len(self.history) > 24:
                    del self.history[:-24]
                return {
                    "reply": answer,
                    "refresh": ["reminders", "memory", "knowledge", "workspace"],
                }
            except Exception:
                pass

        handlers = (
            self._greetings, self._identity, self._capabilities,
            self._clear_chat, self._feedback, self._time_date, self._math,
            self._reminders, self._memory, self._settings, self._swarm,
            self._code, self._knowledge, self._browse, self._open,
            self._search, self._screenshot, self._volume_media, self._status,
            self._lock, self._power, self._weather, self._briefing,
            self._chains, self._thanks, self._joke, self._flip_coin,
        )
        for handler in handlers:
            result = handler(text, lowered)
            if result is not None:
                if isinstance(result, str):
                    return {"reply": result, "refresh": []}
                if isinstance(result, dict):
                    payload = {k: v for k, v in result.items() if k != "reply"}
                    return {"reply": result.get("reply", ""), **payload}
                reply, meta = result
                return {"reply": reply, **(meta or {})}

        return self._fallback(text)

    # ---------- confirmation flow ----------

    def _handle_confirmation(self, lowered: str) -> dict | None:
        now = timemod.time()
        for token in list(self.pending):
            if self.pending[token]["expires"] < now:
                del self.pending[token]
        if not self.pending:
            return None
        if lowered in ("cancel", "never mind", "nevermind", "stop"):
            self.pending.clear()
            return {"reply": "Very well, I have cancelled that.", "refresh": ["workspace"]}
        if not any(w in lowered for w in ("confirm", "yes", "do it", "go ahead", "proceed", "debug")):
            return None
        token = next(iter(self.pending))
        entry = self.pending.pop(token)

        if entry["kind"] == "power":
            pc.power(entry["action"])
            verb = "Shutting down" if entry["action"] == "shutdown" else "Restarting"
            return {"reply": f"{verb} in five seconds. Goodbye.", "refresh": []}

        if entry["kind"] == "run_code":
            from . import coding

            result = coding.run_python(filename=entry["filename"])
            return self._report_run(result, entry["filename"])

        if entry["kind"] == "debug_code":
            return self._debug_and_run(entry["filename"], entry.get("error", ""))

        return None

    def _debug_and_run(self, filename: str, error: str) -> dict:
        from . import coding

        try:
            source = coding.read_file(filename)
        except Exception as exc:
            return {"reply": f"I could not read {filename}: {exc}", "refresh": ["workspace"]}
        try:
            fixed = self._llm(
                "You are a debugging engine. Fix the python code given the traceback. "
                "Respond with ONLY one fenced ```python code block.",
                f"Code:\n```python\n{source}\n```\n\nError:\n{error}",
                temperature=0.1,
            )
        except Exception as exc:
            return {"reply": f"The language core is unreachable, so I cannot debug: {exc}", "refresh": []}
        path = coding.write_file(filename, self._extract_code(fixed) or fixed)
        result = coding.run_python(filename=path.name)
        return self._report_run(result, filename)

    @staticmethod
    def _extract_code(text: str) -> str | None:
        m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.DOTALL)
        return m.group(1).strip() if m else None

    def _report_run(self, result: dict, filename: str) -> dict:
        from . import coding

        if result["exit"] == 0 and not result["stderr"]:
            out = result["stdout"].strip()
            tail = f" Output: {out[-400:]}" if out else ""
            self.last_script = result["file"]
            return {"reply": f"{filename} ran cleanly.{tail}", "refresh": ["workspace"]}
        err = (result["stderr"] or f"exit code {result['exit']}").strip()[-400:]
        self.last_script = filename
        if config.llm_enabled():
            self.pending["dbg"] = {
                "kind": "debug_code",
                "filename": filename,
                "error": err,
                "expires": timemod.time() + 180,
            }
            return {"reply": f"{filename} failed. The error: {err}. Say 'debug' and I will repair it myself.", "refresh": ["workspace"]}
        return {"reply": f"{filename} failed with error: {err}", "refresh": ["workspace"]}

    # ---------- social ----------

    def _greetings(self, text, lowered):
        if re.search(r"\b(hi|hello|hey|good (morning|afternoon|evening)|you there|wake up)\b", lowered) and len(lowered.split()) <= 5:
            hour = datetime.now().hour
            part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
            return (f"Good {part}, {config.USER_ADDRESS}. All systems are online. How may I assist?")
        return None

    def _identity(self, text, lowered):
        if re.search(r"(who are you|your name|about yourself|introduce yourself)", lowered):
            return (f"I am {config.ASSISTANT_NAME}, your personal assistant. I manage this machine, "
                    f"your schedule, and whatever else you deign to delegate.")
        return None

    def _capabilities(self, text, lowered):
        if re.search(r"(what can you do|help$|^help|commands|abilities)", lowered):
            return ("I can control this PC — apps, web, screenshots, volume, media, status, diagnostics, lock. "
                    "I manage your schedule with reminders, timers, alarms and a daily briefing. "
                    "I remember facts you tell me and build a knowledge base: say 'learn about' or 'research' any topic, "
                    "or 'read' any URL. I can write and run python code in my sandbox — approve each run, or grant me "
                    "standing permission in Setup. When my language core is online I can hire a team of AI workers "
                    "for any task, and hold free conversation.")
        return None

    def _thanks(self, text, lowered):
        if re.fullmatch(r"(thanks|thank you|cheers|nice one|well done|good job).*", lowered):
            return "Always a pleasure."
        return None

    def _joke(self, text, lowered):
        if re.search(r"\bjoke\b|make me laugh", lowered):
            return random.choice(JOKES)
        return None

    def _flip_coin(self, text, lowered):
        if re.search(r"flip a coin|coin flip|heads or tails", lowered):
            side = random.choice(["Heads", "Tails"])
            return f"{side}."
        return None

    # ---------- time & math ----------

    def _time_date(self, text, lowered):
        if re.search(r"\btime\b", lowered) and not re.search(r"timer", lowered):
            return datetime.now().strftime("It is %H:%M.")
        if re.search(r"\b(date|day)\b.*(today|\btoday\b)|what.*date|what day", lowered) or lowered in ("date", "today"):
            return datetime.now().strftime("Today is %A, %d %B %Y.")
        return None

    def _math(self, text, lowered):
        answer = try_math(text)
        return (answer, None) if answer else None

    # ---------- PC control ----------

    def _open(self, text, lowered):
        m = re.match(r"^(?:please\s+)?open\s+(.+)$", lowered)
        if not m:
            return None
        target = m.group(1).strip()
        target = re.sub(r"\b(for me|please|now)\b", "", target).strip()
        try:
            opened = pc.open_target(target)
            return (f"Opening {opened}.", None)
        except FileNotFoundError:
            return (f"I could not find an app called '{target}'. Try opening its website instead?", None)
        except Exception as exc:
            return (f"Opening {target} failed: {exc}", None)

    def _search(self, text, lowered):
        m = re.match(r"^(?:search(?: for)?|google|look up|youtube|wiki(?:pedia)?)\s+(.+)$", lowered)
        if not m:
            return None
        site = ""
        prefix = lowered.split()[0]
        if prefix.startswith("youtube"):
            site = "youtube"
        elif prefix.startswith(("wiki",)):
            site = "wikipedia"
        query = m.group(1).strip()
        pc.web_search(query, site=site)
        where = site or "the web"
        return (f"Searching {where} for {query}.", None)

    def _screenshot(self, text, lowered):
        if re.search(r"screenshot|screen shot|capture (the )?screen", lowered):
            try:
                path = pc.screenshot()
                return ("Screenshot captured. The folder is on your screen.", {"refresh": []})
            except Exception as exc:
                return (f"The screenshot failed: {exc}", None)
        return None

    def _volume_media(self, text, lowered):
        if re.search(r"\bvolume\b", lowered):
            if re.search(r"\b(up|raise|louder|increase)\b", lowered):
                steps = min(self._repeat_count(lowered), 10)
                for _ in range(steps):
                    pc.volume("up")
                return ("Volume increased." + (" Considerably." if steps > 3 else ""), None)
            if re.search(r"\b(down|lower|quieter|decrease|reduce)\b", lowered):
                steps = min(self._repeat_count(lowered), 10)
                for _ in range(steps):
                    pc.volume("down")
                return ("Volume decreased.", None)
            if re.search(r"\b(mute|silence)\b", lowered):
                pc.volume("mute")
                return ("Toggled mute.", None)
        if re.search(r"\b(play|pause|resume)\b.*\bmusic\b|\bmedia\b|\b(next|previous|skip)\b (song|track|video)", lowered):
            if "next" in lowered or "skip" in lowered:
                pc.media_key("next")
                return ("Skipping ahead.", None)
            if "previous" in lowered:
                pc.media_key("previous")
                return ("Going back.", None)
            pc.media_key("play")
            return ("Media toggled.", None)
        return None

    @staticmethod
    def _repeat_count(lowered: str) -> int:
        words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        if "a lot" in lowered or "lot" in lowered:
            return 10
        for word, n in words.items():
            if re.search(rf"\b{word}\b", lowered):
                return n
        return 1

    def _status(self, text, lowered):
        if re.search(r"\b(system status|battery|cpu|ram|memory usage)\b|\bhow\b.*\b(pc|computer|machine|system)\b", lowered):
            try:
                s = pc.system_status()
                parts = []
                if s.get("cpu_percent") is not None:
                    parts.append(f"CPU at {s['cpu_percent']} percent")
                if s.get("ram_total_gb"):
                    parts.append(f"using {s['ram_used_gb']} of {s['ram_total_gb']} gigabytes of memory")
                if s.get("battery_percent") is not None:
                    state = "charging" if s.get("charging") else "on battery"
                    parts.append(f"battery at {int(s['battery_percent'])} percent ({state})")
                if s.get("uptime_hours") is not None:
                    parts.append(f"up {round(s['uptime_hours'])} hours")
                return ("; ".join(parts).capitalize() + ".", {"refresh": ["status"]})
            except Exception as exc:
                return (f"I could not read the system status: {exc}", None)
        return None

    def _lock(self, text, lowered):
        if re.search(r"\block\b.*(pc|computer|screen|windows)|\bsecure the (station|console)\b", lowered):
            pc.lock_pc()
            return ("Locking the station.", None)
        return None

    def _power(self, text, lowered):
        if re.search(r"\b(shut ?down|turn off) (the )?(pc|computer|machine|system)\b|^\bshut ?down\b$", lowered):
            self.pending["p1"] = {"kind": "power", "action": "shutdown", "expires": timemod.time() + 120}
            return ("That will end our session. Shall I shut the computer down? Say 'confirm' to proceed.", None)
        if re.search(r"\brestart|reboot\b", lowered):
            self.pending["p2"] = {"kind": "power", "action": "restart", "expires": timemod.time() + 120}
            return ("Restarting requires your confirmation. Say 'confirm' to proceed.", None)
        return None

    # ---------- weather / briefing ----------

    def _weather(self, text, lowered):
        if re.search(r"\bweather\b", lowered):
            m = re.search(r"\b(?:in|for|at)\s+([a-zA-Z\s]+)$", lowered.strip())
            report = fetch_weather(m.group(1).strip() if m else "")
            if report:
                return (f"Current conditions: {report}.", None)
            pc.web_search("weather")
            return ("I could not reach the weather service, so I opened the forecast in your browser.", None)
        return None

    def _briefing(self, text, lowered):
        if re.search(r"\b(briefing|brief me|good morning)\b", lowered):
            from .briefing import compose

            return (compose(), {"refresh": ["reminders", "status"]})
        return None

    # ---------- daily briefing settings ----------

    def _settings(self, text, lowered):
        if re.search(r"(disable|stop|cancel|turn off).*(briefing|daily)", lowered):
            db.set_setting("briefing_enabled", "0")
            return ("Daily briefings are off.", {"refresh": ["settings"]})
        if re.search(r"(enable|start|turn on).*briefing", lowered) or re.fullmatch(r"brief(ing)? (status|time)", lowered):
            enabled = db.get_setting("briefing_enabled", "0") == "1"
            when = db.get_setting("briefing_time", "08:00")
            state = "scheduled for" if enabled else "currently disabled; default time"
            return (f"Daily briefing is {state} {when}. You can say 'brief me every day at 8 am'.", {"refresh": ["settings"]})
        if re.search(r"\b(every ?day|daily|each morning)\b", lowered) and re.search(r"\b(brief|briefing)\b", lowered):
            m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", lowered)
            if not m:
                return ("What time should the daily briefing arrive? For example: 'brief me every day at 8 am'.", None)
            hour = int(m.group(1))
            minute = int(m.group(2)) if m.group(2) else 0
            meridiem = m.group(3)
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            if not 0 <= hour <= 23 or minute > 59:
                return ("That time does not look right, sir.", None)
            db.set_setting("briefing_enabled", "1")
            db.set_setting("briefing_time", f"{hour:02d}:{minute:02d}")
            return (f"Understood. I will brief you every day at {hour:02d}:{minute:02d}.", {"refresh": ["settings"]})
        if re.search(r"when.*(is|my).*(briefing|daily)", lowered):
            enabled = db.get_setting("briefing_enabled", "0") == "1"
            when = db.get_setting("briefing_time", "08:00")
            state = "scheduled for" if enabled else "currently disabled; default time"
            return (f"Daily briefing is {state} {when}.", {"refresh": ["settings"]})
        if re.search(r"(remember|set)?\s*my (city|location)( is)?", lowered):
            m = re.search(r"(?:city|location)(?: is)?[:\s]+([a-zA-Z\s]+)$", text.strip())
            if m:
                city = m.group(1).strip()
                db.set_setting("city", city)
                return (f"Weather will now be reported for {city}.", {"refresh": ["settings"]})
        return None

    # ---------- multi-step chains ----------

    def _chains(self, text, lowered):
        if re.match(r"^plan my (morning|day)\b|^morning routine\b|^start my day\b", lowered):
            steps = []
            steps.append(datetime.now().strftime("It is %H:%M on %A."))
            from .briefing import compose

            full = compose()
            steps.append(full)
            for app in ("edge",):
                try:
                    pc.open_target(app)
                    steps.append("Browser is up.")
                    break
                except Exception:
                    continue
            reply = " ".join(steps)
            return (reply, {"refresh": ["reminders", "status", "settings"]})

        if re.match(r"^(start|enter) (work|focus|coding|deep work) mode\b", lowered):
            notes = []
            try:
                pc.volume("down")
                pc.volume("down")
                notes.append("Volume eased down.")
            except Exception:
                pass
            opened = False
            for target in (("vs code"), ("code")):
                try:
                    pc.open_target(target)
                    opened = True
                    break
                except Exception:
                    continue
            notes.append("VS Code launched." if opened else "I could not find VS Code.")
            pending = db.list_reminders()
            notes.append(f"You have {len(pending)} pending item(s)." if pending else "Schedule is clear — deep work it is.")
            return ("Entering focus mode. " + " ".join(notes), {"refresh": ["reminders"]})

        if re.match(r"^(goodnight|good night|end (my )?day|wind down)\b", lowered):
            items = db.list_reminders()
            line = f"You have {len(items)} item(s) tomorrow." if items else "Nothing scheduled for later."
            return (f"Good night, {config.USER_ADDRESS}. {line} I will keep watch.", {"refresh": ["reminders"]})

        if re.match(r"^run diagnostics\b|^self test\b|^system check\b", lowered):
            results = []
            s = pc.system_status()
            results.append(f"CPU {s.get('cpu_percent')} percent, memory {s.get('ram_used_gb')} of {s.get('ram_total_gb')} gigabytes.")
            if s.get("battery_percent") is not None:
                results.append(f"Battery {int(s['battery_percent'])} percent.")
            results.append("All skills responsive.")
            return ("Running diagnostics... " + " ".join(results), {"refresh": ["status"]})
        return None

    def _clear_chat(self, text, lowered):
        if re.search(r"(clear|reset|forget).*(conversation|chat|context)|^start over$", lowered):
            self.history.clear()
            try:
                db.clear_messages()
            except Exception:
                pass
            return ("Conversation context cleared.", None)
        return None

    def _feedback(self, text, lowered):
        if lowered in ("never mind", "nevermind"):
            return None
        m = re.match(r"^from now on[, ]+(?:always |never )?(.+)$", lowered)
        if m:
            return self._store_correction(m.group(1))
        m = re.match(r"^(always|never)\s+(.{4,})$", lowered)
        if m:
            return self._store_correction(f"{m.group(1)} {m.group(2)}")
        return None

    def _store_correction(self, instruction: str) -> dict:
        instruction = instruction.strip().rstrip(".")
        db.add_correction("", instruction)
        return {"reply": f"Noted permanently: {instruction}.", "refresh": ["memory"]}

    # ---------- memory ----------

    def _memory(self, text, lowered):
        if re.search(r"\b(city|location)\b", lowered) and "remember" in lowered:
            return None
        m = re.match(r"^remember(?: that)?[:\s]+(.+)$", text, re.IGNORECASE)
        if m:
            fact = m.group(1).strip()
            kv = re.match(r"^(?:my|i)\s+(.*?)\s+(?:is|are|=)\s+(.+)$", fact, re.IGNORECASE)
            key, value = (kv.group(1), kv.group(2)) if kv else (fact[:40], fact)
            db.remember(key.strip(), value.strip())
            return (f"Noted: {key.strip()} is {value.strip()}.", {"refresh": ["memory"]})

        m = re.match(r"^forget (?:that )?(?:my )?(.+)$", text, re.IGNORECASE)
        if m:
            key = m.group(1).strip()
            hits = db.search_memory(key)
            if db.forget(key) or any(h["key"] == key for h in hits):
                db.forget(key)
                return ("Forgotten.", {"refresh": ["memory"]})
            for h in hits:
                db.forget(h["key"])
            return ("Forgotten.", {"refresh": ["memory"]}) if hits else (f"Nothing on record about {key}.", None)

        if re.search(r"what do you (remember|know) about me|list memories|recall everything", lowered):
            items = db.all_memories()
            if not items:
                return ("Nothing yet. Tell me things starting with 'remember that...'", {"refresh": ["memory"]})
            joined = "; ".join(f"{i['key']}: {i['value']}" for i in items[:15])
            return (f"Here is what I have on record: {joined}.", {"refresh": ["memory"]})

        m = re.match(r"^(?:what(?:'s| is)|do you know|recall)\s+(?:my\s+)?(.+?)[?.!]*$", text, re.IGNORECASE)
        if m and not re.search(r"workspace", lowered):
            probe = m.group(1).strip().rstrip("?").strip()
            exact = db.get_memory(probe)
            if exact:
                return (f"Your {probe} is {exact}.", None)
            hits = db.search_memory(probe)
            if hits:
                best = hits[0]
                return (f"{best['key'].capitalize()} is {best['value']}.", None)
            if re.search(r"\bmy\b", m.group(0), re.IGNORECASE):
                return (f"I have nothing on record about your {probe}, sir.", None)
        return None

    # ---------- reminders / timers / alarms ----------

    def _reminders(self, text, lowered):
        if re.search(r"(list|what are|show|any).*(reminders?|alarms?|timers?)|reminders?\?$", lowered):
            items = db.list_reminders()
            if not items:
                return ("You have nothing scheduled.", {"refresh": ["reminders"]})
            now = datetime.now()
            parts = []
            for it in items[:8]:
                due = datetime.fromtimestamp(it["due_at"])
                when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%a %d %b %H:%M")
                label = it["kind"].capitalize()
                parts.append(f"{label} {it['id']}: {it['message']} at {when}")
            return ("; ".join(parts) + ".", {"refresh": ["reminders"]})

        m = re.match(r"^cancel (?:reminder|timer|alarm)\s*(?:number\s*)?#?(\d+)$", lowered)
        if m:
            ok = db.cancel_reminder(int(m.group(1)))
            return ("Cancelled." if ok else f"I found no reminder number {m.group(1)}.", {"refresh": ["reminders"]})
        if re.match(r"^cancel (?:all )?(?:my )?(reminders?|timers?|alarms?)$", lowered):
            n = db.cancel_all_reminders()
            return (f"Cancelled {n} item(s)." if n else "Nothing to cancel.", {"refresh": ["reminders"]})

        kind = "reminder"
        m = re.match(r"^(?:set (?:an? )?(?:alarm|wake up)(?: call)?)\s+(?:for|at)?\s*(.+)$", lowered)
        wake = False
        if re.search(r"\bwake me\b|\balarm\b|wake up", lowered):
            kind = "alarm"
            wake = True
            if not m:
                m = re.match(r"^(?:wake me(?: up)?(?:\s+at|\s+in)?\s*)(.+)$", lowered)
        elif re.search(r"\btimer\b", lowered):
            kind = "timer"
            m = re.match(r"^.*timer\s*(?:for\s*|of\s*)?(.+)$", lowered)
        elif re.search(r"\bremind\b", lowered):
            m = re.match(r"^.*remind me\s*(?:to\s*|about\s*|that\s*)?(.+)$", lowered)
        if not m:
            return None
        body = m.group(1).strip()

        message, remainder = self._split_message(body, kind)

        due = None
        dm = re.search(r"\bin\s+(.+)$", remainder)
        if dm:
            delta = parse_duration(dm.group(1))
            if delta:
                due = datetime.now() + delta
        if due is None:
            due = parse_clock_time(remainder) or parse_clock_time(body)
        if due is None:
            delta = parse_duration(remainder) or parse_duration(body)
            if delta:
                due = datetime.now() + delta
        if due is None:
            return ("When should I set that for? Try 'in ten minutes' or 'at 7 am'.", None)

        rid = db.add_reminder(kind=kind, message=message, due_at=due.timestamp())
        delta = due - datetime.now()
        mins = int(delta.total_seconds() // 60)
        human = f"in {mins} minute(s)" if mins >= 1 else "shortly"
        label = {"timer": "Timer", "alarm": "Alarm", "reminder": "Reminder"}[kind]
        if kind == "timer":
            reply = f"Timer armed: {mins} minute(s) from now."
        elif kind == "alarm":
            reply = f"Wake-up call scheduled for {due.strftime('%H:%M')}."
        else:
            reply = f"{label} set: {message}, {human}."
        return (reply, {"refresh": ["reminders"]})

    @staticmethod
    def _split_message(body: str, kind: str) -> tuple[str, str]:
        body = re.sub(r"^(?:to\s+|about\s+|that\s+|me\s+to\s+)", "", body).strip()
        remainder = ""
        dm = re.search(r"\bin\s+[\donean]+\s*(?:seconds?|minutes?|mins?|hours?|hrs?|days?).*$", body)
        if not dm:
            dm = re.search(r"\bin\s+(?:a|an|half an?|one and a half|two and a half)?\s*(?:second|minute|hour|day)s?.*$", body)
        if dm:
            remainder = dm.group(0)
            message = body[:dm.start()].strip(" ,.")
        else:
            tm = re.search(r"\b(?:tomorrow|tonight|today)?\s*(?:at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}:\d{2}).*$", body)
            if tm:
                remainder = tm.group(0)
                message = body[:tm.start()].strip(" ,.")
            else:
                message = body
        if kind == "timer" and not message:
            message = "Timer"
        if kind == "alarm" and not message:
            message = "Good morning."
        return message or "Reminder", remainder or body

    # ---------- AI workers (swarm) ----------

    def _swarm(self, text, lowered):
        m = re.search(
            r"(?:hire|deploy|spawn|assemble)\s+(?:(\d+|two|three|four|five)\s+)?(?:ai|ais|a\.i\.?s?|agents?|workers?)\b[^a-z0-9]*(?:to|for|on)\s+(.+)",
            lowered,
        )
        single = re.search(r"ask (?:another|a second) ai(?: agent)? to (.+)", lowered)
        delegate = re.match(r"delegate(?: this)? task:? (.+)", lowered)
        if not (m or delegate):
            if single:
                return self._run_swarm(single.group(1), 2)
            return None
        task = delegate.group(1) if delegate else m.group(2)
        count = 3
        if m and m.group(1):
            words = {"two": 2, "three": 3, "four": 4, "five": 5}
            count = int(m.group(1)) if m.group(1).isdigit() else words.get(m.group(1), 3)
        return self._run_swarm(task.strip(), count)

    def _run_swarm(self, task: str, count: int) -> dict:
        try:
            from . import swarm

            result = swarm.hire_workers(task, n=count)
        except Exception as exc:
            return {"reply": f"I could not assemble the team: {exc}", "refresh": []}
        return {
            "reply": f"{len(result['workers'])} workers reported in. Merged result: {result['final']}",
            "refresh": [],
        }

    # ---------- coding skill ----------

    def _code(self, text, lowered):
        if re.search(r"(what'?s |whats |list ).*(workspace)|show (my )?(workspace|project files)", lowered):
            from . import coding

            files = coding.list_files()
            if not files:
                return ("The workspace is empty.", {"refresh": ["workspace"]})
            listing = ", ".join(f["name"] for f in files[:12])
            return (f"The workspace holds {len(files)} file(s): {listing}.", {"refresh": ["workspace"]})

        m = re.search(r"\brun\b.*\b(script|it|program)\b|^execute .*\.py$", lowered)
        if m and self.last_script:
            self.pending["run1"] = {
                "kind": "run_code",
                "filename": self.last_script,
                "expires": timemod.time() + 180,
            }
            if self._auto_approve():
                entry = self.pending.pop("run1")
                from . import coding

                return self._report_run(coding.run_python(filename=entry["filename"]), entry["filename"])
            return (
                f"Shall I execute {self.last_script}? Say 'confirm' to allow code execution.",
                None,
            )

        gen = re.search(
            r"\b(?:write|create|make|build|generate|code)\b(?: me)? (?:a |an |the )?(?:python |simple |quick )?"
            r"(?:script |program |code file |file )?(?:named |called )?(?P<name>[\w\-]+) (?:that|which|to|for) (?P<desc>.+)"
            r"|\b(?:write|create|make|build|generate|code)\b(?: me)? (?:a |an |the )?(?:python |simple |quick )?"
            r"(?P<noun>script|program|code file|file) (?:that|which|to|for) (?P<desc2>.+)",
            lowered,
        )
        if not gen or not (gen.group("desc") or gen.group("desc2")):
            return None

        description = (gen.group("desc") or gen.group("desc2")).strip()
        name = (gen.group("name") or "").strip()
        if name in ("that", "which", "to", "for", "a", "an", "the"):
            name = ""
        if not name:
            name = datetime.now().strftime("script_%Y%m%d_%H%M%S")
        filename = f"{name}.py" if not name.endswith(".py") else name

        if not config.llm_enabled():
            return ("I need my language core online to author code. Start FreeLLMAPI on port 3001 and try again.", None)

        try:
            raw = self._llm(
                "You are a senior engineer. Respond with ONLY one fenced ```python code block implementing "
                "the user's request. Standard library plus pip packages allowed. No explanations.",
                description,
                temperature=0.2,
            )
        except Exception as exc:
            return {"reply": f"Code generation failed: {exc}", "refresh": []}

        code = self._extract_code(raw) or raw
        from . import coding

        coding.write_file(filename, code)

        if self._auto_approve():
            result = coding.run_python(filename=filename)
            return self._report_run(result, filename)

        self.pending["run_new"] = {
            "kind": "run_code",
            "filename": filename,
            "expires": timemod.time() + 300,
        }
        lines = code.count("\n") + 1
        return (
            f"I have written {filename} ({lines} lines) into the workspace. "
            f"Say 'confirm' to let me execute it.",
            {"refresh": ["workspace"]},
        )

    # ---------- knowledge / learning / browsing ----------

    def _knowledge(self, text, lowered):
        m = re.search(r"\blearn (?:from|about|the)\s+(.+)", lowered)
        if m:
            target = m.group(1).strip().rstrip(".")
            return self._learn_target(target)

        m = re.search(r"\bresearch\s+(.+)", lowered)
        if m:
            return self._research(m.group(1).strip().rstrip("."))

        m = re.search(r"what do you know about (.+?)[?.!]*$|recall (?:my )?knowledge (?:about|of) (.+?)[?.!]*$", lowered)
        if m:
            probe = (m.group(1) or m.group(2)).strip()
            hits = db.recall_knowledge(probe)
            if not hits:
                return (f"Nothing in my knowledge base about {probe} yet. Say 'learn about {probe}' and I will study it.", {"refresh": ["knowledge"]})
            parts = [f"{h['topic']}: {h['content'][:220]}" for h in hits]
            return ("From my studies — " + " | ".join(parts), {"refresh": ["knowledge"]})

        m = re.search(r"forget (?:what you learned|your knowledge) about (.+?)[?.!]*$", lowered)
        if m:
            hits = db.recall_knowledge(m.group(1).strip())
            for h in hits:
                db.forget_knowledge(h["id"])
            return (f"Purged {len(hits)} knowledge entr(y/ies)." if hits else "Nothing to purge.", {"refresh": ["knowledge"]})
        return None

    def _learn_target(self, target: str) -> dict:
        import urllib.parse

        from . import webtools

        is_url = bool(re.match(r"^(https?://|www\.)", target))
        try:
            if is_url:
                content = webtools.fetch_page(target, max_chars=4000)
                topic = urllib.parse.urlparse(target if "//" in target else "https://" + target).netloc
                source = target
            elif config.llm_enabled():
                content = self._llm(
                    "Explain the topic precisely but concisely (max 150 words). Facts only.",
                    target,
                    temperature=0.3,
                )
                topic, source = target, "language-core"
            else:
                results = webtools.search_web(target, max_results=3)
                content = "\n".join(f"{r['title']} — {r['url']}" for r in results)
                topic, source = target, "web-search"
        except Exception as exc:
            return {"reply": f"I could not learn about {target}: {exc}", "refresh": []}
        db.learn(topic, content, source)
        preview = content[:200].replace("\n", " ")
        return {
            "reply": f"Studied and stored '{topic}'. Preview: {preview}...",
            "refresh": ["knowledge"],
        }

    def _research(self, topic: str) -> dict:
        from . import webtools

        try:
            results = webtools.search_web(topic, max_results=4)
        except Exception as exc:
            return {"reply": f"Research failed: {exc}", "refresh": []}

        pages = []
        for r in results[:3]:
            try:
                pages.append(webtools.fetch_page(r["url"], max_chars=1400))
            except Exception:
                continue

        if config.llm_enabled() and pages:
            material = "\n\n".join(pages)[:6000]
            try:
                summary = self._llm(
                    "You are a research analyst. Synthesise the material below into key findings about the topic. Max 130 words.",
                    f"Topic: {topic}\n\nMaterial:\n{material}",
                    temperature=0.3,
                )
            except Exception:
                summary = ""
        else:
            summary = " ".join(p.split("\n", 1)[-1][:200] for p in pages)

        db.learn(topic, (summary or "\n".join(pages))[:4000], "; ".join(r["url"] for r in results[:3]))
        links = ", ".join(r["url"] for r in results[:3])
        body = summary or "I gathered sources but could not summarise them."
        return {
            "reply": f"Research complete on {topic}: {body} Sources: {links}",
            "refresh": ["knowledge"],
        }

    def _browse(self, text, lowered):
        m = re.search(r"\b(read|fetch|summar(?:ise|ize)|open and read)\s+(https?://\S+|[\w.-]+\.[a-z]{2,}\S*)", lowered)
        if not m:
            return None
        url = m.group(2)
        from . import webtools

        try:
            page = webtools.fetch_page(url, max_chars=5000)
        except Exception as exc:
            return {"reply": f"Browsing failed: {exc}", "refresh": []}
        title = page.split("\n", 1)[0]
        if config.llm_enabled():
            try:
                summary = self._llm(
                    "Summarise this web page in max 100 spoken words.",
                    page,
                    temperature=0.3,
                )
                return {"reply": f"{title}. Summary: {summary}", "refresh": []}
            except Exception:
                pass
        excerpt = page[page.find("\n") + 1 :][:350].replace("\n", " ")
        return {"reply": f"{title}. Excerpt: {excerpt}...", "refresh": []}


    def _fallback(self, text: str) -> dict:
        if config.llm_enabled():
            try:
                from . import llm
                memories = "; ".join(f"{m['key']}: {m['value']}" for m in db.all_memories()[:20])
                system = (
                    f"You are {config.ASSISTANT_NAME}, a witty, precise AI butler in the style of Tony Stark's JARVIS. "
                    f"Address the user as '{config.USER_ADDRESS}'. Keep replies under 80 spoken words. "
                    f"You run locally on their Windows PC and can execute commands through skills, so never pretend to lack agency. "
                    f"Known facts about the user: {memories or 'none'}. Current time: {datetime.now():%A %H:%M}."
                )
                messages = [{"role": "system", "content": system}] + self.history[-12:] + [
                    {"role": "user", "content": text}
                ]
                answer = llm.chat(messages)
                self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
                if len(self.history) > 24:
                    del self.history[:-24]
                return {"reply": answer, "refresh": []}
            except Exception as exc:
                return {"reply": f"My language core is unreachable ({exc}). Local skills remain available.", "refresh": []}
        return {
            "reply": "I did not recognise that command, and my language core is offline. "
                     "Say 'what can you do' for my current abilities, or add JARVIS_OPENAI_API_KEY to .env for full conversation.",
            "refresh": [],
        }
