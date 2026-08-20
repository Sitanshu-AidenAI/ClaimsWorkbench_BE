"""Finding the policy passages that answer a notice.

Two backends fused by **Reciprocal Rank Fusion**, exactly as
`app/services/intelligence/retrieval.py` does it and for the same reason: a cosine
similarity of 0.82 and a `ts_rank` of 0.19 cannot be averaged, but their *ranks* can
be combined. Both sides are optional and the degradation is the point.

| Available      | Behaviour |
|----------------|-----------|
| both           | RRF over the two rankings |
| keyword only   | `ts_rank` order — on this corpus a genuinely good answer, not a stub |
| semantic only  | cosine order |
| neither        | empty, and the caller says so rather than guessing |

Keyword-only deserves its own note here, because it is a *better* fallback on policy
wordings than on claim documents. The tokens that identify a policy — the number, the
insured's name, a form number, a postcode — are rare, and rare tokens are what an
inverted index is best at. A deployment with no embedding provider gets policy
matching that works; it loses the ability to match a loss *description* against a
clause that never uses the notice's words, which is what the dense side buys.

## What this module adds over the claim-side one: facets

The claim side searches one case's passages with one query per field. Here there is
one notice and a whole library, and the notice asks several different questions at
once — who is insured, where is the site, what happened, what cover is wanted. A
single concatenated query embeds to the *average* of those questions, and the nearest
passage to an average is frequently the nearest passage to none of them.

So `search_for_notice` issues one query per `MatchFacet`, concurrently and bounded,
and reports which facets each document answered. That is both better retrieval and a
better explanation: "this wording answered your cause of loss" is a sentence, where a
fused score is a number.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.config import PolicyLibrarySettings, settings
from app.core.logging import get_logger
from app.domain.policy_matching import MatchFacet, NoticeQuery
from app.models.policy_document import PolicyDocumentChunk
from app.repositories.policy_document import PolicyDocumentRepository
from app.services.intelligence.embedding import EmbeddingProvider
from app.services.policies.vectors import PolicyVectorStore

logger = get_logger(__name__)

#: RRF's rank offset. 60 is the original paper's value and the one the claim-side
#: fusion uses; keeping them equal means a passage score means the same thing in both
#: halves of the product.
RRF_K = 60


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One policy passage that matched, and how it was found."""

    chunk: PolicyDocumentChunk
    score: float
    facet: MatchFacet | None = None
    semantic_score: float | None = None
    keyword_score: float | None = None

    @property
    def chunk_ref(self) -> str:
        return self.chunk.chunk_ref

    @property
    def document_id(self) -> uuid.UUID:
        return self.chunk.policy_document_id


@dataclass(slots=True)
class RetrievalResult:
    """What one search returned, and how it was answered.

    `strategy` and `degraded` are recorded rather than inferred, so a stored match can
    answer "why did it rank that" months later — including the case where the answer
    is "the vector store was down and this is keyword order".
    """

    hits: list[RetrievedChunk] = field(default_factory=list)
    strategy: str = "none"
    degraded: bool = False
    chunks_available: int = 0

    def __bool__(self) -> bool:
        return bool(self.hits)


@dataclass(slots=True)
class FacetedRetrieval:
    """Every facet's hits, merged and grouped by document.

    Grouped here rather than in the matcher because grouping is a retrieval concern:
    the matcher wants "these documents, with these excerpts and these facets", and
    working that out from a flat hit list is the same loop written twice.
    """

    by_document: dict[uuid.UUID, list[RetrievedChunk]] = field(default_factory=dict)
    facets_by_document: dict[uuid.UUID, set[MatchFacet]] = field(default_factory=dict)
    strategy: str = "none"
    degraded: bool = False
    chunks_considered: int = 0
    facets_queried: tuple[MatchFacet, ...] = ()

    @property
    def document_ids(self) -> list[uuid.UUID]:
        return list(self.by_document)


class PolicyRetrievalService:
    """Ranked policy passages for a query, across the library."""

    def __init__(
        self,
        documents: PolicyDocumentRepository,
        *,
        embeddings: EmbeddingProvider | None = None,
        vectors: PolicyVectorStore | None = None,
        config: PolicyLibrarySettings | None = None,
    ) -> None:
        self._documents = documents
        self._embeddings = embeddings
        self._vectors = vectors
        self._config = config or settings.policy_library

    @property
    def documents(self) -> PolicyDocumentRepository:
        """The passage store this service searches, for a caller that needs rows."""
        return self._documents

    # -- One query -----------------------------------------------------------

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        min_score: float | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
        line_of_business: str | None = None,
        facet: MatchFacet | None = None,
        vector: list[float] | None = None,
    ) -> RetrievalResult:
        """The library's passages that best answer `query`.

        `vector` is the query's embedding when the caller already has it, which is how
        `search_for_notice` embeds every facet in one concurrent burst and then walks
        the database serially.
        """
        if not self._config.retrieval_enabled or not query.strip():
            return RetrievalResult()

        top_k = limit or self._config.retrieval_chunk_limit
        floor = self._config.retrieval_min_score if min_score is None else min_score
        available = await self._documents.count_chunks()
        if available == 0:
            return RetrievalResult(chunks_available=0)

        embedded = vector
        failed = False
        if embedded is None:
            embedded, failed = await self._embed(query)

        semantic, search_failed = await self._semantic(
            embedded,
            limit=top_k * 2,
            document_ids=document_ids,
            line_of_business=line_of_business,
        )
        keyword = await self._keyword(query, limit=top_k * 2, document_ids=document_ids)
        degraded = failed or search_failed

        if not semantic and not keyword:
            return RetrievalResult(strategy="none", degraded=degraded, chunks_available=available)

        strategy = "hybrid-rrf"
        if not semantic:
            strategy = "keyword"
        elif not keyword:
            strategy = "semantic"

        ranked = fuse_ranks(
            semantic,
            keyword,
            semantic_weight=self._config.retrieval_semantic_weight,
            keyword_weight=self._config.retrieval_keyword_weight,
            floor=floor,
        )
        hits = await self._hydrate(ranked, semantic, keyword, facet=facet)
        return RetrievalResult(
            hits=hits[:top_k],
            strategy=strategy,
            degraded=degraded,
            chunks_available=available,
        )

    # -- One notice, several facets -----------------------------------------

    async def search_for_notice(
        self,
        notice: NoticeQuery,
        *,
        line_of_business: str | None = None,
        limit_per_facet: int | None = None,
    ) -> FacetedRetrieval:
        """Run the notice's facet queries and group the hits by document.

        **Parallel over the network, serial over the database**, and the split is not
        an optimisation — it is a correctness requirement. An `AsyncSession` is not
        safe for concurrent use: two coroutines awaiting queries on one session raise
        `InvalidRequestError: this session is provisioning a new connection`, and the
        session is left in a state its own `close()` refuses. So the two halves are
        separated by what they talk to:

        1. **Embeddings**, concurrently and bounded. This is the latency that matters —
           one network round trip per facet — and bounded because unbounded is a burst
           that gets a deployment rate-limited on exactly the request that matters.
        2. **Vector-store searches**, concurrently. Its client is its own connection
           pool and has nothing to do with the session.
        3. **Keyword searches and hydration**, serially, on the one session. Cheap:
           they are indexed lookups against a GIN index, and hydration is folded into
           a *single* query across every facet rather than one per facet.
        """
        queries = build_facet_queries(notice)
        merged = FacetedRetrieval(facets_queried=tuple(facet for facet, _ in queries))
        if not queries:
            return merged

        available = await self._documents.count_chunks()
        if available == 0:
            return merged

        per_facet = limit_per_facet or self._config.retrieval_chunk_limit
        floor = self._config.retrieval_min_score

        # Phase 1 and 2: everything that is a network call, concurrently.
        vectors, embed_failed = await self._embed_many([query for _, query in queries])
        semantic_by_facet, search_failed = await self._semantic_many(
            vectors,
            limit=per_facet * 2,
            line_of_business=line_of_business,
        )
        merged.degraded = embed_failed or search_failed

        # Phase 3: the database, one facet at a time.
        ranked_by_facet: dict[MatchFacet, list[tuple[str, float]]] = {}
        semantic_lookup: dict[str, float] = {}
        keyword_lookup: dict[str, float] = {}
        strategies: set[str] = set()

        for index, (facet, query) in enumerate(queries):
            semantic = semantic_by_facet[index]
            keyword = await self._keyword(query, limit=per_facet * 2, document_ids=None)
            semantic_lookup.update(dict(semantic))
            keyword_lookup.update(dict(keyword))

            if not semantic and not keyword:
                strategies.add("none")
                continue
            if not semantic:
                strategies.add("keyword")
            elif not keyword:
                strategies.add("semantic")
            else:
                strategies.add("hybrid-rrf")

            ranked_by_facet[facet] = fuse_ranks(
                semantic,
                keyword,
                semantic_weight=self._config.retrieval_semantic_weight,
                keyword_weight=self._config.retrieval_keyword_weight,
                floor=floor,
            )[:per_facet]

        # One hydration query for every facet's hits together. Four queries here would
        # be three round trips bought for nothing: the refs are known and the lookup is
        # on a unique index.
        refs = {ref for ranked in ranked_by_facet.values() for ref, _ in ranked}
        rows = {
            row.chunk_ref: row for row in await self._documents.list_chunks_by_refs(sorted(refs))
        }

        for facet, ranked in ranked_by_facet.items():
            for ref, score in ranked:
                row = rows.get(ref)
                if row is None:
                    # A hit the vector store knows and Postgres does not: a point left
                    # behind by a deleted document. Skipped rather than surfaced, and
                    # the next ingest run removes it.
                    logger.info("policy_retrieval_hit_without_passage", chunk_ref=ref)
                    continue
                merged.chunks_considered += 1
                merged.by_document.setdefault(row.policy_document_id, []).append(
                    RetrievedChunk(
                        chunk=row,
                        score=score,
                        facet=facet,
                        semantic_score=semantic_lookup.get(ref),
                        keyword_score=keyword_lookup.get(ref),
                    )
                )
                merged.facets_by_document.setdefault(row.policy_document_id, set()).add(facet)

        # The strongest strategy any facet achieved. A run where one facet degraded to
        # keyword and three did not is a hybrid run with a note, not a keyword run.
        for candidate in ("hybrid-rrf", "semantic", "keyword", "none"):
            if candidate in strategies:
                merged.strategy = candidate
                break

        # Keep the best passages per document, best first. The cap is what stops one
        # long wording — a schedule of values across forty pages — from contributing
        # forty excerpts and drowning the card it is meant to explain.
        keep = max(1, self._config.excerpts_per_policy)
        for document_id, hits in merged.by_document.items():
            merged.by_document[document_id] = _dedupe(hits)[:keep]

        return merged

    # -- Backends ------------------------------------------------------------

    async def _embed(self, query: str) -> tuple[list[float] | None, bool]:
        """`(vector, failed)`. `failed` distinguishes "unavailable" from "unconfigured"."""
        if self._embeddings is None or self._vectors is None:
            return None, False
        try:
            return await self._embeddings.embed_query(query), False
        except Exception as exc:
            logger.info("policy_retrieval_embed_unavailable", error=type(exc).__name__)
            return None, True

    async def _embed_many(self, queries: Sequence[str]) -> tuple[list[list[float] | None], bool]:
        """Embed every facet query concurrently, bounded.

        One failure does not lose the others: a slot that could not be embedded holds
        `None` and that facet degrades to keyword, which is a per-facet degradation
        rather than a whole-match one.
        """
        if self._embeddings is None or self._vectors is None:
            return [None] * len(queries), False

        gate = asyncio.Semaphore(max(1, self._config.retrieval_concurrency))

        async def one(query: str) -> tuple[list[float] | None, bool]:
            async with gate:
                return await self._embed(query)

        outcomes = await asyncio.gather(*(one(query) for query in queries))
        return [vector for vector, _ in outcomes], any(failed for _, failed in outcomes)

    async def _semantic(
        self,
        vector: list[float] | None,
        *,
        limit: int,
        document_ids: Sequence[uuid.UUID] | None,
        line_of_business: str | None,
    ) -> tuple[list[tuple[str, float]], bool]:
        """`(hits, failed)`. Empty and not-failed means no vector store is configured."""
        if vector is None or self._vectors is None:
            return [], False

        try:
            found = await self._vectors.search(
                vector,
                limit=limit,
                document_ids=list(document_ids) if document_ids else None,
                line_of_business=line_of_business,
            )
        except Exception as exc:
            # Broad on purpose. A vector-store client raises its own exception
            # hierarchy, and an unreachable index must degrade this search to keyword
            # rather than fail the match that asked for it. Reported upward as
            # `degraded`, not swallowed.
            logger.info("policy_retrieval_semantic_unavailable", error=type(exc).__name__)
            return [], True

        return [(hit.chunk_ref, hit.score) for hit in found], False

    async def _semantic_many(
        self,
        vectors: Sequence[list[float] | None],
        *,
        limit: int,
        line_of_business: str | None,
    ) -> tuple[list[list[tuple[str, float]]], bool]:
        """Search the vector store for every facet concurrently.

        Safe to run concurrently, unlike the keyword half: the store has its own
        client and its own pool and knows nothing about the database session.
        """
        if self._vectors is None:
            return [[] for _ in vectors], False

        gate = asyncio.Semaphore(max(1, self._config.retrieval_concurrency))

        async def one(vector: list[float] | None) -> tuple[list[tuple[str, float]], bool]:
            async with gate:
                return await self._semantic(
                    vector, limit=limit, document_ids=None, line_of_business=line_of_business
                )

        outcomes = await asyncio.gather(*(one(vector) for vector in vectors))
        return [hits for hits, _ in outcomes], any(failed for _, failed in outcomes)

    async def _keyword(
        self, query: str, *, limit: int, document_ids: Sequence[uuid.UUID] | None
    ) -> list[tuple[str, float]]:
        try:
            rows = await self._documents.keyword_search(
                query, limit=limit, document_ids=document_ids
            )
        except Exception as exc:
            logger.info("policy_retrieval_keyword_unavailable", error=type(exc).__name__)
            return []
        return [(row.chunk_ref, score) for row, score in rows]

    # -- Hydration -----------------------------------------------------------

    async def _hydrate(
        self,
        ranked: list[tuple[str, float]],
        semantic: list[tuple[str, float]],
        keyword: list[tuple[str, float]],
        *,
        facet: MatchFacet | None,
    ) -> list[RetrievedChunk]:
        """Attach the passage rows to a fused ranking. One query for the whole list."""
        if not ranked:
            return []

        semantic_by_ref = dict(semantic)
        keyword_by_ref = dict(keyword)
        rows = {
            row.chunk_ref: row
            for row in await self._documents.list_chunks_by_refs([ref for ref, _ in ranked])
        }

        hits: list[RetrievedChunk] = []
        for ref, score in ranked:
            row = rows.get(ref)
            if row is None:
                logger.info("policy_retrieval_hit_without_passage", chunk_ref=ref)
                continue
            hits.append(
                RetrievedChunk(
                    chunk=row,
                    score=score,
                    facet=facet,
                    semantic_score=semantic_by_ref.get(ref),
                    keyword_score=keyword_by_ref.get(ref),
                )
            )
        return hits


# --- fusion -------------------------------------------------------------------


def fuse_ranks(
    semantic: Sequence[tuple[str, float]],
    keyword: Sequence[tuple[str, float]],
    *,
    semantic_weight: float,
    keyword_weight: float,
    floor: float,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over two rankings, normalised and floored.

    Pure, and separated from the service for that reason: this is the arithmetic a
    retrieval order depends on, and it is testable with two lists.

    Normalised against the best result, so the configured floor means the same thing
    whether one backend answered or both did. An RRF score is otherwise unitless and
    its range depends on how many backends contributed — which would make the floor a
    different threshold on a deployment with no vector store than on one with.
    """
    scores: dict[str, float] = {}
    for rank, (ref, _) in enumerate(semantic, start=1):
        scores[ref] = scores.get(ref, 0.0) + semantic_weight / (RRF_K + rank)
    for rank, (ref, _) in enumerate(keyword, start=1):
        scores[ref] = scores.get(ref, 0.0) + keyword_weight / (RRF_K + rank)
    if not scores:
        return []

    best = max(scores.values())
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [
        (ref, round(raw / best if best else 0.0, 6))
        for ref, raw in ordered
        if (raw / best if best else 0.0) >= floor
    ]


# --- query building -----------------------------------------------------------


def build_facet_queries(notice: NoticeQuery) -> list[tuple[MatchFacet, str]]:
    """One query per question the notice asks, skipping the ones it cannot ask.

    A facet with nothing behind it is **omitted rather than sent empty**, which is
    what keeps the "facets answered" signal honest: a notice that never described a
    cause of loss must not be credited with a policy that answered a peril query
    built from three stopwords.

    The strings themselves are written in the vocabulary a *policy* uses rather than
    the one a notice uses, because the corpus being searched is the policy. Asking a
    wording about "the insured named in the declarations" retrieves the declarations
    page; asking it about "who is claiming" retrieves nothing, because no policy says
    that.
    """
    queries: list[tuple[MatchFacet, str]] = []

    identity = _join(
        (
            notice.policy_number,
            notice.broker_reference,
            notice.insured_name,
            notice.insured_organisation,
            notice.broker_name,
            notice.contract_number,
            notice.project_name,
        )
    )
    if identity:
        queries.append(
            (
                MatchFacet.IDENTITY,
                f"policy number named insured declarations producer {identity}",
            )
        )

    location = _join((notice.risk_location, notice.loss_location, notice.loss_postcode))
    if location:
        queries.append(
            (
                MatchFacet.RISK_LOCATION,
                f"schedule of covered premises location address project site {location}",
            )
        )

    peril = _join((notice.cause_of_loss, notice.loss_description))
    if peril:
        queries.append(
            (
                MatchFacet.PERIL,
                f"covered causes of loss perils insured against exclusions {peril}",
            )
        )

    coverage = _join((notice.claim_type, notice.line_of_business, notice.cause_of_loss))
    if coverage:
        queries.append(
            (
                MatchFacet.COVERAGE,
                f"coverage part limits of insurance deductible coverage form {coverage}",
            )
        )

    return queries


def _join(values: Sequence[str | None]) -> str:
    """The non-empty values, deduplicated, in order.

    Deduplicated because a notice frequently repeats itself across columns — the
    cause of loss and the loss description often say the same thing — and a term
    repeated three times in a `tsquery` skews `ts_rank` toward whichever passage
    happens to use that one word most.
    """
    seen: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if cleaned and cleaned.lower() not in {item.lower() for item in seen}:
            seen.append(cleaned)
    return " ".join(seen)[:1_200]


def _dedupe(hits: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """One entry per passage, keeping the highest-scoring facet that found it.

    Two facets retrieving the same clause is common and is evidence *for* the policy —
    counted in `facets_by_document` — but showing the officer the same paragraph twice
    is not. The facet breadth is recorded separately, so nothing is lost by collapsing
    the excerpt.
    """
    best: dict[str, RetrievedChunk] = {}
    for hit in hits:
        current = best.get(hit.chunk_ref)
        if current is None or hit.score > current.score:
            best[hit.chunk_ref] = hit
    return sorted(best.values(), key=lambda hit: hit.score, reverse=True)


__all__ = [
    "RRF_K",
    "FacetedRetrieval",
    "PolicyRetrievalService",
    "RetrievalResult",
    "RetrievedChunk",
    "build_facet_queries",
    "fuse_ranks",
]
