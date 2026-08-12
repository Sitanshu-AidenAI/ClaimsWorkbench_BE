"""HTML sanitising and PDF rendering."""

from __future__ import annotations

from app.services.pdf import markdown_to_safe_html, render_markdown_pdf
from app.utils.sanitise import sanitise_html, strip_html


def test_sanitise_strips_scripts_and_event_handlers() -> None:
    dirty = '<p onclick="steal()">safe<script>alert(1)</script></p>'

    cleaned = sanitise_html(dirty)

    assert "script" not in cleaned
    assert "onclick" not in cleaned
    assert "safe" in cleaned


def test_sanitise_keeps_the_allowed_subset() -> None:
    cleaned = sanitise_html("<p><strong>bold</strong> and <em>italic</em></p>")

    assert "<strong>bold</strong>" in cleaned
    assert "<em>italic</em>" in cleaned


def test_sanitise_hardens_outbound_links() -> None:
    cleaned = sanitise_html('<a href="https://example.test">link</a>')

    assert 'href="https://example.test"' in cleaned
    assert "noopener" in cleaned


def test_strip_html_removes_all_markup() -> None:
    assert strip_html("<p>hello <b>there</b></p>").strip() == "hello there"


def test_markdown_is_rendered_then_sanitised() -> None:
    html = markdown_to_safe_html("# Heading\n\n<script>alert(1)</script>\n\nBody **text**.")

    assert "<h1>Heading</h1>" in html
    assert "script" not in html
    assert "<strong>text</strong>" in html


def test_render_markdown_pdf_produces_a_pdf() -> None:
    source = (
        "# Claim summary\n\n"
        "Adjuster notes with **emphasis** and a [link](https://example.test).\n\n"
        "## Findings\n\n"
        "- First finding\n"
        "- Second finding\n\n"
        "1. Step one\n"
        "2. Step two\n\n"
        "> A quoted remark.\n"
    )

    pdf = render_markdown_pdf(source, title="Claim 123")

    assert pdf.startswith(b"%PDF-")
    assert len(pdf) > 1000


def test_render_markdown_pdf_handles_empty_input() -> None:
    assert render_markdown_pdf("").startswith(b"%PDF-")
