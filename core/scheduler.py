import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, List

from . import db


class Dispatcher(threading.Thread):
    """Background thread that fires due reminders as announcements."""

    def __init__(self, poll_seconds: float = 2.0) -> None:
        super().__init__(daemon=True, name="evo-dispatcher")
        self.poll_seconds = poll_seconds
        self._subscribers: List[Deque[Dict]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.on_fire: List[Callable[[Dict], None]] = []
        self.backlog: Deque[Dict] = deque(maxlen=40)
        self._last_idle = 0.0
        self._last_welcome = 0.0

    def subscribe(self) -> Deque[Dict]:
        """Register a live subscriber. Old backlog events are deliberately NOT
        replayed: the console re-subscribes on every poll, so seeding queues
        with history made announcements repeat forever."""
        q: Deque[Dict] = deque(maxlen=50)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: Deque[Dict]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: Dict) -> None:
        event.setdefault("ts", time.time())
        with self._lock:
            self.backlog.append(dict(event))
        for cb in list(self.on_fire):
            try:
                cb(event)
            except Exception:
                pass
        with self._lock:
            subs = list(self._subscribers)

        def _push() -> None:
            for q in subs:
                q.append(event)

        _push()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_briefing()
            except Exception:
                pass
            try:
                self._welcome_check()
            except Exception:
                pass
            try:
                fired = db.due_reminders(time.time())
                for item in fired:
                    kind = item.get("kind") or "reminder"
                    message = item.get("message") or ""
                    if kind == "timer":
                        text = f"Your timer has finished."
                    elif kind == "alarm":
                        text = f"It is time. {message}".strip()
                    elif message:
                        text = f"Reminder: {message}"
                    else:
                        text = f"You have a {kind}."
                    self.publish({"type": "reminder_due", "kind": kind, "id": item["id"], "text": text})
            except Exception:
                pass
            self._stop.wait(self.poll_seconds)

    def _check_briefing(self) -> None:
        if db.get_setting("briefing_enabled", "0") != "1":
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if db.get_setting("last_brief", "") == today:
            return
        target = db.get_setting("briefing_time", "08:00")
        try:
            hh, mm = (int(x) for x in target.split(":"))
        except ValueError:
            return
        if (now.hour, now.minute) < (hh, mm):
            return
        from .briefing import compose

        db.set_setting("last_brief", today)
        self.publish({"type": "briefing", "kind": "briefing", "text": compose()})

    def _read_idle_seconds(self) -> float:
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return 0.0
            return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
        except Exception:
            return 0.0

    def _welcome_check(self) -> None:
        idle = self._read_idle_seconds()
        self._welcome_transition(idle)

    def _welcome_transition(self, idle: float) -> None:
        now = time.time()
        was_away = self._last_idle > 1500
        is_back = idle < 120
        if was_away and is_back and now - self._last_welcome > 4 * 3600:
            self._last_welcome = now
            message = "Welcome back, sir."
            try:
                from .perception import active_window

                message += f" I see {active_window()} on screen."
            except Exception:
                pass
            summary = db.get_setting("last_screen_summary", "")
            if summary and "|" in summary:
                ts_raw, text = summary.split("|", 1)
                try:
                    if time.time() - float(ts_raw) < 3600:
                        message += f" Last I looked, your screen showed: {text.strip()}"
                except ValueError:
                    pass
            self.publish({"type": "welcome", "kind": "welcome", "text": message})
        self._last_idle = idle

    def stop(self) -> None:
        self._stop.set()


dispatcher = Dispatcher()
