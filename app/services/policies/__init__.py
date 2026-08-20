"""The policy library: ingesting wordings, and matching a notice against them.

The claim side of this product reads *one case's* documents to find the passage a
field was read from. This package reads *the whole book* to answer a different
question: which policy is this loss under. Same primitives, opposite shape — one
corpus and many queries there, one query and many corpora here — which is why the
stages live in their own package rather than as flags on the FNOL ones.

| Module       | Job |
|--------------|-----|
| `vectors`    | the policy collection in Qdrant, behind a Protocol |
| `ingestion`  | one uploaded PDF end to end: read, chunk, embed, upsert |
| `library`    | accept an upload, store it, and hand it to the worker |
| `retrieval`  | a query to ranked policy passages, hybrid and degrading |
| `matching`   | an FNOL case to ranked policies, with the reasons |
| `runner`     | the entry points the Celery worker calls |

What is deliberately **reused rather than reimplemented**:

* `app/services/documents/` for validation, storage and PDF reading. A policy
  wording is a PDF and the reader that reads a loss notice reads it correctly.
* `app/services/intelligence/chunking.py` — pure, page-aware, and holds the
  content-equals-slice invariant an excerpt's page number depends on.
* `app/services/intelligence/embedding.py` — one embedding provider for the whole
  deployment. Two would be two vector spaces, and a policy library embedded by a
  different model than the query that searches it does not match anything.

What is **not** reused, and why: the vector store. `VectorStore` in
`app/services/intelligence/vectors.py` takes `case_id` as a required argument on
every read and write, because every claim passage belongs to a case. A policy
belongs to none, so satisfying that Protocol would mean inventing a case id and
filtering on a lie.
"""

from __future__ import annotations

__all__: list[str] = []
