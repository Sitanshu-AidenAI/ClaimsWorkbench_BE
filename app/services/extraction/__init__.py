"""Configurable, retrieval-backed field extraction.

What to read out of an intake is configuration — a dataset of fields, each with a
description written in the vocabulary a document uses — and this package is what
runs it. It knows fields, passages and confidence, and nothing about insurance:
mapping a dataset's answers onto a claim record is `app.services.fnol.adapter`.

| Module | Job |
|---|---|
| `schema.py` | `DatasetSchema` / `FieldSpec`, and typed coercion that never discards a value. |
| `registry.py` | Loading a dataset from the database, and seeding the ones that ship. |
| `prompts.py` | The one prompt every dataset uses, and the shape a model answers in. |
| `engine.py` | Retrieve per field, batch the fields, ask once per batch, write the values. |
| `locate.py` | Finding a value's text in a document, for highlighting. |
"""

from __future__ import annotations

from app.services.extraction.engine import RunOutcome, SchemaExtractionEngine
from app.services.extraction.registry import BUILTIN_FNOL_KEY, seed_builtin_schemas, to_dataset
from app.services.extraction.schema import DatasetSchema, FieldSpec

__all__ = [
    "BUILTIN_FNOL_KEY",
    "DatasetSchema",
    "FieldSpec",
    "RunOutcome",
    "SchemaExtractionEngine",
    "seed_builtin_schemas",
    "to_dataset",
]
