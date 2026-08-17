"""A deterministic embedding provider.

Distinct texts must produce distinct vectors, and similar texts must produce similar
ones, or every test that asserts anything about retrieval order passes for the wrong
reason. So the vector is derived from the text's *words* rather than from a hash of the
whole string: two passages sharing most of their vocabulary land close together, which
is the one property retrieval depends on.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from app.services.ai.base import AIProviderError

_WORD_RE = re.compile(r"[0-9a-z]+")


class FakeEmbeddingProvider:
    """A bag-of-words vector, hashed into a fixed number of dimensions.

    The classic hashing trick: each word increments one dimension chosen by its hash.
    Cosine similarity then behaves the way a real embedding roughly does for the thing
    these tests measure — shared vocabulary means a higher score — with no model, no
    network and no non-determinism.
    """

    name = "fake"

    def __init__(self, *, dimension: int = 64) -> None:
        self._dimension = dimension
        #: Every call, so a test can assert that a re-run embedded nothing.
        self.document_calls = 0
        self.query_calls = 0
        self.embedded_texts: list[str] = []

    @property
    def model(self) -> str:
        return "fake-embedding"

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        self.embedded_texts.extend(texts)
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)

    async def aclose(self) -> None:
        return None

    def _vector(self, text: str) -> list[float]:
        buckets = [0.0] * self._dimension
        for word in _WORD_RE.findall(text.lower()):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            buckets[digest[0] % self._dimension] += 1.0

        norm = math.sqrt(sum(value * value for value in buckets))
        if norm == 0.0:
            # A passage of pure punctuation. A unit vector in one dimension rather than
            # zeros, so it is still comparable and never divides by zero downstream.
            buckets[0] = 1.0
            return buckets
        return [value / norm for value in buckets]


class FailingEmbeddingProvider:
    """Raises on every call, the way an unreachable provider does."""

    name = "failing"

    def __init__(self, *, retryable: bool = True, dimension: int = 64) -> None:
        self._retryable = retryable
        self._dimension = dimension
        self.calls = 0

    @property
    def model(self) -> str:
        return "failing-embedding"

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        self.calls += 1
        raise AIProviderError("The embedding provider is unreachable.", retryable=self._retryable)

    async def embed_query(self, text: str) -> list[float]:
        del text
        self.calls += 1
        raise AIProviderError("The embedding provider is unreachable.", retryable=self._retryable)

    async def aclose(self) -> None:
        return None


__all__ = ["FailingEmbeddingProvider", "FakeEmbeddingProvider"]
