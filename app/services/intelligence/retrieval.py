"""Finding the passages that answer a question.

Two backends, fused. The semantic side is the vector store; the keyword side is
Postgres full text over the same passages. Fusion is **Reciprocal Rank Fusion** —
IIF's approach, and the right one here — because the two backends produce scores on
incomparable scales: a cosine similarity of 0.82 and a `ts_rank` of 0.19 cannot be
averaged, but their *ranks* can be combined.

Both sides are optional and the degradation is the point:

| Available            | Behaviour |
|----------------------|-----------|
| both                 | RRF over the two rankings |
| keyword only         | `ts_rank` order — a real answer, not a stub |
| semantic only        | cosine order (the keyword query raised, or matched nothing) |
| neither              | empty, and the caller sends the whole corpus as before |

One thing added that IIF's retrieval lacks: **a score floor**. Top-k with no
threshold always returns k results, so a case whose documents mention nothing like
the query still yields six confident-looking passages about nothing — and those
passages then become the evidence a field is extracted from. The floor is applied to
the fused score, after normalisation, so it means the same thing whichever backends
were available.

Deliberately not built: reranking, MMR, query rewriting, multi-query. All are real
improvements and none of them change this module's interface, which is the point of
keeping `search()` returning scored, ordered hits.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from app.core.config import DocumentIntelligenceSettings, settings
from app.core.logging import get_logger
from app.models.fnol import FNOLDocumentChunk
from app.repositories.chunks import DocumentChunkRepository
from app.services.ai.base import AIProviderError
from app.services.intelligence.embedding import EmbeddingProvider
from app.services.intelligence.vectors import VectorStore

logger = get_logger(__name__)

#: RRF's rank offset. 60 is the value the original paper uses and the one IIF uses;
#: it flattens the difference between rank 1 and rank 2 enough that a single backend's
#: strong opinion cannot dominate the other's.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One passage that matched, and how it was found."""

    chunk: FNOLDocumentChunk
    score: float
    semantic_score: float | None = None
    keyword_score: float | None = None

    @property
    def chunk_ref(self) -> str:
        return self.chunk.chunk_ref


@dataclass(slots=True)
class RetrievalResult:
    """What one search returned, and how it was answered.

    `strategy` and `degraded` are recorded rather than inferred so the analysis row
    on the case can answer "why did the model read that" months later — including the
    case where the answer is "the vector store was down and it read keyword matches".
    """

    hits: list[RetrievedChunk] = field(default_factory=list)
    strategy: str = "none"
    degraded: bool = False
    chunks_available: int = 0

    def __bool__(self) -> bool:
        return bool(self.hits)


class RetrievalService:
    """Ranked passages for a query, within one case."""

    def __init__(
        self,
        chunks: DocumentChunkRepository,
        *,
        embeddings: EmbeddingProvider | None = None,
        vectors: VectorStore | None = None,
        config: DocumentIntelligenceSettings | None = None,
    ) -> None:
        self._chunks = chunks
        self._embeddings = embeddings
        self._vectors = vectors
        self._config = config or settings.docint

    @property
    def chunks(self) -> DocumentChunkRepository:
        """The passage store this service searches.

        Exposed so a caller that needs specific passages rather than a ranked
        answer — the notification body, which is always included regardless of
        score — can read them without a second repository of its own.
        """
        return self._chunks

    @property
    def embeddings(self) -> EmbeddingProvider | None:
        """The provider, or `None`. Exposed so a caller that can cache a query's
        vector across runs — a dataset field's query never changes — can compute
        it once and hand it back through `search(vector=…)`."""
        return self._embeddings

    async def search(
        self,
        case_id: uuid.UUID,
        query: str,
        *,
        limit: int | None = None,
        document_id: uuid.UUID | None = None,
        min_score: float | None = None,
        vector: list[float] | None = None,
    ) -> RetrievalResult:
        """The passages in `case_id` that best answer `query`.

        `vector` is the query's embedding when the caller already has it. A
        dataset field's query is fixed for the life of that field, so embedding
        it on every run of every notice is a network round trip bought back for
        the price of a cached column.
        """
        if not self._config.retrieval_enabled or not query.strip():
            return RetrievalResult()

        top_k = limit or self._config.retrieval_top_k
        floor = self._config.retrieval_min_score if min_score is None else min_score
        available = await self._chunks.count_for_case(case_id)
        if available == 0:
            return RetrievalResult(chunks_available=0)

        semantic, semantic_failed = await self._semantic(
            case_id, query, limit=top_k * 2, document_id=document_id, vector=vector
        )
        keyword = await self._keyword(case_id, query, limit=top_k * 2, document_id=document_id)

        if not semantic and not keyword:
            return RetrievalResult(
                strategy="none", degraded=semantic_failed, chunks_available=available
            )

        strategy = "hybrid-rrf"
        if not semantic:
            strategy = "keyword"
        elif not keyword:
            strategy = "semantic"

        fused = await self._fuse(semantic, keyword, floor=floor)
        hits = fused[:top_k]
        return RetrievalResult(
            hits=hits,
            strategy=strategy,
            # Degraded means "a backend that should have answered did not", which is
            # not the same as a backend simply not being configured.
            degraded=semantic_failed,
            chunks_available=available,
        )

    async def search_many(
        self,
        case_id: uuid.UUID,
        queries: list[str],
        *,
        limit: int | None = None,
        vectors: list[list[float] | None] | None = None,
    ) -> list[RetrievalResult]:
        """Run several queries concurrently, bounded.

        Bounded because a dataset has tens of fields and each is a network round trip
        to the embedding provider; unbounded concurrency here is a burst that gets a
        deployment rate-limited on exactly the request that matters.

        `vectors`, when given, is positional against `queries` and supplies each
        query's embedding — `None` in a slot means "embed this one".
        """
        gate = asyncio.Semaphore(max(1, self._config.retrieval_concurrency))
        supplied = vectors or [None] * len(queries)

        async def one(query: str, vector: list[float] | None) -> RetrievalResult:
            async with gate:
                return await self.search(case_id, query, limit=limit, vector=vector)

        return list(
            await asyncio.gather(
                *(one(query, vector) for query, vector in zip(queries, supplied, strict=True))
            )
        )

    # -- Backends ------------------------------------------------------------

    async def _semantic(
        self,
        case_id: uuid.UUID,
        query: str,
        *,
        limit: int,
        document_id: uuid.UUID | None,
        vector: list[float] | None = None,
    ) -> tuple[list[tuple[str, float]], bool]:
        """`(hits, failed)`. `failed` distinguishes "unavailable" from "unconfigured"."""
        if self._embeddings is None or self._vectors is None:
            return [], False

        try:
            embedded = vector if vector is not None else await self._embeddings.embed_query(query)
            found = await self._vectors.search(
                embedded, case_id=case_id, limit=limit, document_id=document_id
            )
        except (AIProviderError, Exception) as exc:
            # Broad on purpose. A vector store client raises its own exception
            # hierarchy, and an unreachable index must degrade this search to keyword
            # rather than fail the extraction that asked for it. The failure is
            # reported upward as `degraded`, not swallowed.
            logger.info("retrieval_semantic_unavailable", error=type(exc).__name__)
            return [], True

        return [(hit.chunk_ref, hit.score) for hit in found], False

    async def _keyword(
        self, case_id: uuid.UUID, query: str, *, limit: int, document_id: uuid.UUID | None
    ) -> list[tuple[str, float]]:
        try:
            rows = await self._chunks.keyword_search(
                case_id, query, limit=limit, document_id=document_id
            )
        except Exception as exc:
            logger.info("retrieval_keyword_unavailable", error=type(exc).__name__)
            return []
        return [(row.chunk_ref, score) for row, score in rows]

    # -- Fusion --------------------------------------------------------------

    async def _fuse(
        self,
        semantic: list[tuple[str, float]],
        keyword: list[tuple[str, float]],
        *,
        floor: float,
    ) -> list[RetrievedChunk]:
        """Combine two rankings, then filter on the fused score."""
        scores: dict[str, float] = {}
        semantic_by_ref = dict(semantic)
        keyword_by_ref = dict(keyword)

        for rank, (ref, _) in enumerate(semantic, start=1):
            scores[ref] = scores.get(ref, 0.0) + self._config.retrieval_semantic_weight / (
                RRF_K + rank
            )
        for rank, (ref, _) in enumerate(keyword, start=1):
            scores[ref] = scores.get(ref, 0.0) + self._config.retrieval_keyword_weight / (
                RRF_K + rank
            )

        if not scores:
            return []

        # Normalised to 0..1 against the best result, so the configured floor means
        # the same thing whether one backend answered or both did. An RRF score is
        # otherwise unitless and its range depends on how many backends contributed.
        best = max(scores.values())
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)

        # One query for every hit, whichever backend found it. This is the hydration
        # the payload-holds-no-text decision costs, and it is one indexed lookup.
        refs = [ref for ref, _ in ordered]
        rows = {row.chunk_ref: row for row in await self._chunks.list_by_refs(refs)}

        hits: list[RetrievedChunk] = []
        for ref, raw in ordered:
            row = rows.get(ref)
            if row is None:
                # A hit the vector store knows and Postgres does not: a point left
                # behind by a deleted document. Skipped rather than surfaced, and the
                # next index run removes it.
                logger.info("retrieval_hit_without_passage", chunk_ref=ref)
                continue
            normalised = raw / best if best else 0.0
            if normalised < floor:
                continue
            hits.append(
                RetrievedChunk(
                    chunk=row,
                    score=round(normalised, 6),
                    semantic_score=semantic_by_ref.get(ref),
                    keyword_score=keyword_by_ref.get(ref),
                )
            )
        return hits


__all__ = ["RRF_K", "RetrievalResult", "RetrievalService", "RetrievedChunk"]
