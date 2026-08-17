"""Graph's message JSON, read into something this codebase can hold.

Pure functions over dictionaries: no HTTP, no settings, no database. That is
what makes the mapping testable against a captured Graph payload without a
tenant, and it is where every "Graph sometimes omits this" allowance lives, so
the client above stays readable.

Graph is generous with nulls. A message can arrive with no `from` (a draft
copied into the folder), no `body` (a `$select` that did not ask for one) and a
`receivedDateTime` in a format that is ISO-8601 but not the one Python's parser
liked before 3.11. Each of those is handled here rather than at nine call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Graph's discriminator for a real file. The other two — `itemAttachment` (an
#: embedded message or event) and `referenceAttachment` (a link to OneDrive) —
#: carry no bytes on this endpoint and are skipped.
FILE_ATTACHMENT_TYPE = "#microsoft.graph.fileAttachment"

#: The fields intake asks Graph for. Stated once, because `$select` is also what
#: keeps a 40MB body out of a list response.
MESSAGE_SELECT_FIELDS = (
    "id",
    "internetMessageId",
    "conversationId",
    "subject",
    "from",
    "sender",
    "toRecipients",
    "ccRecipients",
    "receivedDateTime",
    "hasAttachments",
    "isRead",
    "bodyPreview",
    "body",
)

#: Attachment metadata, deliberately without `contentBytes`: the size is read
#: first and the bytes are fetched separately, so an oversized attachment is
#: refused without ever being downloaded.
ATTACHMENT_SELECT_FIELDS = ("id", "name", "contentType", "size", "isInline")


@dataclass(slots=True, frozen=True)
class GraphRecipient:
    """One name/address pair off an envelope. Either half may be missing."""

    name: str | None
    address: str | None

    def as_text(self) -> str:
        if self.name and self.address:
            return f"{self.name} <{self.address}>"
        return self.address or self.name or ""


@dataclass(slots=True, frozen=True)
class GraphAttachmentMetadata:
    """What Graph says about an attachment before anything is downloaded."""

    attachment_id: str
    name: str
    content_type: str | None
    size_bytes: int
    is_inline: bool
    odata_type: str

    @property
    def is_file(self) -> bool:
        return self.odata_type == FILE_ATTACHMENT_TYPE


@dataclass(slots=True)
class GraphMessage:
    """One mailbox message, in the only shape the intake service knows about."""

    message_id: str
    subject: str
    body: str
    body_is_html: bool
    received_at: datetime
    has_attachments: bool
    is_read: bool
    internet_message_id: str | None = None
    conversation_id: str | None = None
    sender: GraphRecipient = field(default_factory=lambda: GraphRecipient(None, None))
    to_recipients: list[GraphRecipient] = field(default_factory=list)
    cc_recipients: list[GraphRecipient] = field(default_factory=list)
    body_preview: str | None = None

    @property
    def dedupe_key(self) -> str:
        """The identity a notice is deduplicated on across every channel.

        `internetMessageId` is the RFC 5322 identifier the sending server minted,
        so it survives the message being moved, copied or re-delivered — which
        the Graph id does not. The Graph id is the fallback, namespaced so it can
        never be mistaken for a real message id from another integration.
        """
        return self.internet_message_id or f"graph:{self.message_id}"


def parse_message(payload: dict[str, Any]) -> GraphMessage:
    """Read one `microsoft.graph.message` resource."""
    body = payload.get("body") or {}
    content_type = str(body.get("contentType") or "text").lower()

    return GraphMessage(
        message_id=str(payload.get("id") or ""),
        internet_message_id=_clean(payload.get("internetMessageId")),
        conversation_id=_clean(payload.get("conversationId")),
        subject=_clean(payload.get("subject")) or "(no subject)",
        body=str(body.get("content") or ""),
        body_is_html=content_type == "html",
        body_preview=_clean(payload.get("bodyPreview")),
        received_at=parse_timestamp(payload.get("receivedDateTime")),
        has_attachments=bool(payload.get("hasAttachments")),
        is_read=bool(payload.get("isRead")),
        sender=parse_recipient(payload.get("from") or payload.get("sender")),
        to_recipients=parse_recipients(payload.get("toRecipients")),
        cc_recipients=parse_recipients(payload.get("ccRecipients")),
    )


def parse_recipient(payload: Any) -> GraphRecipient:
    if not isinstance(payload, dict):
        return GraphRecipient(None, None)
    address = payload.get("emailAddress")
    if not isinstance(address, dict):
        return GraphRecipient(None, None)
    return GraphRecipient(name=_clean(address.get("name")), address=_clean(address.get("address")))


def parse_recipients(payload: Any) -> list[GraphRecipient]:
    if not isinstance(payload, list):
        return []
    parsed = [parse_recipient(item) for item in payload]
    return [item for item in parsed if item.address or item.name]


def parse_attachment(payload: dict[str, Any]) -> GraphAttachmentMetadata:
    """Read one attachment's metadata. Never its bytes."""
    return GraphAttachmentMetadata(
        attachment_id=str(payload.get("id") or ""),
        name=_clean(payload.get("name")) or "attachment",
        content_type=_clean(payload.get("contentType")),
        size_bytes=int(payload.get("size") or 0),
        is_inline=bool(payload.get("isInline")),
        odata_type=str(payload.get("@odata.type") or FILE_ATTACHMENT_TYPE),
    )


def parse_timestamp(raw: Any) -> datetime:
    """Graph's `receivedDateTime`, as an aware UTC datetime.

    Falls back to now rather than raising: a message whose timestamp cannot be
    read is still a claim notification, and refusing to collect it because of a
    date format would be the wrong trade every time.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
