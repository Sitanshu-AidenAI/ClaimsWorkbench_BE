"""Retrieval, evidence assembly and per-field citations.

The chain this exercises is the one the user actually sees: a query finds passages, the
passages go into the prompt with labels, the model echoes a label back, and the label
becomes a row that the review screen turns into "here is the page this came from".

Every link has a failure mode worth a test, and two of them are the kind that fail
*silently*: a hallucinated label stored as if it were real, and retrieval quietly
returning nothing so the model reads a truncated corpus without anyone noticing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config import AISettings, DocumentIntelligenceSettings
from app.domain.enums import FieldSource
from app.domain.extraction import (
    AdditionalExtraction,
    ExtractedField,
    FNOLExtraction,
    LossExtraction,
    NotificationExtraction,
    PolicyExtraction,
)
from app.services.fnol.evidence import SECTION_QUERIES, FNOLEvidenceService
from app.services.fnol.extraction import FNOLExtractionService
from app.services.intelligence.retrieval import RetrievalService
from tests.fakes import FakeEmbeddingProvider, FakeVectorStore, StubProvider


@dataclass
class Row:
    """Stands in for `FNOLDocumentChunk`."""

    content: str
    chunk_ref: str
    fnol_document_id: uuid.UUID
    fnol_case_id: uuid.UUID
    chunk_index: int = 0
    page_number: int | None = 1
    section_label: str | None = None
    char_start: int = 0
    char_end: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class Repo:
    """Passages in a list, with a keyword search that behaves like a ranked search."""

    def __init__(self, rows: list[Row], *, keyword_raises: bool = False) -> None:
        self.rows = rows
        self._keyword_raises = keyword_raises

    async def count_for_case(self, case_id: uuid.UUID) -> int:
        return sum(1 for row in self.rows if row.fnol_case_id == case_id)

    async def list_by_refs(self, refs: Any) -> list[Row]:
        wanted = set(refs)
        return [row for row in self.rows if row.chunk_ref in wanted]

    async def keyword_search(
        self, case_id: uuid.UUID, query: str, *, limit: int, document_id: Any = None
    ) -> list[tuple[Row, float]]:
        if self._keyword_raises:
            raise RuntimeError("full text is unavailable")

        terms = {word for word in query.lower().split() if len(word) > 2}
        scored: list[tuple[Row, float]] = []
        for row in self.rows:
            if row.fnol_case_id != case_id:
                continue
            if document_id is not None and row.fnol_document_id != document_id:
                continue
            body = row.content.lower()
            overlap = sum(1 for term in terms if term in body)
            if overlap:
                scored.append((row, overlap / max(1, len(terms))))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]


CASE_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()

PASSAGES = [
    "Policy number: CP-2026-4471 issued to Harborview Logistics Ltd",
    "Date of loss: 08 March 2026. The incident occurred overnight.",
    "Cause of loss: escape of water from a failed riser joint on the third floor",
    "Estimated loss: GBP 128,000 with a repair estimate of GBP 96,400",
    "The site address is Unit 7 Harborview Estate, Kingston upon Hull",
    "Injuries: none reported. Fatalities: none.",
    "Police reference: WY-2026-88123 attended by the local fire brigade",
    "Attached: schedule of values, repair quotation, damage photographs",
    "The insured operates a chilled distribution warehouse on the site",
    "Business interruption is expected while the riser is replaced",
    "Loss adjuster appointed: M. Odele of Odele Loss Adjusting",
    "Broker contact: Jane Brooks, jane.brooks@brokerage.example, 01482 555 0134",
    "Reinstatement is expected to take eleven weeks from the date of instruction",
    "The policy period ran 01 January 2026 to 31 December 2026",
]


def make_rows(count: int | None = None) -> list[Row]:
    bodies = PASSAGES[: count if count is not None else len(PASSAGES)]
    return [
        Row(
            content=body,
            chunk_ref=f"{DOC_ID}:{index:05d}",
            fnol_document_id=DOC_ID,
            fnol_case_id=CASE_ID,
            chunk_index=index,
            char_start=index * 100,
            char_end=index * 100 + len(body),
        )
        for index, body in enumerate(bodies)
    ]


def make_retrieval(
    rows: list[Row],
    *,
    with_vectors: bool = True,
    keyword_raises: bool = False,
    config: DocumentIntelligenceSettings | None = None,
) -> tuple[RetrievalService, FakeVectorStore | None, FakeEmbeddingProvider | None]:
    repo = Repo(rows, keyword_raises=keyword_raises)
    settings_used = config or DocumentIntelligenceSettings(retrieval_min_score=0.0)

    if not with_vectors:
        return RetrievalService(repo, config=settings_used), None, None  # type: ignore[arg-type]

    embeddings = FakeEmbeddingProvider()
    store = FakeVectorStore()
    return (
        RetrievalService(repo, embeddings=embeddings, vectors=store, config=settings_used),  # type: ignore[arg-type]
        store,
        embeddings,
    )


async def seed_vectors(
    store: FakeVectorStore, embeddings: FakeEmbeddingProvider, rows: list[Row]
) -> None:
    from app.services.intelligence.vectors import VectorRecord

    vectors = await embeddings.embed_documents([row.content for row in rows])
    await store.upsert(
        [
            VectorRecord(
                chunk_ref=row.chunk_ref,
                case_id=row.fnol_case_id,
                document_id=row.fnol_document_id,
                vector=vector,
                content_hash="h",
                page_number=row.page_number,
                section_label=row.section_label,
            )
            for row, vector in zip(rows, vectors, strict=True)
        ]
    )


class TestRetrieval:
    async def test_hybrid_finds_the_relevant_passage(self) -> None:
        rows = make_rows()
        service, store, embeddings = make_retrieval(rows)
        assert store is not None and embeddings is not None
        await seed_vectors(store, embeddings, rows)

        result = await service.search(CASE_ID, "policy number", limit=3)

        assert result.strategy == "hybrid-rrf"
        assert result.degraded is False
        assert "CP-2026-4471" in result.hits[0].chunk.content
        # Both backends contributed to the top hit.
        assert result.hits[0].semantic_score is not None
        assert result.hits[0].keyword_score is not None

    async def test_keyword_only_when_no_vector_store_is_configured(self) -> None:
        rows = make_rows()
        service, _, _ = make_retrieval(rows, with_vectors=False)

        result = await service.search(CASE_ID, "police reference", limit=3)

        assert result.strategy == "keyword"
        # Not degraded: nothing failed, a backend simply is not configured.
        assert result.degraded is False
        assert "WY-2026-88123" in result.hits[0].chunk.content

    async def test_semantic_only_when_the_keyword_side_raises(self) -> None:
        rows = make_rows()
        service, store, embeddings = make_retrieval(rows, keyword_raises=True)
        assert store is not None and embeddings is not None
        await seed_vectors(store, embeddings, rows)

        result = await service.search(CASE_ID, "policy number", limit=3)
        assert result.strategy == "semantic"
        assert result.hits

    async def test_an_unreachable_vector_store_degrades_rather_than_fails(self) -> None:
        rows = make_rows()
        repo = Repo(rows)
        service = RetrievalService(
            repo,  # type: ignore[arg-type]
            embeddings=FakeEmbeddingProvider(),
            vectors=FakeVectorStore(fail_on_upsert=False),
            config=DocumentIntelligenceSettings(retrieval_min_score=0.0),
        )

        class Broken(FakeVectorStore):
            async def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
                raise ConnectionError("qdrant is down")

        service._vectors = Broken()
        result = await service.search(CASE_ID, "policy number", limit=3)

        # Keyword results still come back, and the degradation is *reported* rather
        # than swallowed — that is what makes it visible in the analysis record.
        assert result.strategy == "keyword"
        assert result.degraded is True
        assert result.hits

    async def test_the_score_floor_rejects_weak_matches(self) -> None:
        rows = make_rows()
        service, _, _ = make_retrieval(
            rows,
            with_vectors=False,
            config=DocumentIntelligenceSettings(retrieval_min_score=0.99),
        )
        result = await service.search(CASE_ID, "policy number", limit=5)
        # Only the very best survives a floor that high — the point being that a floor
        # exists at all, so a case mentioning nothing like the query returns nothing
        # rather than five confident-looking passages about nothing.
        assert len(result.hits) <= 1

    async def test_an_empty_case_returns_nothing(self) -> None:
        service, _, _ = make_retrieval([], with_vectors=False)
        result = await service.search(CASE_ID, "policy number")
        assert result.hits == []
        assert result.chunks_available == 0

    async def test_a_hit_with_no_surviving_passage_is_skipped(self) -> None:
        # A point left behind by a deleted document. Skipped rather than surfaced.
        rows = make_rows()
        service, store, embeddings = make_retrieval(rows)
        assert store is not None and embeddings is not None
        await seed_vectors(store, embeddings, rows)

        orphan = Row(
            content="a passage nobody has any more",
            chunk_ref=f"{DOC_ID}:99999",
            fnol_document_id=DOC_ID,
            fnol_case_id=CASE_ID,
        )
        await seed_vectors(store, embeddings, [orphan])

        result = await service.search(CASE_ID, "passage nobody", limit=5)
        assert all(hit.chunk_ref != orphan.chunk_ref for hit in result.hits)


class TestEvidenceBundle:
    async def test_a_small_case_sends_the_whole_corpus(self) -> None:
        # Selecting six passages out of eight is overhead with a downside and no upside.
        rows = make_rows(4)
        service, _, _ = make_retrieval(rows, with_vectors=False)
        evidence = FNOLEvidenceService(
            service, config=DocumentIntelligenceSettings(retrieval_min_chunks=12)
        )

        bundle = await evidence.gather(CASE_ID, [])
        assert bundle.used is False
        assert bundle.strategy == "whole-corpus"

    async def test_a_large_enough_case_gets_labelled_passages(self) -> None:
        rows = make_rows()
        service, _, _ = make_retrieval(
            rows, with_vectors=False, config=DocumentIntelligenceSettings(retrieval_min_score=0.0)
        )
        evidence = FNOLEvidenceService(
            service,
            config=DocumentIntelligenceSettings(retrieval_min_chunks=5, max_evidence_chunks=8),
        )

        bundle = await evidence.gather(CASE_ID, [])

        assert bundle.used is True
        assert len(bundle.by_label) <= 8
        assert "PASSAGE [C1]" in bundle.prompt
        # Every label in the prompt resolves, which is the property that makes a
        # citation work at all.
        for label in bundle.by_label:
            assert f"[{label}]" in bundle.prompt
            assert bundle.resolve(label) is not None

    async def test_a_passage_wanted_by_several_sections_appears_once(self) -> None:
        rows = make_rows()
        service, _, _ = make_retrieval(rows, with_vectors=False)
        evidence = FNOLEvidenceService(
            service,
            config=DocumentIntelligenceSettings(retrieval_min_chunks=5, max_evidence_chunks=20),
        )
        bundle = await evidence.gather(CASE_ID, [])

        refs = [citation.chunk_ref for citation in bundle.by_label.values()]
        assert len(refs) == len(set(refs))

    async def test_the_trace_records_what_was_searched(self) -> None:
        rows = make_rows()
        service, _, _ = make_retrieval(rows, with_vectors=False)
        evidence = FNOLEvidenceService(
            service, config=DocumentIntelligenceSettings(retrieval_min_chunks=5)
        )
        bundle = await evidence.gather(CASE_ID, [])

        # The record that answers "why did the model read that" months later.
        assert bundle.trace["strategy"] == bundle.strategy
        assert bundle.trace["passages_shown"] == len(bundle.by_label)
        assert set(bundle.trace["sections"]).issubset(set(SECTION_QUERIES))

    def test_an_invented_label_resolves_to_nothing(self) -> None:
        from app.services.fnol.evidence import EvidenceBundle

        bundle = EvidenceBundle()
        assert bundle.resolve("C9") is None
        assert bundle.resolve(None) is None


# --- citations ----------------------------------------------------------------


class FieldRow:
    """Stands in for `FNOLExtractedField`."""

    def __init__(self, path: str) -> None:
        self.field_path = path
        self.confidence: float | None = None
        self.human_modified = False
        self.source_document_id: uuid.UUID | None = None
        self.source_chunk_id: uuid.UUID | None = None
        self.evidence_snippet: str | None = None


class FieldRepo:
    """Records what `upsert_field` was asked to write."""

    def __init__(self) -> None:
        self.written: dict[str, dict[str, Any]] = {}

    async def upsert_field(self, case_id: uuid.UUID, **kwargs: Any) -> FieldRow:
        del case_id
        path = kwargs["field_path"]
        self.written[path] = kwargs
        row = FieldRow(path)
        row.confidence = kwargs.get("confidence")
        row.source_document_id = kwargs.get("source_document_id")
        row.source_chunk_id = kwargs.get("source_chunk_id")
        return row

    async def get_field(self, case_id: uuid.UUID, path: str) -> None:
        del case_id, path
        return None

    async def upsert_party(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class Case:
    """The attributes `apply` writes onto."""

    def __init__(self) -> None:
        self.id = CASE_ID
        self.reference = "FNOL-2026-000001"
        self.currency = "GBP"
        self.extraction_confidence: float | None = None


def field(
    value: str | None, *, ref: str | None = None, evidence: str | None = None
) -> ExtractedField:
    return ExtractedField(
        value=value,
        confidence=0.9 if value else 0.0,
        evidence=evidence,
        source_chunk_ref=ref,
    )


def extraction(policy_ref: str | None) -> FNOLExtraction:
    empty = field(None)
    return FNOLExtraction(
        notification=NotificationExtraction(
            reporter_name=empty,
            reporter_organisation=empty,
            reporter_role=empty,
            reporter_email=empty,
            reporter_phone=empty,
        ),
        policy=PolicyExtraction(
            policy_number=field(
                "CP-2026-4471", ref=policy_ref, evidence="Policy number: CP-2026-4471"
            ),
            insured_name=empty,
            insured_organisation=empty,
            policy_type=empty,
            effective_date=empty,
            expiry_date=empty,
        ),
        loss=LossExtraction(
            date_of_loss=empty,
            time_of_loss=empty,
            loss_location=empty,
            loss_country=empty,
            loss_description=empty,
            cause_of_loss=empty,
            affected_assets=empty,
            injuries=empty,
            fatalities=empty,
            estimated_loss_amount=empty,
            currency=empty,
            business_interruption=empty,
            structural_damage=empty,
            environmental_exposure=empty,
        ),
        additional=AdditionalExtraction(
            police_reference=empty,
            incident_reference=empty,
            authorities_involved=empty,
            repair_estimate_amount=empty,
            potential_litigation=empty,
            supporting_documents=empty,
        ),
        parties=[],
        overall_confidence=0.8,
    )


async def bundle_of(rows: list[Row]) -> Any:
    service, _, _ = make_retrieval(rows, with_vectors=False)
    evidence = FNOLEvidenceService(
        service, config=DocumentIntelligenceSettings(retrieval_min_chunks=5, max_evidence_chunks=20)
    )
    return await evidence.gather(CASE_ID, [])


class TestCitations:
    async def test_a_resolved_label_becomes_a_document_and_passage_id(self) -> None:
        bundle = await bundle_of(make_rows())
        label = next(iter(bundle.by_label))
        citation = bundle.by_label[label]

        repo = FieldRepo()
        service = FNOLExtractionService(repo, provider=None)  # type: ignore[arg-type]
        await service.apply(Case(), extraction(label), evidence=bundle)

        written = repo.written["policy.policy_number"]
        assert written["source_chunk_id"] == citation.chunk_id
        assert written["source_document_id"] == citation.document_id
        assert written["value_text"] == "CP-2026-4471"

    async def test_an_invented_label_is_dropped_but_the_value_survives(self) -> None:
        # A model may read a value correctly and mislabel where it came from. Losing the
        # value would be worse than losing the citation; storing a citation that points
        # nowhere would be worse than both.
        bundle = await bundle_of(make_rows())

        repo = FieldRepo()
        service = FNOLExtractionService(repo, provider=None)  # type: ignore[arg-type]
        await service.apply(Case(), extraction("C99"), evidence=bundle)

        written = repo.written["policy.policy_number"]
        assert written["value_text"] == "CP-2026-4471"
        assert written["source_chunk_id"] is None
        assert written["source_document_id"] is None

    async def test_no_evidence_bundle_writes_no_citation(self) -> None:
        repo = FieldRepo()
        service = FNOLExtractionService(repo, provider=None)  # type: ignore[arg-type]
        await service.apply(Case(), extraction(None), evidence=None)

        written = repo.written["policy.policy_number"]
        assert written["value_text"] == "CP-2026-4471"
        assert written["source_chunk_id"] is None
        assert written["source"] == FieldSource.AI

    def test_a_label_shaped_like_prose_is_rejected_by_the_schema(self) -> None:
        # The model answering "the survey report" here has not cited a passage, and
        # storing it would put prose in a column resolved as an identifier.
        assert field("x", ref="the survey report").source_chunk_ref is None
        assert field("x", ref="page 4").source_chunk_ref is None
        assert field("x", ref="c3").source_chunk_ref == "C3"
        assert field("x", ref=" C12 ").source_chunk_ref == "C12"


class TestPromptComposition:
    async def test_retrieval_narrows_the_prompt_and_keeps_the_notice_whole(self) -> None:
        rows = make_rows()
        bundle = await bundle_of(rows)
        assert bundle.used

        service, _, _ = make_retrieval(rows, with_vectors=False)
        evidence = FNOLEvidenceService(
            service,
            config=DocumentIntelligenceSettings(retrieval_min_chunks=5, max_evidence_chunks=8),
        )
        provider = StubProvider(result=extraction(None))
        extractor = FNOLExtractionService(
            FieldRepo(),  # type: ignore[arg-type]
            provider=provider,
            evidence=evidence,
        )

        await extractor.extract(
            source_text="Broker email: please see the attached survey report.",
            document_texts=["a very long document " * 500],
            channel="broker_email",
            case_id=CASE_ID,
        )

        prompt = provider.prompt_seen
        # The notification body is always first and always whole.
        assert "Broker email: please see the attached survey report." in prompt
        assert "=== PASSAGE [C1]" in prompt
        # And the whole-corpus concatenation is *not* what was sent.
        assert "a very long document a very long document" not in prompt

    async def test_without_retrieval_the_prompt_is_the_whole_corpus(self) -> None:
        provider = StubProvider(result=extraction(None))
        extractor = FNOLExtractionService(
            FieldRepo(),  # type: ignore[arg-type]
            provider=provider,
            ai_config=AISettings(max_input_characters=60_000),
        )

        await extractor.extract(
            source_text="Broker email body.",
            document_texts=["Attachment text about the loss."],
            channel="broker_email",
        )

        prompt = provider.prompt_seen
        assert "Broker email body." in prompt
        assert "Attachment text about the loss." in prompt
        assert "PASSAGE [" not in prompt

    async def test_a_failing_evidence_service_does_not_fail_the_extraction(self) -> None:
        class Broken:
            async def gather(self, *_args: Any, **_kwargs: Any) -> Any:
                raise RuntimeError("retrieval exploded")

        provider = StubProvider(result=extraction(None))
        extractor = FNOLExtractionService(
            FieldRepo(),  # type: ignore[arg-type]
            provider=provider,
            evidence=Broken(),  # type: ignore[arg-type]
        )

        outcome = await extractor.extract(
            source_text="Broker email body.",
            document_texts=["Attachment text."],
            channel="broker_email",
            case_id=CASE_ID,
        )

        # The whole corpus is still there and is what the pipeline read before
        # retrieval existed.
        assert outcome.succeeded
        assert outcome.retrieval_used is False
        assert "Attachment text." in provider.prompt_seen

    def test_the_system_prompt_tells_the_model_never_to_invent_a_label(self) -> None:
        from app.services.fnol.extraction import SYSTEM_PROMPT

        assert "source_chunk_ref" in SYSTEM_PROMPT
        assert "Never invent a label" in SYSTEM_PROMPT


class TestFieldQueryCoverage:
    def test_every_field_section_has_queries(self) -> None:
        # Enforced at import too, but asserted here so the failure names the omission
        # rather than appearing as an import error in an unrelated test.
        from app.services.fnol.extraction import _FIELD_META

        sections = {section for _, section in _FIELD_META.values()}
        assert sections <= set(SECTION_QUERIES)

    @pytest.mark.parametrize("section", sorted(SECTION_QUERIES))
    def test_no_query_group_is_empty(self, section: str) -> None:
        assert SECTION_QUERIES[section]
        assert all(query.strip() for query in SECTION_QUERIES[section])
