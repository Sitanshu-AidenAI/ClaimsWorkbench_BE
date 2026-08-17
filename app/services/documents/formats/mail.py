"""Reading a saved email: `.eml` and Outlook `.msg`.

This is the format the IIF pipeline does not handle at all, and the one a claims
mailbox receives constantly. A broker replying "see the chain below" attaches the
chain, and `app/integrations/graph/messages.py` skips Graph's `itemAttachment`
because it carries no bytes — so a forwarded conversation reaches this service as a
`.msg` or `.eml` *file* or not at all.

What it replaces for `.eml` is worse than nothing. `message/rfc822` was registered to
the plain-text reader, so a forwarded email arrived as raw MIME: header blocks,
boundary markers, and **base64 blobs of its own attachments** — tens of thousands of
characters of `JVBERi0xLjcK…` charged against a 40,000-character budget, pushing the
broker's actual words out of the extraction prompt.

Two things are deliberate here:

* **HTML bodies go through `app.utils.sanitise.strip_html`.** That function already
  converts block tags to newlines specifically so the FNOL label reader can match
  `Label: value` at the start of a line — a dependency its own docstring records. An
  HTML email body is exactly the case it was written for.
* **Nested attachments are read one level deep and no further.** Their text is
  inlined under a named marker. Attachments that cannot be read are *named* rather
  than dropped, because "there is a survey report attached" is a fact worth
  extracting even when the bytes are unreadable. Materialising them as documents of
  their own is a separate piece of work: it needs its own de-duplication,
  per-case count limit and cycle story.

`olefile` rather than `extract-msg`: `.msg` is a documented OLE container and the six
MAPI property streams below are all this needs, whereas `extract-msg` is GPL-3.0 —
the same licence objection that rules out PyMuPDF for the PDF reader.
"""

from __future__ import annotations

import email
import io
import os
from email import policy
from email.message import EmailMessage
from typing import Any

from app.core.logging import get_logger
from app.services.documents.extracted import (
    MAX_TEXT_CHARACTERS,
    ExtractedDocumentText,
    ExtractedPage,
    clean_text,
)
from app.utils.sanitise import strip_html

logger = get_logger(__name__)

EML_EXTRACTOR = "eml"
MSG_EXTRACTOR = "msg"
EXTRACTOR_VERSION = "1"

#: How deep the reader follows attached messages. One: an email's attachments are
#: read, an attachment's attachments are named but not opened.
_MAX_NESTING = 1

#: Characters allowed for all nested attachment text combined. The message's own
#: body is never charged against this — the covering note is the notice, and an
#: attached 40-page schedule must not be able to displace it.
_NESTED_BUDGET = MAX_TEXT_CHARACTERS // 2

_HEADER_FIELDS = ("From", "To", "Cc", "Subject", "Date", "Reply-To")


# --- .eml ---------------------------------------------------------------------


def extract_eml(content: bytes, *, depth: int = 0) -> ExtractedDocumentText:
    """A saved RFC-822 message: its envelope, its body, and its attachments' text."""
    try:
        message = email.message_from_bytes(content, policy=policy.default)
    except Exception as exc:
        return ExtractedDocumentText.failed(
            f"This email file could not be parsed: {type(exc).__name__}.",
            extractor=EML_EXTRACTOR,
        )

    envelope = _envelope(message)
    body = _body(message)
    attachments, names = _attachments(_eml_parts(message), depth=depth)

    return _assemble(
        envelope=envelope,
        body=body,
        attachments=attachments,
        unread_names=names,
        subject=_header(message, "Subject"),
        extractor=EML_EXTRACTOR,
    )


def _envelope(message: EmailMessage) -> str:
    """The headers a claims officer reads, and nothing else.

    Rendered `Label: value` per line, which is both how a person reads an email
    header block and the one form the deterministic field reader can parse.
    """
    lines = []
    for field in _HEADER_FIELDS:
        value = _header(message, field)
        if value:
            lines.append(f"{field}: {value}")
    return "\n".join(lines)


def _header(message: EmailMessage, field: str) -> str:
    try:
        value = message.get(field)
    except Exception:
        # A malformed RFC-2047 encoded word raises during header decoding. One
        # unreadable header must not lose the message.
        return ""
    return " ".join(str(value).split()) if value else ""


def _own_leaves(message: Any) -> list[Any]:
    """The leaf parts belonging to *this* message.

    `Message.walk()` descends into an attached `message/rfc822` and yields its body
    parts too. Using it for the body would mean a forwarded chain's text arriving as
    the covering note's own words — attributed to the wrong message, and duplicated
    when the attachment is read properly a moment later. So the traversal stops at
    any `message/*` part and hands it to the attachment path instead.
    """
    if message.get_content_maintype() != "multipart":
        return [message]

    leaves: list[Any] = []
    for part in message.iter_parts():
        if part.get_content_maintype() == "message":
            leaves.append(part)
            continue
        leaves.extend(_own_leaves(part))
    return leaves


def _body(message: EmailMessage) -> str:
    """The body, preferring `text/plain` and falling back to stripped HTML."""
    plain: list[str] = []
    html: list[str] = []

    for part in _own_leaves(message):
        if _is_attachment(part):
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            text = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if isinstance(text, str):
            (plain if content_type == "text/plain" else html).append(text)

    if plain:
        return clean_text("\n\n".join(plain))
    return clean_text("\n\n".join(strip_html(fragment) for fragment in html))


def _eml_parts(message: EmailMessage) -> list[tuple[str, bytes]]:
    parts: list[tuple[str, bytes]] = []
    for part in _own_leaves(message):
        if not _is_attachment(part):
            continue
        payload = _part_bytes(part)
        if payload:
            parts.append((_attachment_name(part), payload))
    return parts


def _part_bytes(part: Any) -> bytes:
    """The attachment's own bytes.

    An attached message is a parsed `Message`, not a byte payload, so
    `get_payload(decode=True)` returns `None` for it and the message has to be
    re-serialised. That is the normal shape of a forwarded chain, not an edge case.
    """
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        if isinstance(payload, list) and payload:
            try:
                return payload[0].as_bytes()
            except Exception:
                return b""
        if isinstance(payload, bytes):
            return payload

    decoded = part.get_payload(decode=True)
    return decoded if isinstance(decoded, bytes) else b""


def _attachment_name(part: Any) -> str:
    """The filename, invented from the subject only when the part carries none.

    Outlook does not always name an attached message, and the extension is what
    decides which reader runs — so a nameless `message/rfc822` has to arrive as
    something ending `.eml` or it cannot be read at all.
    """
    filename = part.get_filename()
    if filename:
        return filename
    if part.get_content_maintype() == "message":
        payload = part.get_payload()
        subject = ""
        if isinstance(payload, list) and payload:
            subject = _header(payload[0], "Subject")
        stem = "".join(
            character for character in subject if character.isalnum() or character in " -_"
        ).strip()
        return f"{stem[:80] or 'forwarded message'}.eml"
    return "attachment"


def _is_attachment(part: Any) -> bool:
    if part.get_content_maintype() == "multipart":
        return False
    # An attached message is an attachment whether or not anyone named it.
    if part.get_content_maintype() == "message":
        return True
    disposition = (part.get_content_disposition() or "").lower()
    return disposition == "attachment" or bool(part.get_filename())


# --- .msg ---------------------------------------------------------------------

#: MAPI property tags, as they appear in an OLE stream name. The suffix is the
#: property type: `001F` Unicode, `001E` 8-bit, `0102` binary. Both string types are
#: tried because Outlook's choice depends on the version that saved the file.
_MSG_SUBJECT = "0037"
_MSG_BODY = "1000"
_MSG_SENDER_NAME = "0C1A"
_MSG_SENDER_EMAIL = "0C1F"
_MSG_SENT_REPRESENTING = "0042"
_MSG_DISPLAY_TO = "0E04"
_MSG_DISPLAY_CC = "0E03"
#: The full RFC-822 header block, when Outlook kept it. This is where a real `Date`
#: comes from — the sent time itself is a FILETIME in the fixed-property stream,
#: and parsing that to answer a question these headers already answer is not worth
#: the code.
_MSG_TRANSPORT_HEADERS = "007D"
_MSG_ATTACH_DATA = "3701"
_MSG_ATTACH_LONG_NAME = "3707"
_MSG_ATTACH_SHORT_NAME = "3704"


def extract_msg(content: bytes, *, depth: int = 0) -> ExtractedDocumentText:
    """An Outlook message file, read from its MAPI property streams."""
    import olefile

    buffer = io.BytesIO(content)
    if not olefile.isOleFile(buffer):
        return ExtractedDocumentText.unsupported(
            "This file is named as an Outlook message but is not one.",
            extractor=MSG_EXTRACTOR,
        )

    buffer.seek(0)
    try:
        container = olefile.OleFileIO(buffer)
    except Exception as exc:
        return ExtractedDocumentText.failed(
            f"This Outlook message could not be opened: {type(exc).__name__}.",
            extractor=MSG_EXTRACTOR,
        )

    try:
        subject = _msg_string(container, [], _MSG_SUBJECT)
        envelope = _msg_envelope(container, subject=subject)
        body = clean_text(_msg_string(container, [], _MSG_BODY))
        attachments, names = _attachments(_msg_parts(container), depth=depth)
    finally:
        container.close()

    return _assemble(
        envelope=envelope,
        body=body,
        attachments=attachments,
        unread_names=names,
        subject=subject,
        extractor=MSG_EXTRACTOR,
    )


def _msg_envelope(container: Any, *, subject: str) -> str:
    """The header block, taken from the real headers when Outlook kept them."""
    transport = _msg_string(container, [], _MSG_TRANSPORT_HEADERS)
    if transport.strip():
        try:
            parsed = email.message_from_string(transport, policy=policy.default)
        except Exception:
            parsed = None
        if parsed is not None:
            rendered = _envelope(parsed)
            if rendered:
                return rendered

    sender = _msg_string(container, [], _MSG_SENDER_NAME) or _msg_string(
        container, [], _MSG_SENT_REPRESENTING
    )
    address = _msg_string(container, [], _MSG_SENDER_EMAIL)
    lines = []
    if sender or address:
        both = f"{sender} <{address}>" if sender and address else sender or address
        lines.append(f"From: {both}")
    for field, tag in (("To", _MSG_DISPLAY_TO), ("Cc", _MSG_DISPLAY_CC)):
        value = _msg_string(container, [], tag)
        if value:
            lines.append(f"{field}: {value}")
    if subject:
        lines.append(f"Subject: {subject}")
    return "\n".join(lines)


def _msg_string(container: Any, path: list[str], tag: str) -> str:
    """One string property, trying Unicode then 8-bit."""
    for suffix, encoding in (("001F", "utf-16-le"), ("001E", "cp1252")):
        stream = [*path, f"__substg1.0_{tag}{suffix}"]
        if not container.exists("/".join(stream)):
            continue
        try:
            raw = container.openstream(stream).read()
        except Exception:
            continue
        # MAPI string streams are sometimes NUL-padded to a block boundary, and a
        # NUL cannot be stored in a Postgres text column.
        return raw.decode(encoding, errors="replace").replace("\x00", "").strip()
    return ""


def _msg_parts(container: Any) -> list[tuple[str, bytes]]:
    """Attachment `(filename, bytes)` pairs from the `__attach_…` sub-storages."""
    parts: list[tuple[str, bytes]] = []

    for entry in container.listdir(streams=False, storages=True):
        # Only top-level attachment storages: a nested one belongs to an attached
        # message, and those are named rather than opened.
        if len(entry) != 1 or not entry[0].startswith("__attach_version1.0"):
            continue
        path = [entry[0]]
        name = (
            _msg_string(container, path, _MSG_ATTACH_LONG_NAME)
            or _msg_string(container, path, _MSG_ATTACH_SHORT_NAME)
            or "attachment"
        ).strip()

        data_stream = [*path, f"__substg1.0_{_MSG_ATTACH_DATA}0102"]
        if not container.exists("/".join(data_stream)):
            # An attached *message* is a sub-storage rather than a byte stream.
            # Named, not opened: a chain three messages deep is a rabbit hole.
            parts.append((name, b""))
            continue
        try:
            parts.append((name, container.openstream(data_stream).read()))
        except Exception:
            parts.append((name, b""))

    return parts


# --- shared -------------------------------------------------------------------


def _attachments(
    parts: list[tuple[str, bytes]], *, depth: int
) -> tuple[list[tuple[str, str]], list[str]]:
    """`([(filename, text)], [filename_of_unread])` for one message's attachments."""
    read: list[tuple[str, str]] = []
    unread: list[str] = []
    remaining = _NESTED_BUDGET

    for filename, data in parts:
        if depth >= _MAX_NESTING or not data or remaining <= 0:
            unread.append(filename)
            continue

        text = _nested_text(filename, data, depth=depth)
        if not text:
            unread.append(filename)
            continue

        clipped = text[:remaining]
        remaining -= len(clipped)
        read.append((filename, clipped))

    return read, unread


def _nested_text(filename: str, data: bytes, *, depth: int) -> str:
    """Read one attachment, or return `""` if this service cannot read it."""
    # Imported here rather than at module scope: `text` registers this module's
    # extractors at import time, so a top-level import would be a cycle.
    from app.services.documents.text import extract_text
    from app.services.documents.validation import ALLOWED_EXTENSIONS

    content_type = ALLOWED_EXTENSIONS.get(os.path.splitext(filename.lower())[1])
    if content_type is None:
        return ""

    try:
        if content_type == "message/rfc822":
            result = extract_eml(data, depth=depth + 1)
        elif content_type == "application/vnd.ms-outlook":
            result = extract_msg(data, depth=depth + 1)
        else:
            result = extract_text(content_type, data)
    except Exception as exc:
        logger.info("nested_attachment_read_failed", error=type(exc).__name__)
        return ""

    return result.text


def _assemble(
    *,
    envelope: str,
    body: str,
    attachments: list[tuple[str, str]],
    unread_names: list[str],
    subject: str,
    extractor: str,
) -> ExtractedDocumentText:
    """One page for the message, one per attachment that could be read."""
    head = "\n\n".join(part for part in (envelope, body) if part.strip())
    if not head and not attachments:
        return ExtractedDocumentText.unsupported(
            "This email file contained no readable text.", extractor=extractor
        )

    label = subject.strip() or None
    if unread_names:
        listed = ", ".join(sorted(set(unread_names)))
        head = f"{head}\n\nAttachments not read: {listed}." if head else f"Attachments: {listed}."

    pages = [ExtractedPage(number=1, text=head, label=label)]
    pages.extend(
        ExtractedPage(
            number=index,
            text=f"--- ATTACHMENT: {filename} ---\n{text}",
            label=filename,
        )
        for index, (filename, text) in enumerate(attachments, start=2)
    )

    return ExtractedDocumentText.from_pages(
        pages,
        extractor=extractor,
        # An email has no pages. The page numbers here separate the message from its
        # attachments so a citation can name which one it read; the label is what an
        # officer is actually shown.
        page_count=None,
    )


__all__ = [
    "EML_EXTRACTOR",
    "EXTRACTOR_VERSION",
    "MSG_EXTRACTOR",
    "extract_eml",
    "extract_msg",
]
