import os
import re
import time
from pathlib import Path

from .config import DATA_DIR

SITES_DIR = DATA_DIR / "sites"
SITES_DIR.mkdir(parents=True, exist_ok=True)

THEME_SYSTEM = (
    "You are a senior product designer writing production CSS. Return ONLY raw CSS — no markdown fences, no commentary. "
    "Requirements: CSS custom properties for the palette; modern professional aesthetic; system font stack; "
    "responsive breakpoints at 900px and 600px; prefers-color-scheme dark variant; accessible contrast ratios; "
    "sticky translucent header with backdrop blur; hero section; card grid with hover lift; polished buttons; footer; "
    "scroll-behavior smooth; :focus-visible outlines. Maximum ~220 lines."
)

PAGE_SYSTEM = (
    "You are an elite web copywriter and front-end engineer producing ONE page of a multi-page site. "
    "Hard rules: complete HTML5 document starting with <!DOCTYPE html>; charset and viewport meta; title, meta description, Open Graph tags; "
    '<link rel="stylesheet" href="style.css"> and <link rel="icon" href="favicon.svg">; semantic header/nav/main/section/footer; '
    "nav links EXACTLY to these files: {nav_list} — mark {current} with class 'active'; brand '{brand}' in the nav; "
    "hero section aligned to the brief; substantial real copy (never lorem ipsum); alt attributes on every image "
    "(use https://images.unsplash.com photo URLs when imagery helps); footer with © year and brand; "
    "one small inline <script> only for the mobile menu toggle. Return ONLY raw HTML."
)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _valid_html(html: str) -> bool:
    low = html.lower()
    return "</html>" in low and "viewport" in low and "style.css" in low


def _derive_pages(brief: str, provided: list[str] | None) -> list[str]:
    if provided:
        cleaned = [re.sub(r"[^a-z0-9-]+", "", p.lower()).strip("-") for p in provided]
        pages = list(dict.fromkeys(p or "index" for p in cleaned if p))
    else:
        pages = ["index"]
        lowered = brief.lower()
        for keyword, page in (
            ("about", "about"), ("pricing", "pricing"), ("contact", "contact"),
            ("service", "services"), ("portfolio", "portfolio"), ("blog", "blog"),
            ("menu", "menu"), ("gallery", "gallery"), ("team", "team"),
        ):
            if keyword in lowered and page not in pages:
                pages.append(page)
    if "index" not in pages:
        pages.insert(0, "index")
    return pages[:6]


def _brand(brief: str, fallback: str) -> str:
    try:
        from .llm import chat

        raw = chat(
            [
                {"role": "system", "content": "Extract ONLY the business/site name from this brief. Reply with the name alone, nothing else."},
                {"role": "user", "content": brief},
            ],
            temperature=0.2,
        )
        name = raw.strip().strip('"').split("\n")[0][:40]
        return name or fallback
    except Exception:
        return fallback


def _favicon(brand: str) -> str:
    letter = re.sub(r"[^A-Za-z0-9]", "", brand)[:1].upper() or "E"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#4fd2ff"/><stop offset="1" stop-color="#2563eb"/></linearGradient></defs>'
        f'<rect width="64" height="64" rx="14" fill="url(#g)"/>'
        f'<text x="32" y="42" font-family="Segoe UI,Arial" font-size="30" font-weight="700" '
        f'fill="#04101e" text-anchor="middle">{letter}</text></svg>'
    )


DEPLOY_README = """How to publish this site
========================
Option A (fastest): drag the whole folder onto https://app.netlify.com/drop
Option B: push to GitHub and enable GitHub Pages (branch root).
Option C: any static host (Vercel, Cloudflare Pages, cPanel).

Everything is plain HTML/CSS — no build step required.
"""


def build_site(brief: str, name: str = "site", pages: list[str] | None = None) -> dict:
    from .llm import chat

    brief = (brief or "").strip()[:3000]
    if not brief:
        raise ValueError("A website brief is required.")
    safe_name = re.sub(r"[^a-z0-9_-]+", "-", (name or "").lower()).strip("-") or f"site-{int(time.time())}"
    page_list = _derive_pages(brief, pages)
    brand = _brand(brief, safe_name.replace("-", " ").title())

    theme_css = ""
    for attempt in range(2):
        raw = chat(
            [{"role": "system", "content": THEME_SYSTEM}, {"role": "user", "content": brief}],
            temperature=0.5,
        )
        theme_css = _strip_fences(raw)
        if len(theme_css) > 250 and "{" in theme_css:
            break

    folder = SITES_DIR / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "style.css").write_text(theme_css[:30000], encoding="utf-8")
    (folder / "favicon.svg").write_text(_favicon(brand), encoding="utf-8")
    (folder / "README-deploy.txt").write_text(DEPLOY_README, encoding="utf-8")

    nav_list = ", ".join("index.html" if p == "index" else f"{p}.html" for p in page_list)
    written = ["style.css", "favicon.svg", "README-deploy.txt"]
    for page in page_list:
        current = "index.html" if page == "index" else f"{page}.html"
        system_prompt = PAGE_SYSTEM.format(nav_list=nav_list, current=current, brand=brand)
        user_prompt = f"BRIEF:\n{brief}\n\nThis is the '{page}' page."
        html = ""
        for attempt in range(2):
            raw = chat(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.6,
            )
            candidate = _strip_fences(raw)
            if _valid_html(candidate):
                html = candidate
                break
            if not html:
                html = candidate
        if not _valid_html(html):
            html += "\n<!-- note: generated without full validation -->"
        (folder / current).write_text(html[:120000], encoding="utf-8")
        written.append(current)

    try:
        os.startfile(folder)  # noqa: S606
    except Exception:
        pass
    return {
        "folder": str(folder),
        "brand": brand,
        "pages": page_list,
        "files": written,
        "summary": (
            f"Website '{brand}' built at {folder} with pages: {', '.join(page_list)}. "
            "Open README-deploy.txt to publish it free on Netlify/GitHub Pages."
        ),
    }
