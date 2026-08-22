"""Persistent World State — a live picture of what is happening on the machine."""
import threading
import time

from . import appctl, db, pc

_lock = threading.Lock()
_state: dict = {"updated": 0.0}
_thread: threading.Thread | None = None


def snapshot(max_age: float = 6.0, force: bool = False) -> dict:
    with _lock:
        if not force and time.time() - float(_state.get("updated", 0)) < max_age:
            return dict(_state)

    state: dict = {"updated": round(time.time(), 2)}
    try:
        from .perception import active_window

        state["active_window"] = active_window()
    except Exception:
        state["active_window"] = "unknown"
    try:
        state["visible_windows"] = appctl.running_apps(12)
    except Exception:
        state["visible_windows"] = []
    try:
        from . import browser_control

        state["browser"] = browser_control.status()
    except Exception:
        state["browser"] = {"open": False}
    try:
        state["missions_running"] = len(
            [p for p in db.list_projects() if p["status"] == "running"]
        )
    except Exception:
        state["missions_running"] = 0
    try:
        s = pc.system_status()
        state["cpu_percent"] = s.get("cpu_percent")
        state["battery_percent"] = s.get("battery_percent")
    except Exception:
        pass

    _state.clear()
    _state.update(state)
    return dict(_state)


def context_line() -> str:
    s = snapshot()
    bits = [f"active window: {s.get('active_window', 'unknown')}"]
    b = s.get("browser") or {}
    if b.get("open"):
        bits.append(f"browser on: {b.get('url', '')[:90]}")
    if s.get("missions_running"):
        bits.append(f"{s['missions_running']} mission(s) running")
    return "; ".join(bits)


def start_updater(interval_s: float = 5.0) -> None:
    global _thread

    def loop():
        while True:
            try:
                snapshot(force=True)
            except Exception:
                pass
            time.sleep(interval_s)

    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=loop, daemon=True, name="evo-world-state")
        _thread.start()
