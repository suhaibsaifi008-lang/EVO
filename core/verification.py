"""Grounded verification — prove that actions actually happened."""
import os
import time


def wait_until(fn, timeout_s: float = 8.0, interval: float = 0.4):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return None


def verify_app_window(name_substring: str, timeout_s: float = 8.0) -> dict:
    """Postcondition for 'open app': a matching visible window must appear."""
    from . import appctl

    hit = wait_until(lambda: appctl.wait_for_window(name_substring, 0), timeout_s)
    if hit:
        return {"verified": True, "window": hit[1]}
    return {"verified": False, "reason": f"no window containing '{name_substring}' appeared within {timeout_s}s"}


def verify_file_exists(path: str, timeout_s: float = 5.0) -> bool:
    return wait_until(lambda: os.path.exists(path) and os.path.getsize(path) > 0, timeout_s) is not None


def verify_process_gone(image_name: str, timeout_s: float = 5.0) -> bool:
    import subprocess

    def check():
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.lower()
        return image_name.lower() not in out

    return wait_until(check, timeout_s) is True
