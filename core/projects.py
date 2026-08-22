import json
import threading
from datetime import datetime

from . import config, db, tools
from .scheduler import dispatcher

DEFAULT_MAX_STEPS = 40

ALLOWED_TOOLS = {
    "web_search", "read_page", "save_code", "run_code", "read_file",
    "list_files", "delete_file", "learn_topic", "recall_knowledge",
    "system_status", "current_datetime", "remember_fact",
}

PROJECT_SYSTEM = (
    "You are {name}, an autonomous project engine on the user's Windows PC. Address the user as '{address}'.\n"
    "PROJECT GOAL: {goal}\n"
    "Work step-by-step. Each turn reply with ONLY one JSON object:\n"
    '{{"action": {{"tool": "<tool>", "args": {{...}}}}}} to perform a step\n'
    '{{"finish": "<final summary for the user>"}} when the goal is met or impossible.\n'
    "Allowed tools this session: {tools_list}.\n"
    "Be efficient: at most {max_steps} steps total for this session. Verify your work (run code you wrote if permitted). "
    "If you are stopped early your progress is saved and can be resumed later. "
    "The finish summary should be spoken-word friendly and under 120 words."
)


def _log(pid: int, entry: str) -> None:
    db.project_log_append(pid, entry)
    try:
        dispatcher.publish({"type": "project_log", "kind": "project_log", "id": pid, "text": entry[:200]})
    except Exception:
        pass


def _push_quietly(title: str, body: str) -> None:
    try:
        from . import notify

        notify.push(title, body[:400])
    except Exception:
        pass


class ProjectManager:
    def __init__(self) -> None:
        self._threads: dict[int, threading.Thread] = {}

    def start(self, goal: str, max_steps: int | None = None) -> int:
        budget = max(1, min(int(max_steps or DEFAULT_MAX_STEPS), 200))
        pid = db.create_project(goal, max_steps=budget)
        t = self._spawn(pid, goal, [], budget)
        return pid

    def _spawn(self, pid: int, goal: str, transcript: list[dict], budget: int) -> threading.Thread:
        t = threading.Thread(target=self._worker, args=(pid, goal, transcript, budget), daemon=True,
                             name=f"evo-project-{pid}")
        self._threads[pid] = t
        t.start()
        return t

    def stop(self, pid: int) -> bool:
        row = db.get_project(pid)
        if not row or row["status"] != "running":
            return False
        db.finish_project(pid, "stopped", "Paused by user. Say 'resume project <id>' to continue from where it stopped.", keep_state=True)
        return True

    def resume(self, pid: int) -> str:
        row = db.get_project(pid)
        if not row:
            return f"No project #{pid}."
        t = self._threads.get(pid)
        alive = t is not None and t.is_alive()
        if row["status"] == "running" and alive:
            return f"Project #{pid} is already running."
        try:
            transcript = json.loads(row.get("state") or "[]")
        except Exception:
            transcript = []
        if not isinstance(transcript, list):
            transcript = []
        if row["status"] == "running" and transcript and not alive:
            _log(pid, "Detected a dead worker from an interrupted run — resuming.")
        if not transcript:
            if row["status"] == "running":
                db.finish_project(pid, "failed", "Interrupted before any progress was saved.")
            return f"Project #{pid} has no saved progress to resume — start a new one instead."
        db.set_project_running(pid)
        budget = int(row.get("max_steps") or DEFAULT_MAX_STEPS)
        # Each resume is a fresh session: guarantee real headroom even when the
        # previous session already consumed its whole budget.
        used = len([m for m in transcript if m.get("role") == "assistant"])
        if used >= budget:
            budget = used + DEFAULT_MAX_STEPS
        self._spawn(pid, row["goal"], transcript, budget)
        return f"resumed: Project #{pid} resumed with a fresh {budget}-step budget."

    def _worker(self, pid: int, goal: str, transcript: list[dict], budget: int) -> None:
        from .agent_loop import parse_json_object
        from .llm import chat

        try:
            self._worker_inner(pid, goal, transcript, budget, parse_json_object, chat)
        finally:
            self._threads.pop(pid, None)

    def _worker_inner(self, pid: int, goal: str, transcript: list[dict], budget: int,
                      parse_json_object, chat) -> None:
        system = PROJECT_SYSTEM.format(
            name=config.ASSISTANT_NAME,
            address=config.USER_ADDRESS,
            goal=goal,
            tools_list=", ".join(sorted(ALLOWED_TOOLS)),
            max_steps=budget,
        )
        steps_used = len([m for m in transcript if m.get("role") == "assistant"])
        if steps_used:
            _log(pid, f"Resumed at step {steps_used}/{budget}.")
        else:
            _log(pid, f"Accepted goal: {goal}")

        try:
            while steps_used < budget:
                if db.project_status(pid) != "running":
                    return
                try:
                    from .control import is_halted

                    if is_halted():
                        db.save_project_state(pid, json.dumps(transcript[-16:], ensure_ascii=False))
                        db.finish_project(
                            pid, "paused",
                            f"Paused by global STOP with progress saved at step {steps_used}.",
                            keep_state=True,
                        )
                        _log(pid, "Halted by global STOP.")
                        return
                except Exception:
                    pass
                raw = chat(
                    [{"role": "system", "content": system}] + transcript[-12:]
                    + [{"role": "user", "content": "Proceed with the next step, or finish."}],
                    temperature=0.4,
                )
                data = parse_json_object(raw)
                if data is None:
                    _log(pid, "Unparseable response; retrying.")
                    continue
                if "finish" in data:
                    summary = str(data["finish"]).strip()[:1200]
                    db.finish_project(pid, "done", summary)
                    _log(pid, f"Finished: {summary}")
                    dispatcher.publish({"type": "project_done", "kind": "project_done", "id": pid, "text": summary})
                    _push_quietly(f"Project #{pid} finished", summary)
                    return
                action = data.get("action") if isinstance(data.get("action"), dict) else {}
                tool_name = str(action.get("tool", "")).strip()
                args = action.get("args") if isinstance(action.get("args"), dict) else {}
                if tool_name not in ALLOWED_TOOLS:
                    observation = f"DENIED: '{tool_name}' is not allowed in projects."
                else:
                    observation = tools.call(tool_name, args)
                steps_used += 1
                _log(pid, f"[{steps_used}/{budget}] [{tool_name}] {observation[:150]}")
                transcript.append({"role": "assistant", "content": raw})
                transcript.append({"role": "user", "content": f"TOOL RESULT ({tool_name}):\n{observation[:2500]}"})
                db.save_project_state(pid, json.dumps(transcript[-16:], ensure_ascii=False))

            partial = "Partial results and full log are saved."
            if transcript:
                db.save_project_state(pid, json.dumps(transcript[-16:], ensure_ascii=False))
                db.finish_project(pid, "paused", f"I used my {budget}-step budget before finishing. {partial}", keep_state=True)
                dispatcher.publish({
                    "type": "project_done",
                    "kind": "project_done",
                    "id": pid,
                    "text": f"Project hit its step budget — paused with progress saved. You can resume it.",
                })
            else:
                db.finish_project(pid, "failed", "No work could be completed.")
        except Exception as exc:
            db.save_project_state(pid, json.dumps(transcript[-16:], ensure_ascii=False))
            db.finish_project(pid, "failed", f"{exc} Progress was checkpointed and can be resumed.", keep_state=True)
            dispatcher.publish({"type": "project_done", "kind": "project_done", "id": pid,
                                "text": f"A project failed: {exc}. Its progress is saved."})


manager = ProjectManager()


def resume_all_at_boot() -> int:
    """Auto-resume missions that were running when the server died."""
    resumed = 0
    try:
        for row in db.list_projects(50):
            if row["status"] == "running":
                msg = manager.resume(row["id"])
                if msg.startswith("resumed"):
                    _log(row["id"], "Server restarted — mission auto-resumed.")
                    dispatcher.publish({
                        "type": "project_done",
                        "kind": "project_done",
                        "id": row["id"],
                        "text": f"Mission #{row['id']} was interrupted by a restart and has been auto-resumed.",
                    })
                    resumed += 1
    except Exception:
        pass
    return resumed
