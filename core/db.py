import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'reminder',
                message TEXT NOT NULL,
                due_at REAL NOT NULL,
                created_at REAL NOT NULL,
                done INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                result TEXT NOT NULL DEFAULT '',
                log TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool TEXT NOT NULL,
                args TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT 'ok',
                detail TEXT NOT NULL DEFAULT '',
                ts REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                threshold REAL NOT NULL DEFAULT 0,
                interval_sec INTEGER NOT NULL DEFAULT 300,
                status TEXT NOT NULL DEFAULT 'active',
                last_checked REAL NOT NULL DEFAULT 0,
                last_value TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS doc_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                chunk TEXT NOT NULL,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS habits (
                category TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0,
                last_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS repeats (
                phrase_hash TEXT PRIMARY KEY,
                sample TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                last_at REAL NOT NULL,
                proposed INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        _ensure_column(conn, "projects", "state", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "projects", "max_steps", "INTEGER NOT NULL DEFAULT 40")


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def log_audit(tool: str, args: str = "", outcome: str = "ok", detail: str = "") -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO audit(tool, args, outcome, detail, ts) VALUES(?,?,?,?,?)",
            (tool[:60], args[:240], outcome[:20], detail[:400], time.time()),
        )


def recent_audit(limit: int = 100) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT tool, args, outcome, detail, ts FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- watchers ----------


def add_watcher(kind: str, target: str = "", threshold: float = 0, interval_sec: int = 300) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO watchers(kind, target, threshold, interval_sec, created_at) VALUES(?,?,?,?,?)",
            (kind, target[:300], float(threshold), max(60, int(interval_sec)), time.time()),
        )
        return int(cur.lastrowid)


def remove_watcher(watcher_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM watchers WHERE id=?", (watcher_id,))
        return cur.rowcount > 0


def list_watchers(include_triggered: bool = True) -> list[dict]:
    sql = "SELECT * FROM watchers"
    if not include_triggered:
        sql += " WHERE status='active'"
    sql += " ORDER BY id DESC LIMIT 50"
    with _lock, _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def due_watchers(now: float) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM watchers WHERE status='active' AND (last_checked + interval_sec) <= ? LIMIT 20",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_watcher(watcher_id: int, status: str, last_value: str, checked_at: float, note: str = "") -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE watchers SET status=?, last_value=?, last_checked=?, note=? WHERE id=?",
            (status, last_value[:200], checked_at, note, watcher_id),
        )


# ---------- document RAG ----------


def doc_chunk_mtime(path: str) -> float | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT mtime FROM doc_chunks WHERE path=? LIMIT 1", (path,)).fetchone()
    return float(row["mtime"]) if row else None


def replace_doc_chunks(path: str, title: str, chunks: list[str], mtime: float) -> None:
    now = time.time()
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM doc_chunks WHERE path=?", (path,))
        conn.executemany(
            "INSERT INTO doc_chunks(path, title, chunk, mtime, indexed_at) VALUES(?,?,?,?,?)",
            [(path, title[:200], c[:1200], mtime, now) for c in chunks],
        )


def all_doc_chunks(limit: int = 1500) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, path, title, chunk FROM doc_chunks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def doc_stats() -> dict:
    with _lock, _connect() as conn:
        files = conn.execute("SELECT COUNT(DISTINCT path) AS n FROM doc_chunks").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM doc_chunks").fetchone()["n"]
        last = conn.execute("SELECT MAX(indexed_at) AS t FROM doc_chunks").fetchone()["t"]
    return {"files": files, "chunks": chunks, "last_indexed": last}


# ---------- feedback memory ----------


def add_correction(trigger: str, instruction: str) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO corrections(trigger, instruction, created_at) VALUES(?,?,?)",
            (trigger[:200], instruction[:500], time.time()),
        )
        return int(cur.lastrowid)


def list_corrections(limit: int = 30) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, trigger, instruction FROM corrections ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def matching_corrections(text: str, limit: int = 4) -> list[dict]:
    rows = list_corrections(60)
    if not rows or not text:
        return []
    words = {w.strip("?,.!;'\"").lower() for w in text.split() if len(w) > 3}
    scored = []
    for r in rows:
        blob = f"{r['trigger']} {r['instruction']}".lower()
        score = sum(1 for w in words if w in blob)
        if score or not r["trigger"]:
            scored.append((score, r))
    scored.sort(key=lambda p: p[0], reverse=True)
    return [r for _, r in scored[:limit]]


def forget_correction(correction_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM corrections WHERE id=?", (correction_id,))
        return cur.rowcount > 0


# ---------- habit engine ----------


def record_habit(category: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO habits(category, count, last_at) VALUES(?,1,?) "
            "ON CONFLICT(category) DO UPDATE SET count=count+1, last_at=excluded.last_at",
            (category[:40], time.time()),
        )


def top_habits(n: int = 5) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT category, count FROM habits ORDER BY count DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def track_repeat(text: str) -> dict | None:
    """Returns the row when a phrase crosses the proposal threshold (once)."""
    import hashlib
    import re as _re

    normalized = _re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()
    if len(normalized.split()) < 2:
        return None
    phash = hashlib.sha1(normalized.encode()).hexdigest()[:24]
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM repeats WHERE phrase_hash=?", (phash,)).fetchone()
        if row:
            count = row["count"] + 1
            proposed = row["proposed"]
            conn.execute(
                "UPDATE repeats SET count=?, last_at=?, sample=? WHERE phrase_hash=?",
                (count, time.time(), text[:200], phash),
            )
        else:
            count = 1
            proposed = 0
            conn.execute(
                "INSERT INTO repeats(phrase_hash, sample, count, last_at) VALUES(?,?,?,?)",
                (phash, text[:200], count, time.time()),
            )
    if count >= 3 and not proposed:
        with _lock, _connect() as conn:
            conn.execute("UPDATE repeats SET proposed=1 WHERE phrase_hash=?", (phash,))
        return {"sample": text[:200], "count": count}
    return None


def log_message(role: str, content: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO messages(role, content, ts) VALUES(?,?,?)",
            (role, content[:4000], time.time()),
        )


def recent_messages(limit: int = 24) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_messages() -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM messages")


def relevant_knowledge(query: str, top: int = 3, preview: int = 320) -> list[dict]:
    rows = all_knowledge()
    if not rows:
        return []
    stop = {"the", "and", "for", "with", "that", "this", "from", "what", "how", "about", "you", "your"}
    words = {w.strip("?,.!;'\"").lower() for w in query.split() if len(w) > 3} - stop
    if not words:
        return []
    scored = []
    for row in rows[:200]:
        blob = f"{row['topic']} {row['preview']}".lower()
        score = sum(1 for w in words if w in blob)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"topic": r["topic"], "snippet": r["preview"][:preview]}
        for _, r in scored[:top]
    ]


def create_project(goal: str, max_steps: int = 40) -> int:
    now = time.time()
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO projects(goal, max_steps, created_at, updated_at) VALUES(?,?,?,?)",
            (goal, int(max_steps), now, now),
        )
        return int(cur.lastrowid)


def save_project_state(project_id: int, state_json: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE projects SET state=?, updated_at=? WHERE id=?",
            (state_json[:60000], time.time(), project_id),
        )


def project_log_append(project_id: int, entry: str) -> list:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT log FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            return []
        try:
            log = json.loads(row["log"])
        except Exception:
            log = []
        log.append({"ts": time.time(), "entry": entry[:500]})
        conn.execute("UPDATE projects SET log=?, updated_at=? WHERE id=?", (json.dumps(log[-40:]), time.time(), project_id))
        return log[-40:]


def project_status(project_id: int) -> str:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
    return row["status"] if row else "missing"


def set_project_running(project_id: int) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE projects SET status='running', result='', updated_at=? WHERE id=?",
            (time.time(), project_id),
        )


def finish_project(project_id: int, status: str, result: str, keep_state: bool = False) -> None:
    with _lock, _connect() as conn:
        if keep_state:
            conn.execute(
                "UPDATE projects SET status=?, result=?, updated_at=? WHERE id=?",
                (status, result[:4000], time.time(), project_id),
            )
        else:
            conn.execute(
                "UPDATE projects SET status=?, result=?, state='', updated_at=? WHERE id=?",
                (status, result[:4000], time.time(), project_id),
            )


def get_project(project_id: int) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects(limit: int = 20) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, goal, status, substr(result,1,160) AS preview, updated_at FROM projects ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def learn(topic: str, content: str, source: str = "") -> int:
    with _lock, _connect() as conn:
        existing = conn.execute("SELECT id FROM knowledge WHERE topic = ?", (topic.lower(),)).fetchone()
        if existing:
            conn.execute(
                "UPDATE knowledge SET content=?, source=?, created_at=? WHERE id=?",
                (content, source, time.time(), existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO knowledge(topic, content, source, created_at) VALUES(?,?,?,?)",
            (topic.lower(), content, source, time.time()),
        )
        return int(cur.lastrowid)


def recall_knowledge(query: str) -> list[dict]:
    pattern = f"%{query.lower()}%"
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, topic, content, source FROM knowledge WHERE topic LIKE ? OR content LIKE ? "
            "ORDER BY created_at DESC LIMIT 5",
            (pattern, pattern),
        ).fetchall()
    return [dict(r) for r in rows]


def all_knowledge() -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, topic, source, substr(content,1,140) AS preview FROM knowledge ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


def forget_knowledge(kid: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM knowledge WHERE id = ?", (kid,))
        return cur.rowcount > 0


def get_setting(key: str, default: str = "") -> str:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def remember(key: str, value: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO memories(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key.lower(), value, time.time()),
        )


def forget(key: str) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM memories WHERE key = ?", (key.lower(),))
        return cur.rowcount > 0


def get_memory(key: str) -> str | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT value FROM memories WHERE key = ?", (key.lower(),)).fetchone()
        return row["value"] if row else None


def search_memory(query: str) -> list[dict[str, str]]:
    pattern = f"%{query.lower()}%"
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM memories WHERE key LIKE ? OR value LIKE ? ORDER BY updated_at DESC LIMIT 20",
            (pattern, pattern),
        ).fetchall()
    return [{"key": r["key"], "value": r["value"]} for r in rows]


def all_memories() -> list[dict[str, str]]:
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT key, value FROM memories ORDER BY updated_at DESC LIMIT 200").fetchall()
    return [{"key": r["key"], "value": r["value"]} for r in rows]


def add_reminder(kind: str, message: str, due_at: float) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders(kind, message, due_at, created_at) VALUES(?,?,?,?)",
            (kind, message, due_at, time.time()),
        )
        return int(cur.lastrowid)


def cancel_reminder(reminder_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return cur.rowcount > 0


def cancel_all_reminders() -> int:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM reminders")
        return cur.rowcount


def list_reminders(include_done: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reminders"
    if not include_done:
        sql += " WHERE done = 0"
    sql += " ORDER BY due_at ASC LIMIT 50"
    with _lock, _connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def due_reminders(now: float) -> list[dict[str, Any]]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE done = 0 AND due_at <= ? ORDER BY due_at ASC", (now,)
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE reminders SET done = 1 WHERE id IN (%s)"
                % ",".join("?" * len(rows)),
                [r["id"] for r in rows],
            )
    return [dict(r) for r in rows]
