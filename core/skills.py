import ast
import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from .config import DATA_DIR

SKILLS_DIR = DATA_DIR / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

RUN_TIMEOUT = 60

_CONTRACT = (
    "Skill script contract: arguments arrive as ONE JSON string in sys.argv[1]. "
    "Do the work, print the result to stdout (plain text). Exit non-zero on failure."
)

_registry_lock = threading.Lock()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower()).strip("_")
    if not cleaned or not re.match(r"^[a-z_]", cleaned):
        raise ValueError("skill name must start with a letter and use letters/digits/underscores")
    if len(cleaned) > 40:
        raise ValueError("skill name too long")
    return cleaned


def _paths(name: str) -> tuple[Path, Path]:
    base = SKILLS_DIR / name
    return base.with_suffix(".py"), base.with_suffix(".json")


def _script_path(name: str) -> Path:
    return _paths(name)[0]


def validate_code(code: str) -> None:
    try:
        ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"syntax error in skill code: {exc}") from exc


def test_skill(path: Path, example_args: dict | None) -> str:
    payload = json.dumps(example_args or {})
    proc = subprocess.run(
        [sys.executable, "-I", str(path), payload],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=RUN_TIMEOUT,
        cwd=str(SKILLS_DIR),
    )
    if proc.returncode != 0:
        return f"TEST FAILED (exit {proc.returncode}): {(proc.stderr or '')[-500:]}"
    return f"Test run OK. Output: {(proc.stdout or '(empty)').strip()[:400]}"


def save_skill(name: str, description: str, code: str, args_schema: dict | None, example_args: dict | None) -> str:
    clean = _safe_name(name)
    validate_code(code)
    py_path, json_path = _paths(clean)
    header = f'"""{description.strip()[:280]}\n\n{_CONTRACT}"""\n'
    py_path.write_text(header + code.lstrip("\n"), encoding="utf-8")
    json_path.write_text(
        json.dumps({"description": description.strip()[:300], "args": args_schema or {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    report = f"Skill '{clean}' saved and registered."
    if example_args is not None:
        outcome = test_skill(py_path, example_args)
        report += f" {outcome}"
        if outcome.startswith("TEST FAILED"):
            return report + " The skill is saved but may need fixes — inspect with read_file and update it."
    register(clean)
    return report


def delete_skill(name: str) -> bool:
    clean = _safe_name(name)
    py_path, json_path = _paths(clean)
    existed = False
    for p in (py_path, json_path):
        if p.exists():
            p.unlink()
            existed = True
    with _registry_lock:
        from . import tools

        tools._REGISTRY.pop(f"skill_{clean}", None)
    return existed


def load_meta(name: str) -> dict:
    _, json_path = _paths(name)
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {"description": name, "args": {}}


def list_skills() -> list[dict]:
    out = []
    for py_path in sorted(SKILLS_DIR.glob("*.py")):
        meta = load_meta(py_path.stem)
        out.append({"name": py_path.stem, "description": meta.get("description", ""), "args": meta.get("args", {})})
    return out


def _make_invoker(name: str) -> Callable[..., str]:
    path = _script_path(name)

    def invoke(**kwargs) -> str:
        payload = json.dumps(kwargs, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, "-I", str(path), payload],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
            cwd=str(SKILLS_DIR),
        )
        if proc.returncode != 0:
            return f"SKILL '{name}' FAILED (exit {proc.returncode}): {(proc.stderr or '')[-600:]}"
        return (proc.stdout or "(no output)").strip()[:2500]

    invoke.__name__ = f"invoke_{name}"
    return invoke


def register(name: str) -> bool:
    py_path, json_path = _paths(name)
    if not py_path.exists():
        return False
    meta = load_meta(name)
    from . import tools

    tool_obj = tools.Tool(
        name=f"skill_{name}",
        description=f"[learned skill] {meta.get('description', name)}",
        args={k: {"type": str(v) if isinstance(v, str) else "string"} for k, v in (meta.get("args") or {}).items()},
        fn=_make_invoker(name),
    )
    with _registry_lock:
        tools._REGISTRY[tool_obj.name] = tool_obj
    return True


def register_all() -> int:
    count = 0
    for py_path in SKILLS_DIR.glob("*.py"):
        try:
            if register(py_path.stem):
                count += 1
        except Exception:
            continue
    return count
