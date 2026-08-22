import os
import re
import time
from pathlib import Path

from . import db

SUPPORTED = {".txt", ".md", ".py", ".json", ".csv", ".html", ".htm", ".log", ".xml", ".yml", ".yaml", ".pdf"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "site-packages"}
MAX_FILE_BYTES = 400_000


def _extract(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages[:80]:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def chunk_text(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    text = " ".join((text or "").split())
    if len(text) < 40:
        return []
    chunks = []
    start = 0
    while start < len(text):
        piece = text[start : start + size]
        chunks.append(piece)
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def index_folder(root: str, max_files: int = 400) -> dict:
    base = Path(root).expanduser().resolve()
    if not base.exists() or not base.is_dir():
        raise ValueError(f"Folder not found: {root}")
    files_indexed = 0
    chunks_added = 0
    skipped = 0
    stopped = False
    for dirpath, dirnames, filenames in os.walk(base):
        if stopped:
            break
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fname in filenames:
            if files_indexed >= max_files:
                stopped = True
                break
            p = Path(dirpath) / fname
            if p.suffix.lower() not in SUPPORTED:
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if stat.st_size == 0 or stat.st_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            rel_path = str(p)
            known_mtime = db.doc_chunk_mtime(rel_path)
            if known_mtime is not None and abs(known_mtime - stat.st_mtime) < 2:
                continue
            try:
                text = _extract(p)
            except Exception:
                skipped += 1
                continue
            pieces = chunk_text(text)
            db.replace_doc_chunks(rel_path, p.name, pieces, stat.st_mtime)
            files_indexed += 1
            chunks_added += len(pieces)
    return {"files_indexed": files_indexed, "chunks_added": chunks_added, "skipped": skipped}


_STOP = {
    "the", "and", "for", "with", "that", "this", "from", "what", "how",
    "about", "you", "your", "are", "was", "were", "has", "have", "did",
}


def search(query: str, top: int = 6) -> list[dict]:
    words = [w for w in re.findall(r"[a-z0-9]{3,}", (query or "").lower()) if w not in _STOP]
    if not words:
        return []
    rows = db.all_doc_chunks(limit=1500)
    scored = []
    for row in rows:
        blob = row["chunk"].lower()
        score = sum(blob.count(w) for w in words)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    seen_paths: set[str] = set()
    out = []
    for score, row in scored:
        if row["path"] in seen_paths:
            continue
        seen_paths.add(row["path"])
        out.append({"path": row["path"], "title": row["title"], "score": score, "snippet": row["chunk"][:340]})
        if len(out) >= top:
            break
    return out


def status() -> dict:
    return db.doc_stats()


def format_results(results: list[dict]) -> str:
    if not results:
        return "Nothing relevant found in the indexed documents."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['title']}] ({r['path']})\n   {r['snippet']}")
    return "\n\n".join(lines)
