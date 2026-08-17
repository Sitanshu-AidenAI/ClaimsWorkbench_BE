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


def test_strip_html_turns_paragraph_boundaries_into_line_breaks() -> None:
    # Real mail clients send "plain text" as one <p> per line. Without a line
    # break at each boundary, two labelled fields collapse onto one run-on
    # line — and the FNOL heuristic reader matches `Label: value` anchored to
    # the start of a line, so a labelled field on a run-on line is invisible.
    html_body = "<p>Policy number: POL-2026-0041</p><p>Insured: Test Ltd</p>"

    assert strip_html(html_body) == "Policy number: POL-2026-0041\nInsured: Test Ltd"


def test_strip_html_treats_br_as_a_line_break_too() -> None:
    assert strip_html("Line one<br>Line two<br/>Line three") == "Line one\nLine two\nLine three"


def test_strip_html_decodes_entities_rather_than_leaving_them_escaped() -> None:
    assert (
        strip_html("<p>Meridian Print &amp; Packaging Ltd</p>") == "Meridian Print & Packaging Ltd"
    )


def test_strip_html_collapses_runs_of_blank_lines_from_adjacent_block_tags() -> None:
    html_body = "<div><p>First</p><p></p><p><br></p><p>Second</p></div>"

    assert strip_html(html_body) == "First\n\nSecond"


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
