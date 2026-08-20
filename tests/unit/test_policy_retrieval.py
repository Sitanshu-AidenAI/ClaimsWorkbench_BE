"""Policy retrieval: facet queries, fusion, and degradation.

Three things are worth pinning here, and only the third is about scores.

**The facet split.** One concatenated query embeds to the average of four questions,
and the nearest passage to an average is frequently the nearest passage to none of
them. So the queries are built per facet, in the vocabulary a *policy* uses rather than
the one a notice uses, and a facet with nothing behind it is omitted rather than sent
empty — which is what keeps "the wording answered your cause of loss" an honest
sentence rather than a query built from three stopwords.

**Serial over the database.** An `AsyncSession` is not safe for concurrent use. Two
coroutines awaiting queries on one session raise `InvalidRequestError` and leave the
session in a state its own `close()` refuses — which is how the first version of this
module failed, on the fourth policy of a twelve-policy library. The concurrency is over
the network only, and the fake session here counts overlapping calls so a regression
fails loudly rather than intermittently.

**Fusion is rank-based.** A cosine similarity of 0.82 and a `ts_rank` of 0.19 cannot be
averaged; their ranks can be combined. Normalising against the best result is what
makes the configured floor mean the same thing whether one backend answered or both.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config import PolicyLibrarySettings
from app.domain.policy_matching import MatchFacet, NoticeQuery
from app.services.policies.retrieval import (
    PolicyRetrievalService,
    build_facet_queries,
    fuse_ranks,
)
from tests.fakes import FakeEmbeddingProvider, FakePolicyVectorStore


@dataclass
class FakeChunk:
    """Stands in for `PolicyDocumentChunk`."""

    chunk_ref: str
    policy_document_id: uuid.UUID
    content: str
    page_number: int | None = 1
    section_label: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeDocumentRepository:
    """Passages in a list, and a counter that catches concurrent session use.

    `overlaps` is the assertion that matters: a repository call that begins while
    another is still in flight is the bug this fake exists to catch, because the real
    failure is an intermittent `InvalidRequestError` under load.
    """

    def __init__(self, chunks: list[FakeChunk] | None = None) -> None:
        self.chunks = chunks or []
        self.keyword_calls: list[str] = []
        self.hydrate_calls = 0
        self.in_flight = 0
        self.overlaps = 0
        self.keyword_raises = False

    async def _enter(self) -> None:
        self.in_flight += 1
        if self.in_flight > 1:
            self.overlaps += 1
        # A real await, so two coroutines genuinely interleave here if the caller
        # gathers them rather than awaiting one at a time.
        await asyncio.sleep(0)

    def _exit(self) -> None:
        self.in_flight -= 1

    async def count_chunks(self) -> int:
        await self._enter()
        try:
            return len(self.chunks)
        finally:
            self._exit()

    async def keyword_search(
        self, query: str, *, limit: int, document_ids: Any = None
    ) -> list[tuple[FakeChunk, float]]:
        del limit, document_ids
        await self._enter()
        try:
            if self.keyword_raises:
                raise RuntimeError("the full-text index is unavailable")
            self.keyword_calls.append(query)
            terms = {word for word in query.lower().split() if len(word) > 2}
            scored: list[tuple[FakeChunk, float]] = []
            for chunk in self.chunks:
                body = set(chunk.content.lower().split())
                shared = len(terms & body)
                if shared:
                    scored.append((chunk, shared / max(1, len(terms))))
            scored.sort(key=lambda row: row[1], reverse=True)
            return scored
        finally:
            self._exit()

    async def list_chunks_by_refs(self, refs: Any) -> list[FakeChunk]:
        await self._enter()
        try:
            self.hydrate_calls += 1
            wanted = set(refs)
            return [chunk for chunk in self.chunks if chunk.chunk_ref in wanted]
        finally:
            self._exit()


def library(count: int = 3) -> tuple[FakeDocumentRepository, list[uuid.UUID]]:
    """A tiny library: one document per policy, three passages each."""
    documents = [uuid.uuid4() for _ in range(count)]
    bodies = [
        "policy number CP-4471-88210 named insured Harborline Cold Storage declarations",
        "schedule of covered premises 2870 Patapsco Industrial Parkway Baltimore Maryland",
        "covered causes of loss ammonia release refrigeration breakdown exclusions apply",
    ]
    chunks = [
        FakeChunk(
            chunk_ref=f"{document}:{index:05d}",
            policy_document_id=document,
            content=f"{body} for document {position}",
            page_number=index + 1,
        )
        for position, document in enumerate(documents)
        for index, body in enumerate(bodies)
    ]
    return FakeDocumentRepository(chunks), documents


def build(
    repository: FakeDocumentRepository,
    *,
    embeddings: Any = None,
    vectors: Any = None,
    config: PolicyLibrarySettings | None = None,
) -> PolicyRetrievalService:
    return PolicyRetrievalService(
        repository,  # type: ignore[arg-type]
        embeddings=embeddings,
        vectors=vectors,
        config=config or PolicyLibrarySettings(),
    )


NOTICE = NoticeQuery(
    policy_number="CP-4471-88210",
    insured_name="Harborline Cold Storage & Logistics, LLC",
    loss_location="2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
    loss_postcode="21226",
    cause_of_loss="Ammonia release from the refrigeration plant",
    claim_type="property_damage",
    line_of_business="property",
)


class TestFacetQueries:
    def test_a_full_notice_asks_every_facet(self) -> None:
        facets = [facet for facet, _ in build_facet_queries(NOTICE)]

        assert facets == [
            MatchFacet.IDENTITY,
            MatchFacet.RISK_LOCATION,
            MatchFacet.PERIL,
            MatchFacet.COVERAGE,
        ]

    def test_a_facet_with_nothing_behind_it_is_omitted(self) -> None:
        """Omitted rather than sent empty.

        A notice that never described a cause of loss must not be credited with a
        policy that answered a peril query built from three stopwords.
        """
        bare = NoticeQuery(policy_number="CP-4471-88210")
        facets = [facet for facet, _ in build_facet_queries(bare)]

        assert facets == [MatchFacet.IDENTITY]

    def test_an_empty_notice_asks_nothing(self) -> None:
        assert build_facet_queries(NoticeQuery()) == []

    def test_queries_are_written_in_the_policy_s_vocabulary(self) -> None:
        """The corpus being searched is the policy, so the query has to sound like one.

        Asking a wording about "the insured named in the declarations" retrieves the
        declarations page; asking it about "who is claiming" retrieves nothing, because
        no policy says that.
        """
        queries = dict(build_facet_queries(NOTICE))

        assert "declarations" in queries[MatchFacet.IDENTITY]
        assert "named insured" in queries[MatchFacet.IDENTITY]
        assert "schedule of covered premises" in queries[MatchFacet.RISK_LOCATION]
        assert "covered causes of loss" in queries[MatchFacet.PERIL]
        assert "limits of insurance" in queries[MatchFacet.COVERAGE]

    def test_the_notice_s_own_values_are_carried_into_the_query(self) -> None:
        queries = dict(build_facet_queries(NOTICE))

        assert "CP-4471-88210" in queries[MatchFacet.IDENTITY]
        assert "Patapsco" in queries[MatchFacet.RISK_LOCATION]
        assert "Ammonia" in queries[MatchFacet.PERIL]

    def test_repeated_values_across_columns_are_deduplicated(self) -> None:
        """A term repeated three times skews `ts_rank` toward whichever passage uses it most."""
        repetitive = NoticeQuery(
            cause_of_loss="Escape of water", loss_description="Escape of water"
        )
        peril = dict(build_facet_queries(repetitive))[MatchFacet.PERIL]

        assert peril.lower().count("escape of water") == 1

    def test_a_pathological_notice_cannot_build_an_unbounded_query(self) -> None:
        huge = NoticeQuery(loss_description="water " * 5_000)
        queries = build_facet_queries(huge)

        assert all(len(query) <= 1_300 for _, query in queries)


class TestFusion:
    def test_ranks_are_combined_rather_than_scores(self) -> None:
        ranked = fuse_ranks(
            [("a", 0.82), ("b", 0.81)],
            [("b", 0.19), ("c", 0.02)],
            semantic_weight=0.7,
            keyword_weight=0.3,
            floor=0.0,
        )
        order = [ref for ref, _ in ranked]

        # `b` is in both rankings, so it beats `a` which is only in one — even though
        # `a`'s raw cosine is the highest number in the input.
        assert order[0] == "b"
        assert set(order) == {"a", "b", "c"}

    def test_the_best_result_is_normalised_to_one(self) -> None:
        """So the configured floor means the same thing however many backends answered."""
        ranked = fuse_ranks([("a", 0.9)], [], semantic_weight=0.7, keyword_weight=0.3, floor=0.0)

        assert ranked[0][1] == pytest.approx(1.0)

    def test_the_floor_discards_the_tail(self) -> None:
        """Top-k with no threshold always returns k results, about nothing."""
        semantic = [(f"chunk-{index}", 1.0) for index in range(40)]
        ranked = fuse_ranks(semantic, [], semantic_weight=0.7, keyword_weight=0.3, floor=0.9)

        assert 0 < len(ranked) < 40

    def test_two_empty_rankings_fuse_to_nothing(self) -> None:
        assert fuse_ranks([], [], semantic_weight=0.7, keyword_weight=0.3, floor=0.0) == []


class TestSearchOneQuery:
    async def test_keyword_only_is_a_real_answer(self) -> None:
        """On this corpus it is not a consolation prize.

        A policy number and an insured name are rare tokens, and `ts_rank` over a GIN
        index finds them precisely where a dense vector finds them approximately.
        """
        repository, _ = library()
        service = build(repository)

        result = await service.search("policy number CP-4471-88210 Harborline")

        assert result.strategy == "keyword"
        assert result.degraded is False
        assert result.hits

    async def test_both_backends_fuse(self) -> None:
        repository, _ = library()
        vectors = FakePolicyVectorStore()
        embeddings = FakeEmbeddingProvider(dimension=32)
        for chunk in repository.chunks:
            from app.services.policies.vectors import PolicyVectorRecord

            await vectors.upsert(
                [
                    PolicyVectorRecord(
                        chunk_ref=chunk.chunk_ref,
                        document_id=chunk.policy_document_id,
                        vector=(await embeddings.embed_documents([chunk.content]))[0],
                        content_hash="x",
                    )
                ]
            )
        service = build(repository, embeddings=embeddings, vectors=vectors)

        result = await service.search("ammonia release refrigeration")

        assert result.strategy == "hybrid-rrf"
        assert result.hits

    async def test_an_unreachable_vector_store_degrades_to_keyword(self) -> None:
        """Reported as degraded, not swallowed. An unreachable index must not fail the match."""
        repository, _ = library()
        service = build(
            repository,
            embeddings=FakeEmbeddingProvider(dimension=32),
            vectors=FakePolicyVectorStore(fail_on_search=True),
        )

        result = await service.search("policy number CP-4471-88210")

        assert result.strategy == "keyword"
        assert result.degraded is True
        assert result.hits

    async def test_an_unavailable_keyword_index_does_not_raise(self) -> None:
        repository, _ = library()
        repository.keyword_raises = True
        service = build(repository)

        result = await service.search("anything")

        assert result.hits == []
        assert result.strategy == "none"

    async def test_an_empty_library_returns_nothing_and_says_so(self) -> None:
        service = build(FakeDocumentRepository([]))

        result = await service.search("policy number CP-4471-88210")

        assert result.hits == []
        assert result.chunks_available == 0

    async def test_a_blank_query_is_not_sent_anywhere(self) -> None:
        repository, _ = library()
        service = build(repository)

        result = await service.search("   ")

        assert result.hits == []
        assert repository.keyword_calls == []

    async def test_retrieval_can_be_switched_off(self) -> None:
        repository, _ = library()
        service = build(repository, config=PolicyLibrarySettings(retrieval_enabled=False))

        assert (await service.search("CP-4471-88210")).hits == []
        assert repository.keyword_calls == []


class TestSearchForNotice:
    async def test_hits_are_grouped_by_document_with_their_facets(self) -> None:
        repository, documents = library()
        service = build(repository)

        found = await service.search_for_notice(NOTICE)

        assert found.by_document
        assert set(found.by_document) <= set(documents)
        for document_id, hits in found.by_document.items():
            assert hits
            assert found.facets_by_document[document_id]

    async def test_the_database_is_never_touched_concurrently(self) -> None:
        """The correctness requirement, not an optimisation.

        Two coroutines awaiting queries on one `AsyncSession` raise
        `InvalidRequestError` and leave the session in a state its own `close()`
        refuses. This is the regression guard: the concurrency is over the network only.
        """
        repository, _ = library()
        service = build(
            repository,
            embeddings=FakeEmbeddingProvider(dimension=32),
            vectors=FakePolicyVectorStore(),
        )

        await service.search_for_notice(NOTICE)

        assert repository.overlaps == 0

    async def test_hydration_is_one_query_for_every_facet_together(self) -> None:
        """Four queries here would be three round trips bought for nothing."""
        repository, _ = library()
        service = build(repository)

        await service.search_for_notice(NOTICE)

        assert repository.hydrate_calls == 1

    async def test_one_query_per_facet_is_issued(self) -> None:
        repository, _ = library()
        service = build(repository)

        found = await service.search_for_notice(NOTICE)

        assert len(repository.keyword_calls) == len(found.facets_queried) == 4

    async def test_excerpts_per_document_are_capped(self) -> None:
        """One long wording must not contribute forty excerpts to the card it explains."""
        repository, _ = library()
        service = build(repository, config=PolicyLibrarySettings(excerpts_per_policy=1))

        found = await service.search_for_notice(NOTICE)

        assert all(len(hits) == 1 for hits in found.by_document.values())

    async def test_the_same_passage_found_twice_is_one_excerpt(self) -> None:
        """Two facets retrieving one clause is evidence *for* the policy, counted in the
        facet set — but showing the officer the same paragraph twice is not."""
        repository, _ = library()
        service = build(repository)

        found = await service.search_for_notice(NOTICE)

        for hits in found.by_document.values():
            refs = [hit.chunk_ref for hit in hits]
            assert len(refs) == len(set(refs))

    async def test_an_empty_notice_queries_nothing(self) -> None:
        repository, _ = library()
        service = build(repository)

        found = await service.search_for_notice(NoticeQuery())

        assert found.by_document == {}
        assert found.facets_queried == ()
        assert repository.keyword_calls == []

    async def test_a_degraded_facet_marks_the_whole_run_degraded(self) -> None:
        repository, _ = library()
        service = build(
            repository,
            embeddings=FakeEmbeddingProvider(dimension=32),
            vectors=FakePolicyVectorStore(fail_on_search=True),
        )

        found = await service.search_for_notice(NOTICE)

        assert found.degraded is True
        assert found.strategy == "keyword"
