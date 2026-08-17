"""Document intelligence: passages, vectors, retrieval.

The stages between "a file is stored" and "a field on the case can say where its
value came from". Deliberately free of FNOL vocabulary in everything except
`fields.py`, which is the one module that knows what a claim notification contains —
the same boundary `app/services/documents/` already draws, and for the same reason:
the claim workbench will want to search a document without inheriting intake's
concepts.

The stages, and what each is for:

| Module        | Job |
|---------------|-----|
| `chunking`    | pure: split text into passages with exact offsets |
| `embedding`   | text to vectors, behind a Protocol, `None` when unconfigured |
| `vectors`     | Qdrant, behind a Protocol, `None` when unconfigured |
| `indexing`    | one document end to end, isolated and idempotent |
| `retrieval`   | a query to ranked passages; degrades to keyword, then to nothing |
| `fields`      | which retrieval queries answer which FNOL field |
| `highlight`   | a passage to the rectangles that draw it on a page |

Everything except `chunking` is optional at runtime. With no embedding provider the
passages are still written and still searchable by keyword; with no keyword match
either, extraction reads the whole corpus exactly as it did before this package
existed. That is the same posture `app/services/ai/factory.py` takes, and it is what
keeps a demo environment and an air-gapped deployment working.
"""

from __future__ import annotations

from app.services.intelligence.chunking import Chunk, Region, chunk_document, content_hash

__all__ = ["Chunk", "Region", "chunk_document", "content_hash"]
