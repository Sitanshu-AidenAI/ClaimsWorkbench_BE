"""What this service will accept as an attachment.

An upload endpoint that trusts the client is an upload endpoint that stores
whatever the client sends under whatever name the client chose. All three of the
things that go wrong — the filename, the declared type, and the size — are
checked here rather than in the route, so the email ingestion path gets the same
treatment as the browser one.
"""

from __future__ import annotations

import os
import re
import unicodedata

from app.core.errors import ValidationError

#: Extensions a claims file legitimately carries. Anything executable, archived
#: or scriptable is refused: this service extracts text from documents, and there
#: is no claims workflow that needs a `.zip` or an `.html` to do it.
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".eml": "message/rfc822",
    # A broker replying "see the chain below" attaches the chain, and Graph's
    # `itemAttachment` carries no bytes — so a forwarded conversation arrives as a
    # `.msg` file or not at all.
    ".msg": "application/vnd.ms-outlook",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

#: Magic bytes, checked against the extension. A `.pdf` whose first bytes are
#: `MZ` is not a PDF, whatever it is called.
_SIGNATURES: tuple[tuple[bytes, frozenset[str]], ...] = (
    (b"%PDF-", frozenset({".pdf"})),
    (b"\x89PNG\r\n\x1a\n", frozenset({".png"})),
    (b"\xff\xd8\xff", frozenset({".jpg", ".jpeg"})),
    (b"GIF8", frozenset({".gif"})),
    (b"PK\x03\x04", frozenset({".docx", ".xlsx"})),
    # `.msg` shares the OLE compound-document header with the pre-2007 Office
    # formats, so the extension is the only thing that distinguishes them — the same
    # limitation `PK` imposes on `.docx` versus `.xlsx` two lines above. The check
    # still earns its keep: it catches a `.msg` that is really a PDF or a zip.
    (b"\xd0\xcf\x11\xe0", frozenset({".doc", ".xls", ".msg"})),
)

#: Executable and script signatures, refused whatever the extension claims.
_FORBIDDEN_SIGNATURES: tuple[bytes, ...] = (b"MZ", b"\x7fELF", b"#!/", b"<?php")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


class DocumentValidationError(ValidationError):
    """The upload was refused. Carries a sentence fit to show the officer."""

    code = "document_rejected"


def sanitise_filename(filename: str, *, fallback: str = "attachment") -> str:
    """A filename safe to store and safe to echo back.

    Path separators, control characters and leading dots are removed, and the
    result is length-bounded. The stored object key never uses this — that is a
    UUID — so this is about what is displayed and downloaded, not about where the
    bytes land.
    """
    name = unicodedata.normalize("NFKD", filename or "").encode("ascii", "ignore").decode()
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = _SAFE_NAME_RE.sub("_", name).lstrip(". ")
    if not name:
        name = fallback

    stem, extension = os.path.splitext(name)
    return f"{stem[:120] or fallback}{extension[:12].lower()}"


def validate_upload(
    filename: str,
    content: bytes,
    *,
    max_bytes: int,
    declared_content_type: str | None = None,
) -> tuple[str, str]:
    """Check an attachment and return its safe name and resolved content type.

    The content type is resolved from the extension rather than taken from the
    client: a browser's `Content-Type` is a hint, and the one thing it must not be
    allowed to do is decide which extractor runs.
    """
    if not content:
        raise DocumentValidationError("That file is empty.")
    if len(content) > max_bytes:
        limit_mb = max_bytes / 1_000_000
        raise DocumentValidationError(
            f"That file is larger than the {limit_mb:.0f} MB limit for claim attachments."
        )

    safe_name = sanitise_filename(filename)
    extension = os.path.splitext(safe_name)[1].lower()

    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise DocumentValidationError(
            f"{safe_name} is not a file type claims documents are accepted in. Allowed: {allowed}."
        )

    header = content[:16]
    if any(header.startswith(signature) for signature in _FORBIDDEN_SIGNATURES):
        raise DocumentValidationError(f"{safe_name} looks like a program rather than a document.")

    for signature, extensions in _SIGNATURES:
        if extension in extensions and not content.startswith(signature):
            raise DocumentValidationError(
                f"{safe_name} does not contain the file type its name claims."
            )

    resolved = ALLOWED_EXTENSIONS[extension]
    if (
        declared_content_type
        and declared_content_type.startswith("text/")
        and extension
        in {
            ".txt",
            ".md",
            ".csv",
        }
    ):
        resolved = declared_content_type.split(";")[0].strip()

    return safe_name, resolved


def document_kind(content_type: str) -> str:
    """The broad kind the UI draws a glyph for."""
    if content_type.startswith("image/"):
        return "photo"
    if content_type in {"text/csv", "application/vnd.ms-excel"} or "spreadsheet" in content_type:
        return "spreadsheet"
    if content_type in {"message/rfc822", "application/vnd.ms-outlook"}:
        return "email"
    return "document"
