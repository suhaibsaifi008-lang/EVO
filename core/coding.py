import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

WORKSPACE = DATA_DIR / "workspace"
WORKSPACE.mkdir(parents=True, exist_ok=True)


def safe_path(rel: str) -> Path:
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("File name required")
    candidate = (WORKSPACE / rel).resolve()
    if WORKSPACE.resolve() not in candidate.parents and candidate != WORKSPACE.resolve():
        raise PermissionError("Path escapes the workspace")
    return candidate


def write_file(rel: str, content: str) -> Path:
    path = safe_path(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read_file(rel: str) -> str:
    return safe_path(rel).read_text(encoding="utf-8", errors="replace")


def delete_file(rel: str) -> bool:
    path = safe_path(rel)
    if path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        os.rmdir(path)
        return True
    return False


def list_files() -> list[dict]:
    out = []
    for path in sorted(WORKSPACE.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out.append(
                {
                    "name": str(path.relative_to(WORKSPACE)).replace("\\", "/"),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                }
            )
            if len(out) >= 100:
                break
    return out


class _Job:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.timed_out = False

    def kill(self) -> None:
        self.timed_out = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def run_python(code: str = "", filename: str | None = None, timeout: int = 25) -> dict:
    """Execute python code in the sandbox. Returns stdout/stderr/exit code."""
    if filename:
        target = safe_path(filename)
        if target.suffix != ".py":
            raise ValueError("Only .py files can be executed")
    else:
        stamp = datetime.now().strftime("%H%M%S")
        target = write_file(f"_scratch_{stamp}.py", code)

    job = _Job()
    timer = threading.Timer(timeout, job.kill)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(target)],
            cwd=str(WORKSPACE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        job.proc = proc
        timer.start()
        stdout, stderr = proc.communicate()
    finally:
        timer.cancel()

    return {
        "file": str(target.relative_to(WORKSPACE)).replace("\\", "/"),
        "exit": proc.returncode,
        "stdout": (stdout or "")[-3000:],
        "stderr": (stderr or "")[-2000:],
        "timed_out": proc.returncode is not None and proc.returncode < 0,
    }
