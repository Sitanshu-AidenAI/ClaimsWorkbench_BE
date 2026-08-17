"""Page furniture for the demo documents.

The layout primitives every demo pack draws with, in one place so that a claim
form looks the same whichever scenario produced it — and, more usefully, so that
`Label: value` keeps the exact shape the deterministic reader in
`app/domain/heuristics.py` recognises. A pack that laid its fields out
differently would extract differently for reasons that had nothing to do with
the claim.

Imported by the pack builders in this directory rather than published anywhere:
these are fixtures, and the application has no business drawing claim forms.
"""

from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

PAGE_WIDTH, PAGE_HEIGHT = A4
LEFT = 56
TOP = PAGE_HEIGHT - 64

#: Where the value column starts. Wide enough for "Environmental exposure:".
VALUE_COLUMN = 168


def header(pdf: canvas.Canvas, title: str, subtitle: str) -> float:
    """Draw a document's title block and return the y to carry on from."""
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(LEFT, TOP, title)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(LEFT, TOP - 17, subtitle)
    pdf.setLineWidth(0.6)
    pdf.line(LEFT, TOP - 27, PAGE_WIDTH - LEFT, TOP - 27)
    return TOP - 50


def section(pdf: canvas.Canvas, y: float, title: str) -> float:
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawString(LEFT, y, title)
    return y - 16


def pair(pdf: canvas.Canvas, y: float, label: str, value: str) -> float:
    """One `Label: value` line.

    A label column and a value column rather than free prose, because that is how
    a claim form reads and because it is the shape the regex reader recognises —
    so a pack extracts sensibly with or without a model provider configured.
    """
    pdf.setFont("Helvetica", 9.5)
    pdf.drawString(LEFT, y, f"{label}:")
    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawString(LEFT + VALUE_COLUMN, y, value)
    return y - 15


def paragraph(pdf: canvas.Canvas, y: float, label: str, body: str) -> float:
    pdf.setFont("Helvetica", 9.5)
    pdf.drawString(LEFT, y, f"{label}:")
    y -= 14

    pdf.setFont("Helvetica", 9.5)
    for line in wrap(body, 96):
        pdf.drawString(LEFT + 12, y, line)
        y -= 12.5
    return y - 6


def wrap(text: str, width: int) -> list[str]:
    """Greedy word wrap. Enough for a fixture; not a typesetter."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def footer(pdf: canvas.Canvas, note: str, page: int, of: int) -> None:
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(LEFT, 42, note)
    pdf.drawRightString(PAGE_WIDTH - LEFT, 42, f"Page {page} of {of}")


def new_canvas(path: str, title: str) -> canvas.Canvas:
    pdf = canvas.Canvas(path, pagesize=A4)
    pdf.setTitle(title)
    return pdf


__all__ = [
    "LEFT",
    "PAGE_HEIGHT",
    "PAGE_WIDTH",
    "TOP",
    "VALUE_COLUMN",
    "footer",
    "header",
    "new_canvas",
    "pair",
    "paragraph",
    "section",
    "wrap",
]
