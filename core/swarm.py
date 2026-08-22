import re
from concurrent.futures import ThreadPoolExecutor

from . import config
from .llm import chat


class LLMOfflineError(RuntimeError):
    pass


def _require_llm() -> None:
    if not config.llm_enabled():
        raise LLMOfflineError("Language core is offline")


WORKER_SYSTEM = (
    "You are worker agent {i} of {n} in a specialist AI team. "
    "Complete the given task independently and concisely. Provide concrete results — code, facts, plans or answers — not meta commentary."
)


def hire_workers(task: str, n: int = 3, timeout: int = 90) -> dict:
    _require_llm()
    n = max(2, min(int(n), 6))

    def work(i: int) -> str:
        messages = [
            {"role": "system", "content": WORKER_SYSTEM.format(i=i + 1, n=n)},
            {"role": "user", "content": task},
        ]
        try:
            return chat(messages, temperature=0.4 + i * 0.15)
        except Exception as exc:
            return f"Worker {i + 1} failed: {exc}"

    with ThreadPoolExecutor(max_workers=n) as pool:
        outputs = list(pool.map(work, range(n)))

    merge_messages = [
        {
            "role": "system",
            "content": (
                f"{n} AI workers independently attempted the same task. Merge their work into one best answer. "
                "Keep it under 150 spoken words. If they disagree on facts, prefer the majority and note the conflict briefly."
            ),
        },
        {"role": "user", "content": "\n\n".join(f"--- Worker {i+1} ---\n{o}" for i, o in enumerate(outputs))},
    ]
    try:
        final = chat(merge_messages, temperature=0.3)
    except Exception as exc:
        final = f"Workers finished but synthesis failed: {exc}"
    return {"final": final, "workers": outputs}
