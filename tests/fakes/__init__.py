"""Test doubles for the collaborators the pipeline reaches out to.

A package rather than fixtures in `conftest.py` so the unit and integration suites can
import the same doubles. Each one is a real implementation of its Protocol, not a mock:
the point is to run the actual pipeline code, so the doubles have to behave.

`FakeEmbeddingProvider` in particular is **deterministic and non-degenerate** —
distinct texts get distinct, comparable vectors. The IIF pipeline's equivalent
(`IIF_VECTOR_STUB_EMBEDDINGS`) returns zero vectors, which makes every passage
identical and every assertion about retrieval order vacuously true. A test that cannot
fail is worse than no test.
"""

from __future__ import annotations

from tests.fakes.ai import StubProvider
from tests.fakes.embedding import FailingEmbeddingProvider, FakeEmbeddingProvider
from tests.fakes.vectors import FakeVectorStore

__all__ = [
    "FailingEmbeddingProvider",
    "FakeEmbeddingProvider",
    "FakeVectorStore",
    "StubProvider",
]
