"""
Reporting — Concept #5: the system produces a report as a PDF.

Builds a one-page incident/uptime PDF report for a service: event totals by
level, the top recurring messages, and the LLM incident narrative. This is
the artifact that used to take a human ~2 hours to assemble by hand from
raw logs (the 10x claim).
"""
import io
import time
from collections import Counter

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas


def build_report_pdf(service_name: str, stats: dict, llm_summary: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 1 * inch

    c.setFont("Helvetica-Bold", 18)
    c.drawString(1 * inch, y, f"PulseLog Report — {service_name}")
    y -= 0.3 * inch

    c.setFont("Helvetica", 10)
    c.drawString(1 * inch, y, f"Generated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    y -= 0.4 * inch

    c.setFont("Helvetica-Bold", 13)
    c.drawString(1 * inch, y, "Event totals (last 7 days)")
    y -= 0.25 * inch
    c.setFont("Helvetica", 11)
    for level, count in stats["counts_by_level"].items():
        c.drawString(1.2 * inch, y, f"{level}: {count}")
        y -= 0.2 * inch

    y -= 0.15 * inch
    c.setFont("Helvetica-Bold", 13)
    c.drawString(1 * inch, y, "Top recurring messages")
    y -= 0.25 * inch
    c.setFont("Helvetica", 11)
    for msg, count in stats["top_messages"]:
        c.drawString(1.2 * inch, y, f"({count}x) {msg}"[:95])
        y -= 0.2 * inch

    y -= 0.15 * inch
    c.setFont("Helvetica-Bold", 13)
    c.drawString(1 * inch, y, "AI incident summary")
    y -= 0.25 * inch
    c.setFont("Helvetica", 10)
    for line in _wrap(llm_summary, 95):
        c.drawString(1.2 * inch, y, line)
        y -= 0.18 * inch

    c.showPage()
    c.save()
    return buf.getvalue()


def _wrap(text: str, width: int):
    words = text.split()
    line, lines = "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        lines.append(line)
    return lines


def compute_stats(rows) -> dict:
    """The 'expensive' aggregation — this is what the Go cache/cron layer caches."""
    counts = Counter(r["level"] for r in rows)
    msg_counts = Counter(r["message"] for r in rows)
    return {
        "total_events": len(rows),
        "counts_by_level": dict(counts),
        "top_messages": msg_counts.most_common(5),
    }
