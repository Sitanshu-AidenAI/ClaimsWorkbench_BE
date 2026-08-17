"""HTML sanitising.

Every path that stores or emits user-authored HTML goes through here so the
allow-list lives in exactly one place.
"""

from __future__ import annotations

import html
import re

import nh3

ALLOWED_TAGS: set[str] = {
    "a", "abbr", "b", "blockquote", "br", "code", "em", "i", "li", "ol", "p",
    "pre", "span", "strong", "sub", "sup", "table", "tbody", "td", "th", "thead",
    "tr", "ul", "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}  # fmt: skip

# `rel` is deliberately absent from the `a` entry: nh3 manages it via
# `link_rel`, and allowing both is rejected.
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "span": {"class"},
    "code": {"class"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}

# Tags whose *contents* are dropped along with the tag, rather than being
# unwrapped into the surrounding text.
STRIPPED_CONTENT_TAGS: set[str] = {"script", "style", "iframe", "object", "embed"}


def sanitise_html(value: str) -> str:
    """Strip disallowed tags/attributes, keeping the safe subset."""
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        clean_content_tags=STRIPPED_CONTENT_TAGS,
        link_rel="noopener noreferrer",
    )


#: Where a line break belongs in the plain-text a human would read: at a
#: self-closing line tag, or at the *close* of a block element. Only the close
#: is matched — the matching open contributes nothing — because `<p>A</p><p>B</p>`
#: must become `"A\nB"`, one break, not `"A\n\nB"` from counting both tags.
#: Without this, `<p>Policy number: X</p><p>Insured: Y</p>` collapses into one
#: run-on line once the tags are gone, and the FNOL heuristic reader matches
#: `Label: value` anchored to the start of a line — a run-on line is a line
#: nothing on it can ever match.
_LINE_BREAK_RE = re.compile(
    r"<(?:br|hr)\b[^>]*>|</(?:p|div|li|tr|table|h[1-6]|blockquote)\s*>",
    re.IGNORECASE,
)


def strip_html(value: str) -> str:
    """Remove all markup, leaving plain text with paragraph breaks preserved.

    Two things a naive tag-strip gets wrong for a labelled-field reader: block
    boundaries have to become newlines, not nothing, and entities have to be
    decoded — `nh3.clean` still returns HTML-safe text, so `&amp;` stays
    `&amp;` rather than becoming `&`.
    """
    with_breaks = _LINE_BREAK_RE.sub("\n", value)
    stripped = nh3.clean(with_breaks, tags=set(), clean_content_tags=STRIPPED_CONTENT_TAGS)
    text = html.unescape(stripped)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
