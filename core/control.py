"""Global STOP switch — halts missions, TTS and non-critical announcements."""
import threading

from . import db

HALT = threading.Event()


def is_halted() -> bool:
    return HALT.is_set()


def halt(address: str = "sir") -> dict:
    HALT.set()
    stopped = 0
    try:
        from .projects import manager

        for row in db.list_projects(50):
            if row["status"] == "running":
                manager.stop(row["id"])
                stopped += 1
    except Exception:
        pass
    return {
        "text": (
            f"All operations halted, {address}. {stopped} mission(s) paused — "
            "they can be resumed whenever you're ready."
            if stopped
            else f"Standing down, {address}. Everything is quiet."
        ),
        "missions_stopped": stopped,
    }


def resume_next_command() -> None:
    """Called automatically: the next normal command lifts the halt."""
    HALT.clear()
