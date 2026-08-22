import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Deque

import sys

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi import HTTPException
from pydantic import BaseModel

from core import config, db, pc
from core.brain import Brain
from core.config import ROOT
from core.scheduler import Dispatcher, dispatcher

def _static_dir() -> Path:
    candidate = Path(ROOT) / "static"
    if (candidate / "index.html").exists():
        return candidate
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and (Path(meipass) / "static" / "index.html").exists():
        return Path(meipass) / "static"
    return candidate


STATIC_DIR = _static_dir()
brain = Brain()


def _ambient_vision_loop() -> None:
    import time as _time

    while True:
        try:
            interval = int(float(db.get_setting("ambient_vision_min", "0") or 0))
        except ValueError:
            interval = 0
        if interval <= 0:
            _time.sleep(60)
            continue
        try:
            from core.llm import chat_vision
            from core.perception import screen_image_b64

            image = screen_image_b64(max_width=800)
            summary = chat_vision(
                "Describe the user's screen in one short sentence (max 25 words).",
                image,
                temperature=0.2,
            )
            db.set_setting("last_screen_summary", f"{_time.time()}|{summary[:300]}")
        except Exception:
            pass
        _time.sleep(max(interval, 3) * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[evo] host platform={sys.platform} starting on {config.HOST}:{config.PORT}", flush=True)
    db.init_db()
    try:
        from core import skills

        skills.register_all()
    except Exception:
        pass
    dispatcher.start()
    try:
        from core import world_state

        world_state.start_updater(5.0)
    except Exception:
        pass
    try:
        from core import watchers

        watchers.engine.start()
    except Exception:
        pass
    try:
        from core.projects import resume_all_at_boot

        resume_all_at_boot()
    except Exception:
        pass
    try:
        from core import telegram_link

        telegram_link.start(lambda text: brain.respond(text))
    except Exception:
        pass
    import threading as _threading

    _threading.Thread(target=_ambient_vision_loop, daemon=True, name="evo-ambient-vision").start()

    def _prewarm_stt() -> None:
        try:
            from core import stt

            stt.prewarm()
        except Exception:
            pass

    _threading.Thread(target=_prewarm_stt, daemon=True, name="evo-stt-prewarm").start()
    yield
    try:
        from core import watchers

        watchers.engine.stop()
    except Exception:
        pass
    try:
        from core import telegram_link

        telegram_link.stop()
    except Exception:
        pass
    dispatcher.stop()


app = FastAPI(title="EVO", lifespan=lifespan)


class PinGate:
    """Optional shared-PIN gate for LAN access."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and config.ACCESS_PIN and scope["path"].startswith("/api/"):
            headers = dict(scope.get("headers") or [])
            provided = (headers.get(b"x-evo-pin") or b"").decode()
            if not provided:
                query = scope.get("query_string", b"").decode()
                for part in query.split("&"):
                    if part.startswith("pin="):
                        provided = part[4:]
                        break
            if provided != config.ACCESS_PIN:
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": b'{"error":"pin required"}'})
                return
        await self.app(scope, receive, send)


if config.ACCESS_PIN:
    app.add_middleware(PinGate)

from fastapi.staticfiles import StaticFiles  # noqa: E402

app.mount("/static", StaticFiles(directory=str(_static_dir())), name="static")


class ChatIn(BaseModel):
    text: str


class MemoryIn(BaseModel):
    key: str
    value: str


class ReminderIn(BaseModel):
    message: str
    due_at: float
    kind: str = "reminder"


@app.get("/")
def index() -> Response:
    return Response(
        content=(STATIC_DIR / "index.html").read_bytes(),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/chat")
def chat(body: ChatIn) -> dict:
    return brain.respond(body.text)


@app.post("/api/transcribe")
async def transcribe(request: Request) -> dict:
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="no audio received")
    from core import stt

    try:
        text = await asyncio.to_thread(stt.transcribe_wav, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"offline speech engine unavailable: {exc}")
    return {"text": text}


@app.get("/api/transcribe/status")
def transcribe_status() -> dict:
    from core import stt

    try:
        return {"ready": stt.available()}
    except Exception:
        return {"ready": False, "error": stt.last_error()}


@app.get("/api/status")
def status() -> dict:
    try:
        return pc.system_status()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/api/health")
def health() -> dict:
    from core import config

    out = {
        "llm_configured": config.llm_enabled(),
        "llm_online": False,
        "ollama_ready": config.ollama_ready(),
        "db": "ok",
        "watchers_active": 0,
        "missions_running": 0,
        "telegram": bool(__import__("core.telegram_link", fromlist=["x"]).telegram_ready()),
        "version": "3.1",
    }
    try:
        db.get_setting("ping")
    except Exception as exc:
        out["db"] = f"error: {exc}"
    try:
        out["watchers_active"] = len([w for w in db.list_watchers() if w["status"] == "active"])
    except Exception:
        pass
    try:
        out["missions_running"] = len([p for p in db.list_projects() if p["status"] == "running"])
    except Exception:
        pass
    if config.llm_enabled():
        try:
            from core import llm

            llm.chat([{"role": "user", "content": "Reply with the single word: online"}], temperature=0)
            out["llm_online"] = True
        except Exception:
            pass
    return out


@app.get("/api/settings")
def get_settings() -> dict:
    return {
        "name": config.ASSISTANT_NAME,
        "user_address": config.USER_ADDRESS,
        "briefing_enabled": db.get_setting("briefing_enabled", "0") == "1",
        "briefing_time": db.get_setting("briefing_time", "08:00"),
        "city": db.get_setting("city", ""),
        "auto_approve_code": db.get_setting("auto_approve_code", "0") == "1",
        "ambient_perception": db.get_setting("ambient_perception", "1") == "1",
        "deep_mode": db.get_setting("deep_mode", "0") == "1",
        "gui_allowed": db.get_setting("gui_allowed", "0") == "1",
        "allow_mail_send": db.get_setting("allow_mail_send", "0") == "1",
        "calendar_ical_url": db.get_setting("calendar_ical_url", ""),
        "ambient_vision_min": int(float(db.get_setting("ambient_vision_min", "0") or 0)),
    }


@app.post("/api/settings")
def set_settings(payload: dict) -> dict:
    if "briefing_enabled" in payload:
        db.set_setting("briefing_enabled", "1" if payload["briefing_enabled"] else "0")
    if payload.get("briefing_time"):
        db.set_setting("briefing_time", str(payload["briefing_time"]))
    if "auto_approve_code" in payload:
        db.set_setting("auto_approve_code", "1" if payload["auto_approve_code"] else "0")
    if "ambient_perception" in payload:
        db.set_setting("ambient_perception", "1" if payload["ambient_perception"] else "0")
    if "deep_mode" in payload:
        db.set_setting("deep_mode", "1" if payload["deep_mode"] else "0")
    if "gui_allowed" in payload:
        db.set_setting("gui_allowed", "1" if payload["gui_allowed"] else "0")
    if "allow_mail_send" in payload:
        db.set_setting("allow_mail_send", "1" if payload["allow_mail_send"] else "0")
    if "calendar_ical_url" in payload:
        db.set_setting("calendar_ical_url", str(payload["calendar_ical_url"]).strip()[:500])
    try:
        minutes = int(float(payload.get("ambient_vision_min", 0) or 0))
    except (TypeError, ValueError):
        minutes = 0
    db.set_setting("ambient_vision_min", str(max(0, min(minutes, 120))))
    db.set_setting("city", str(payload.get("city", "")))
    return {"ok": True}


@app.get("/api/knowledge")
def knowledge() -> list[dict]:
    return db.all_knowledge()


@app.delete("/api/knowledge/{kid}")
def delete_knowledge(kid: int) -> dict:
    return {"deleted": db.forget_knowledge(kid)}


@app.get("/api/workspace")
def workspace() -> list[dict]:
    from core import coding

    return coding.list_files()


@app.get("/api/workspace/file")
def workspace_file(name: str) -> dict:
    from core import coding

    try:
        return {"name": name, "content": coding.read_file(name)}
    except Exception as exc:
        return {"name": name, "error": str(exc)}


@app.get("/api/conversations")
def conversations(limit: int = 100) -> list[dict]:
    rows = db.recent_messages(max(10, min(limit, 300)))
    return [{"role": r["role"], "content": r["content"]} for r in rows]


@app.post("/api/wake")
def wake() -> dict:
    dispatcher.publish({"type": "wake", "kind": "wake", "text": "wake word detected"})
    return {"ok": True}


@app.get("/api/wake-phrases")
def wake_phrases() -> dict:
    try:
        from core.listener import WAKE_PHRASES

        return {"phrases": list(WAKE_PHRASES)}
    except Exception:
        return {"phrases": ["wake up evo"]}


@app.post("/api/announce")
def announce(event: dict) -> dict:
    text = str((event or {}).get("text", "")).strip()[:600]
    if not text:
        return {"ok": False}
    dispatcher.publish({
        "type": str(event.get("type", "note"))[:40],
        "kind": str(event.get("kind", "note"))[:40],
        "spoken": bool(event.get("spoken")),
        "user_text": event.get("user_text"),
        "text": text,
    })
    return {"ok": True}


@app.get("/api/audit")
def audit(limit: int = 60) -> list[dict]:
    return db.recent_audit(max(1, min(limit, 300)))


@app.get("/api/telegram")
def telegram_status() -> dict:
    from core import telegram_link

    return {
        "configured": telegram_link.telegram_ready(),
        "chat_ids": telegram_link.allowed_chat_ids(),
    }


@app.get("/api/watchers")
def watcher_list() -> list[dict]:
    return db.list_watchers(include_triggered=True)


@app.get("/api/tts")
def tts(text: str, voice: str = "", tone: str = ""):
    from fastapi import Response
    from core import tts as tts_mod

    try:
        path = tts_mod.synthesize(text, voice, tone=tone)
        media = "audio/mpeg"
    except Exception:
        try:
            path = tts_mod.synthesize_offline(text, tone=tone)
            media = "audio/wav"
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"speech unavailable offline and online: {exc}")
    tts_mod.cleanup()
    return Response(
        content=path.read_bytes(),
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/voices")
def voices() -> dict:
    from core import tts as tts_mod

    return {"default": tts_mod.DEFAULT_VOICE, "voices": tts_mod.list_voices()}


@app.get("/api/projects")
def projects() -> list[dict]:
    return db.list_projects()


@app.post("/api/projects")
def create_project(payload: dict) -> dict:
    from core.projects import manager

    goal = str(payload.get("goal", "")).strip()
    if not goal:
        return {"error": "goal required"}
    max_steps = int(payload.get("max_steps") or 40)
    return {"id": manager.start(goal, max_steps=max(1, min(max_steps, 200)))}


@app.post("/api/projects/{pid}/resume")
def resume_project(pid: int) -> dict:
    from core.projects import manager

    return {"result": manager.resume(pid)}


@app.get("/api/projects/{pid}")
def project_detail(pid: int) -> dict:
    row = db.get_project(pid)
    if not row:
        return {"error": "not found"}
    try:
        import json as _json

        row["log"] = _json.loads(row["log"])
    except Exception:
        row["log"] = []
    return row


@app.delete("/api/projects/{pid}")
def stop_project(pid: int) -> dict:
    from core.projects import manager

    return {"stopped": manager.stop(pid)}


@app.get("/api/reminders")
def reminders() -> list[dict]:
    return db.list_reminders(include_done=False)


@app.post("/api/reminders")
def create_reminder(item: ReminderIn) -> dict:
    rid = db.add_reminder(kind=item.kind, message=item.message, due_at=item.due_at)
    return {"id": rid}


@app.delete("/api/reminders/{reminder_id}")
def delete_reminder(reminder_id: int) -> dict:
    return {"deleted": db.cancel_reminder(reminder_id)}


@app.get("/api/memory")
def memory() -> list[dict]:
    return db.all_memories()


@app.post("/api/memory")
def add_memory(item: MemoryIn) -> dict:
    db.remember(item.key, item.value)
    return {"ok": True}


@app.delete("/api/memory/{key}")
def delete_memory(key: str) -> dict:
    return {"deleted": db.forget(key)}


@app.get("/api/events")
async def events() -> dict:
    q: Deque[dict] = dispatcher.subscribe()
    await asyncio.to_thread(_drain_wait, q)
    items = list(q)
    q.clear()
    dispatcher.unsubscribe(q)
    return {"events": items}


def _drain_wait(q: Deque[dict], wait_seconds: float = 3.0) -> None:
    import time

    deadline = time.time() + wait_seconds
    while not q and time.time() < deadline:
        time.sleep(0.2)
