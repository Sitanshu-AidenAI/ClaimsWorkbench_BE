"""One reader per document format.

`app/services/documents/text.py` is the registry that dispatches to these; nothing
in here knows the registry exists, so a new format is a module plus one
registration line rather than a change to any existing reader.

Every reader in this package obeys the same three rules:

1. **It never raises.** A document that cannot be read is a normal state of a claims
   desk, reported as a status and a sentence the officer can act on. The registry
   catches anything that escapes anyway, but a reader that relies on that has thrown
   away its chance to say something useful about *why*.
2. **It produces pages.** Per-page text is what makes a citation clickable. Where a
   format genuinely has no pages, the reader says so with `page_count=None` rather
   than inventing them.
3. **It renders tables through `tables.render_table`.** A cell's meaning lives in its
   row and column, and both readers of this text — a language model and a regex —
   see only what the layout was flattened into.
"""

from __future__ import annotations

from app.services.documents.formats.mail import extract_eml, extract_msg
from app.services.documents.formats.pdf import extract_pdf
from app.services.documents.formats.spreadsheet import extract_xlsx
from app.services.documents.formats.tables import render_table
from app.services.documents.formats.word import extract_docx

__all__ = [
    "extract_docx",
    "extract_eml",
    "extract_msg",
    "extract_pdf",
    "extract_xlsx",
    "render_table",
]
