"""Turning passages into vectors.

A separate Protocol from `AIProvider`, and the reason is in that module's own
docstring: it promises exactly one thing — a schema-validated object — and calls that
promise "the whole defence against a hallucinated policy number reaching the
database". A vector has no schema to validate, so putting it behind the same contract
would make the contract mean two different things.

Three concrete costs of the alternative, for the record:

* `AIProvider` is `@runtime_checkable`, so adding a method changes what `isinstance`
  means for every existing implementor — including the `StubProvider` that half the
  AI tests are built on.
* A deployment configures them separately. A carrier may route chat through an
  internal gateway and embeddings somewhere else, or have a chat model and no
  embedding model at all. One object from one factory cannot express that.
* The failure modes differ. An embedding call that returns the wrong *number* of
  vectors is a silent catastrophe — every passage after the gap is mis-attributed —
  and that is a check with no analogue on the structured-output path.

Two methods rather than one because OpenAI-family models are symmetric but
e5/bge-family models want different prefixes for a passage and a query, and the
batching differs regardless.

Built on `HttpClient` so the project's retry policy applies. The IIF pipeline calls
`AsyncOpenAI().embeddings.create` directly with no retry wrapper at all — the one
un-retried outbound call in an otherwise careful codebase — and this is where that
would have been inherited.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from app.core.config import DocumentIntelligenceSettings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.http import HttpClient
from app.services.ai.base import AIProviderError

logger = get_logger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Text in, vectors out."""

    name: str

    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in order.

        Implementations must return exactly `len(texts)` vectors of exactly
        `dimension` floats, and must verify it rather than trust it. A provider that
        returns fewer, or reorders them, silently mis-attributes every passage after
        the gap — a citation that points at the wrong page of the wrong document,
        with nothing anywhere to indicate that anything went wrong.
        """

    async def embed_query(self, text: str) -> list[float]:
        """One vector for a search query."""

    async def aclose(self) -> None: ...


class OpenAIEmbeddingProvider:
    """An embeddings client for OpenAI-compatible endpoints."""

    name = "openai"

    def __init__(self, config: DocumentIntelligenceSettings | None = None) -> None:
        self._config = config or settings.docint
        key = self._config.resolved_embedding_api_key
        if not key:
            raise ValueError("OpenAIEmbeddingProvider requires an API key.")

        self._client = HttpClient(
            self._config.resolved_embedding_base_url,
            timeout=httpx.Timeout(self._config.embedding_timeout_seconds, connect=10.0),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            max_attempts=self._config.embedding_max_attempts,
        )

    @property
    def model(self) -> str:
        return self._config.embedding_model

    @property
    def dimension(self) -> int:
        return self._config.embedding_dimension

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        batch = max(1, self._config.embedding_batch_size)
        for start in range(0, len(texts), batch):
            window = list(texts[start : start + batch])
            vectors.extend(await self._embed(window))

        if len(vectors) != len(texts):
            raise AIProviderError(
                f"The embedding provider returned {len(vectors)} vectors for {len(texts)} passages."
            )
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text])
        return vectors[0]

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts}
        try:
            response = await self._client.post("/embeddings", json=payload)
        except ExternalServiceError as exc:
            # Logged without the input: passages are claim material.
            logger.error("embedding_request_failed", provider=self.name, model=self.model)
            raise AIProviderError(str(exc), retryable=True) from exc

        if response.status_code >= 400:
            logger.error(
                "embedding_response_rejected",
                provider=self.name,
                status_code=response.status_code,
            )
            raise AIProviderError(
                f"The embedding provider returned {response.status_code}.",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )

        try:
            body = response.json()
            rows = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AIProviderError(
                "The embedding provider returned an unrecognised envelope."
            ) from exc

        if len(rows) != len(texts):
            # The failure this check exists for. Silently zipping a short response
            # against the input shifts every passage's vector by one.
            raise AIProviderError(
                f"The embedding provider returned {len(rows)} vectors for {len(texts)} inputs."
            )

        # `index` is authoritative over position: the API documents that the order is
        # not guaranteed, and trusting position is the same bug one layer down.
        ordered: list[list[float]] = [[] for _ in texts]
        for row in rows:
            position = int(row.get("index", -1))
            vector = row.get("embedding")
            if not isinstance(vector, list) or not (0 <= position < len(texts)):
                raise AIProviderError("The embedding provider returned a malformed vector.")
            if len(vector) != self.dimension:
                raise AIProviderError(
                    f"The embedding provider returned {len(vector)}-dimensional vectors; "
                    f"this deployment is configured for {self.dimension}."
                )
            ordered[position] = [float(value) for value in vector]

        if any(not vector for vector in ordered):
            raise AIProviderError("The embedding provider did not return every vector.")

        usage = (response.json().get("usage") or {}) if response.content else {}
        logger.info(
            "embedding_request_completed",
            provider=self.name,
            model=self.model,
            count=len(texts),
            prompt_tokens=usage.get("prompt_tokens"),
        )
        return ordered


# --- factory ------------------------------------------------------------------
#
# Same shape as `app/services/ai/factory.py`: built once per process, and `None` is a
# legitimate answer rather than a missing one. `_built` is a separate flag rather than
# that module's `_UNSET` sentinel in the union, because a sentinel of type `object`
# sharing a variable with the provider does not narrow — the resulting
# `await provider.aclose()` needs a `type: ignore` to compile, and an ignore on the
# one line that touches a network resource is the wrong place to stop type-checking.

_provider: EmbeddingProvider | None = None
_built = False


def get_embedding_provider() -> EmbeddingProvider | None:
    """The process-wide provider, or `None` when embeddings are not configured.

    `None` is a first-class answer, not an error. The caller writes passages without
    vectors and retrieval falls back to keyword search over the same passages.
    """
    global _provider, _built
    if not _built:
        if not settings.docint.embeddings_configured:
            logger.info("embedding_provider_not_configured")
            _provider = None
        else:
            _provider = OpenAIEmbeddingProvider()
            logger.info(
                "embedding_provider_ready",
                model=settings.docint.embedding_model,
                dimension=settings.docint.embedding_dimension,
            )
        _built = True
    return _provider


def set_embedding_provider(provider: EmbeddingProvider | None) -> None:
    """Override the provider. The seam tests use."""
    global _provider, _built
    _provider = provider
    _built = True


def reset_embedding_provider() -> None:
    global _provider, _built
    _provider = None
    _built = False


async def close_embedding_provider() -> None:
    """Release the transport. Called from the application lifespan."""
    global _provider, _built
    current, _provider, _built = _provider, None, False
    if current is not None:
        await current.aclose()


__all__ = [
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "close_embedding_provider",
    "get_embedding_provider",
    "reset_embedding_provider",
    "set_embedding_provider",
]
