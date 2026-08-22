import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip())[:40].strip("-")
    return base or f"report-{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _open_folder() -> None:
    try:
        subprocess.Popen(["explorer", str(REPORTS_DIR)])
    except Exception:
        pass


def make_chart(title: str, series: str, kind: str = "bar") -> str:
    pairs = []
    for chunk in series.split(";"):
        if ":" not in chunk:
            continue
        label, _, raw_value = chunk.partition(":")
        label = label.strip()
        try:
            value = float(raw_value.strip().replace(",", ""))
        except ValueError:
            continue
        if label:
            pairs.append((label[:40], value))
    if not pairs:
        return "ERROR: series must look like 'Mon:12; Tue:30; Wed:7'."
    kind = kind if kind in ("bar", "line") else "bar"
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [p[0] for p in pairs]
    values = [p[1] for p in pairs]
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=130)
    if kind == "line":
        ax.plot(labels, values, marker="o", linewidth=2.2)
        ax.fill_between(range(len(values)), values, alpha=0.15)
    else:
        bars = ax.bar(labels, values, color="#2563eb")
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), f"{val:g}",
                    ha="center", va="bottom", fontsize=9)
    ax.set_title(title[:80], fontsize=13, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    out = REPORTS_DIR / f"{_slug(title)}.png"
    fig.savefig(out)
    plt.close(fig)
    _open_folder()
    return f"Chart saved: {out}"


def make_pdf(title: str, content: str, filename: str = "") -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    body = ParagraphStyle("EVOBody", parent=styles["Normal"], fontSize=10.5, leading=15)
    bullet = ParagraphStyle("EVOBullet", parent=body, leftIndent=14, bulletIndent=4)

    doc = SimpleDocTemplate(
        str(REPORTS_DIR / f"{_slug(filename or title)}.pdf"),
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=title[:120],
    )
    story = [
        Paragraph(f"<b>{title[:150]}</b>", styles["Title"]),
        Spacer(1, 10),
        Paragraph(datetime.now().strftime("Prepared by EVO — %d %B %Y %H:%M"), styles["Italic"]),
        Spacer(1, 14),
    ]
    for block in (content or "").split("\n\n"):
        text = " ".join(block.split())
        if not text:
            continue
        if text.startswith("- ") or text.startswith("* "):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith(("- ", "* ")):
                    story.append(Paragraph(line[2:], bullet, bulletText="•"))
                    continue
                if line:
                    story.append(Paragraph(line, body))
        else:
            story.append(Paragraph(text.replace("\n", "<br/>"), body))
        story.append(Spacer(1, 8))

    out = REPORTS_DIR / f"{_slug(filename or title)}.pdf"
    doc.build(story)
    _open_folder()
    return f"PDF saved: {out}"
