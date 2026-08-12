"""PDF generation from Markdown.

Markdown is rendered to HTML, sanitised with nh3, and then laid out with
ReportLab. Sanitising happens before layout because the source Markdown may
carry user-authored content (adjuster notes, correspondence).
"""

from __future__ import annotations

import io
import re

import markdown as md
import nh3
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

# ReportLab's Paragraph parser understands a small inline HTML subset; anything
# outside it has to be stripped before layout.
_PARAGRAPH_SAFE_TAGS = {"b", "strong", "i", "em", "u", "br", "font", "sub", "super", "a"}

# The parser rejects attributes it does not know — including the
# `rel="noopener noreferrer"` that nh3 adds to links by default.
_PARAGRAPH_SAFE_ATTRIBUTES = {
    "a": {"href", "color"},
    "font": {"face", "size", "color"},
}

_BLOCK_RE = re.compile(
    r"<(h[1-6]|p|ul|ol|li|blockquote|pre)[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)


def markdown_to_safe_html(source: str) -> str:
    """Render Markdown and strip anything unsafe."""
    html = md.markdown(source, extensions=["extra", "sane_lists", "tables"])
    return nh3.clean(html)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "CWBBody",
        parent=base["BodyText"],
        fontSize=10,
        leading=14,
        spaceAfter=6,
        alignment=TA_LEFT,
    )
    return {
        "body": body,
        "h1": ParagraphStyle("CWBH1", parent=base["Heading1"], fontSize=18, spaceAfter=10),
        "h2": ParagraphStyle("CWBH2", parent=base["Heading2"], fontSize=14, spaceAfter=8),
        "h3": ParagraphStyle("CWBH3", parent=base["Heading3"], fontSize=12, spaceAfter=6),
        "quote": ParagraphStyle(
            "CWBQuote", parent=body, leftIndent=10 * mm, textColor="#4b5563", italic=True
        ),
    }


def _inline_only(fragment: str) -> str:
    """Reduce a fragment to the inline markup ReportLab's parser accepts."""
    return nh3.clean(
        fragment,
        tags=_PARAGRAPH_SAFE_TAGS,
        attributes=_PARAGRAPH_SAFE_ATTRIBUTES,
        link_rel=None,
    ).strip()


def render_markdown_pdf(
    markdown_source: str,
    *,
    title: str | None = None,
    author: str = "Claims Workbench",
) -> bytes:
    """Render Markdown to a PDF document and return the bytes."""
    html = markdown_to_safe_html(markdown_source)
    styles = _styles()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        title=title or "Document",
        author=author,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )

    flowables: list[object] = []
    if title:
        flowables.extend([Paragraph(_inline_only(title), styles["h1"]), Spacer(1, 6)])

    for match in _BLOCK_RE.finditer(html):
        tag = match.group(1).lower()
        content = match.group(2)

        if tag in ("ul", "ol"):
            items = [
                ListItem(Paragraph(_inline_only(li), styles["body"]))
                for li in re.findall(r"<li[^>]*>(.*?)</li>", content, re.DOTALL | re.IGNORECASE)
            ]
            if items:
                # ReportLab wants `start` as a string, even for numbered lists.
                ordered = tag == "ol"
                flowables.append(
                    ListFlowable(
                        items,
                        bulletType="1" if ordered else "bullet",
                        start="1" if ordered else None,
                    )
                )
                flowables.append(Spacer(1, 4))
        elif tag == "li":
            continue  # handled by the parent list
        elif tag.startswith("h"):
            level = min(int(tag[1]), 3)
            flowables.append(Paragraph(_inline_only(content), styles[f"h{level}"]))
        elif tag == "blockquote":
            flowables.append(Paragraph(_inline_only(content), styles["quote"]))
        elif tag == "pre":
            flowables.append(Paragraph(_inline_only(content), styles["body"]))
        else:
            text = _inline_only(content)
            if text:
                flowables.append(Paragraph(text, styles["body"]))

    if not flowables:
        flowables.append(Paragraph("", styles["body"]))

    document.build(flowables)
    return buffer.getvalue()
