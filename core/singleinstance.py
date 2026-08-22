"""Single-instance guard for EVO background processes (Windows).

Newest-wins policy: a freshly started tray/ear REPLACES any older live
instance. This guarantees the running process always executes the current
code on disk - a stale ear can never outvote an update.
"""
import json
import os
import subprocess
import time

from .config import DATA_DIR

_ERROR_ALREADY_EXISTS = 183


def _pid_alive(pid: int) -> bool:
    import ctypes

    try:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:
        return True


def _kill(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", str(int(pid))],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    except Exception:
        pass


def hold_single_instance(lock_name: str, mutex_name: str, max_rounds: int = 6) -> bool:
    """Become THE instance. Returns False only if taking over keeps failing."""
    import ctypes

    k32 = ctypes.windll.kernel32
    for _ in range(max_rounds):
        k32.CreateMutexW(None, False, mutex_name)
        if k32.GetLastError() != _ERROR_ALREADY_EXISTS:
            try:
                (DATA_DIR / lock_name).write_text(
                    json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8"
                )
            except Exception:
                pass
            return True
        # A live instance holds the mutex. Replace it.
        pid = 0
        try:
            raw = (DATA_DIR / lock_name)
            if raw.exists():
                pid = int(json.loads(raw.read_text() or "{}").get("pid", 0))
        except Exception:
            pid = 0
        if pid and pid != os.getpid():
            _kill(pid)
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.3)
        else:
            # Lock file lost/stale but mutex still held by an orphaned handle;
            # brief grace then retry - the holder usually exits on its own.
            time.sleep(1.0)
    return False
