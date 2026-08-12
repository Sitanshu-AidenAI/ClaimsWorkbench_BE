"""HTML sanitising.

Every path that stores or emits user-authored HTML goes through here so the
allow-list lives in exactly one place.
"""

from __future__ import annotations

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


def strip_html(value: str) -> str:
    """Remove all markup, leaving plain text."""
    return nh3.clean(value, tags=set(), clean_content_tags=STRIPPED_CONTENT_TAGS)
