"""Policy ingestion: chunking, vector insertion, idempotency, isolation.

Four properties, each of which is the answer to a question someone will ask in
production:

* *"Why did re-uploading cost money?"* — it must not. Running twice on an unchanged
  wording does no embedding call and no vector write.
* *"Why is nothing matching?"* — with no embedding provider the passages still exist
  and are still keyword-searchable. That is `chunked`, not `failed`.
* *"Why did one bad PDF lose the other thirty-nine?"* — it must not. Each document's
  outcome is its own, and a transient failure is re-raised *after* the row records it.
* *"Which policy is this the wording for?"* — the number read out of the PDF is looked
  up in the book, and a number that matches nothing leaves the document unlinked. It
  never creates a policy.

The repository is a small in-memory fake rather than a real session: this is testing
the service's decision-making, and a database would only make the same assertions
slower. The vector store double is a real implementation of the Protocol, including
overwrite-on-same-id — a mock that appended would make the re-ingest test pass while
the real store accumulated a second copy of every clause.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.core.config import DocumentIntelligenceSettings, PolicyLibrarySettings
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus
from app.repositories.policy_document import policy_chunk_ref
from app.services.policies.ingestion import (
    PolicyIngestionService,
    TransientIngestError,
)
from app.services.policies.vectors import point_id_for
from tests.fakes import FailingEmbeddingProvider, FakeEmbeddingProvider, FakePolicyVectorStore

WORDING = (
    "MERIDIAN ATLANTIC INSURANCE COMPANY\n"
    "COMMERCIAL PROPERTY COVERAGE PART - DECLARATIONS\n\n"
    "POLICY NUMBER: CP-4471-88210\n"
    "POLICY PERIOD: From 03/01/2025 to 03/01/2026\n"
    "NAMED INSURED:      Harborline Cold Storage & Logistics, LLC\n"
    "PRODUCER:           Talbot & Rennick Insurance Brokers, Inc.\n\n"
    + "SECTION I - COVERED CAUSES OF LOSS. "
    + ("This policy insures against direct physical loss. " * 40)
    + "\n\nSECTION II - EXCLUSIONS. "
    + ("We will not pay for loss caused by flood or earth movement. " * 40)
)


@dataclass
class FakeChunkRow:
    """Stands in for `PolicyDocumentChunk`, with the fields the service touches."""

    policy_document_id: uuid.UUID
    policy_id: uuid.UUID | None
    chunk_index: int
    chunk_ref: str
    content: str
    content_hash: str
    token_count: int
    char_start: int
    char_end: int
    page_number: int | None = None
    page_from: int | None = None
    page_to: int | None = None
    section_label: str | None = None
    vector_point_id: uuid.UUID | None = None
    embedding_model: str | None = None
    embedding_dimension: int | None = None
    embedded_at: Any = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass
class FakeDocument:
    """Stands in for `PolicyDocument`."""

    extracted_text: str = WORDING
    filename: str = "POL-CP-4471-88210_Harborline.pdf"
    checksum_sha256: str = "deadbeef"
    text_extractor: str = "pdf_text_layer"
    text_extractor_version: str = "1"
    extraction_status: str = DocumentExtractionStatus.EXTRACTED
    extraction_error: str | None = None
    page_offsets: list[list[int]] | None = None
    policy_id: uuid.UUID | None = None
    policy_number: str | None = None
    insured_name: str | None = None
    insurer_name: str | None = None
    broker_name: str | None = None
    policy_type: str | None = None
    line_of_business: str | None = None
    effective_date: Any = None
    expiry_date: Any = None
    extracted_metadata: dict[str, Any] = field(default_factory=dict)
    ingest_status: str = PolicyIngestStatus.PENDING
    ingest_fingerprint: str | None = None
    ingest_error: str | None = None
    ingest_attempts: int = 0
    ingest_started_at: Any = None
    ingested_at: Any = None
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class FakeDocumentRepository:
    """In-memory passages, keyed by document."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, list[FakeChunkRow]] = {}
        self.replace_calls = 0
        self.linked: list[tuple[uuid.UUID, uuid.UUID | None]] = []

    async def flush(self) -> None:
        return None

    async def replace_chunks_for_document(self, document: Any, chunks: Any) -> list[FakeChunkRow]:
        self.replace_calls += 1
        rows = [
            FakeChunkRow(
                policy_document_id=document.id,
                policy_id=document.policy_id,
                chunk_index=chunk.index,
                chunk_ref=policy_chunk_ref(document.id, chunk.index),
                content=chunk.content,
                content_hash=chunk.content_hash,
                token_count=chunk.token_estimate,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                page_number=chunk.page_number,
                page_from=chunk.page_from,
                page_to=chunk.page_to,
                section_label=chunk.section_label,
            )
            for chunk in chunks
        ]
        self.rows[document.id] = rows
        return rows

    async def list_chunks_for_document(self, document_id: uuid.UUID) -> list[FakeChunkRow]:
        return self.rows.get(document_id, [])

    async def mark_embedded(
        self, rows: Any, *, point_ids: dict[str, uuid.UUID], model: str, dimension: int
    ) -> int:
        count = 0
        for row in rows:
            point = point_ids.get(row.chunk_ref)
            if point is None:
                continue
            row.vector_point_id = point
            row.embedding_model = model
            row.embedding_dimension = dimension
            count += 1
        return count

    async def clear_vectors_for_document(self, document_id: uuid.UUID) -> None:
        for row in self.rows.get(document_id, []):
            row.vector_point_id = None
            row.embedding_model = None

    async def link_policy(self, document: Any, policy_id: uuid.UUID | None) -> None:
        document.policy_id = policy_id
        self.linked.append((document.id, policy_id))
        for row in self.rows.get(document.id, []):
            row.policy_id = policy_id


class FakePolicyBook:
    """Stands in for `PolicyRepository`. Only `get_by_number` is reached."""

    def __init__(self, numbers: dict[str, uuid.UUID] | None = None) -> None:
        self.numbers = numbers or {}
        self.lookups: list[str] = []

    async def get_by_number(self, policy_number: str) -> Any:
        from app.domain.matching import normalise_reference

        self.lookups.append(policy_number)
        key = normalise_reference(policy_number)
        found = self.numbers.get(key)
        return type("Row", (), {"id": found})() if found else None


def build(
    *,
    embeddings: Any = None,
    vectors: Any = None,
    policies: Any = None,
    config: PolicyLibrarySettings | None = None,
    embedding_config: DocumentIntelligenceSettings | None = None,
) -> tuple[PolicyIngestionService, FakeDocumentRepository]:
    repository = FakeDocumentRepository()
    service = PolicyIngestionService(
        repository,  # type: ignore[arg-type]
        policies=policies,
        embeddings=embeddings,
        vectors=vectors,
        config=config or PolicyLibrarySettings(),
        embedding_config=embedding_config or DocumentIntelligenceSettings(),
    )
    return service, repository


class TestChunking:
    async def test_a_wording_becomes_passages_with_exact_offsets(self) -> None:
        """The invariant every excerpt depends on.

        `chunk.content == text[char_start:char_end]`. If a passage were assembled
        rather than sliced, the offsets would point somewhere else and the officer
        would be shown the wrong clause.
        """
        service, repository = build()
        document = FakeDocument()

        outcome = await service.ingest(document)

        rows = repository.rows[document.id]
        assert outcome.chunks == len(rows) > 1
        for row in rows:
            assert row.content == WORDING[row.char_start : row.char_end]

    async def test_passages_are_cut_within_a_page(self) -> None:
        """A policy's page boundaries are meaningful: a schedule is a page.

        Honouring them makes an excerpt's page number exact *and* makes the passage a
        coherent unit rather than a window over two unrelated clauses.
        """
        service, repository = build()
        half = len(WORDING) // 2
        document = FakeDocument(page_offsets=[[0, half], [half, len(WORDING)]])

        await service.ingest(document)

        pages = {row.page_number for row in repository.rows[document.id]}
        assert pages <= {1, 2}
        assert None not in pages

    async def test_page_offsets_past_the_stored_text_are_clamped(self) -> None:
        """A reader whose text was truncated after the fact must not produce bad offsets."""
        service, repository = build()
        document = FakeDocument(page_offsets=[[0, 50], [50, len(WORDING) + 5_000]])

        await service.ingest(document)

        for row in repository.rows[document.id]:
            assert row.char_end <= len(WORDING)

    async def test_a_wording_with_no_text_fails_rather_than_skips(self) -> None:
        """Unlike a claim attachment, a wording with no text is a policy nobody can find.

        The claim pipeline treats an unreadable scan as `skipped` because the extraction
        can still read the rest of the corpus. Here there is no rest: an administrator
        has to see it in red.
        """
        service, _ = build()
        document = FakeDocument(
            extracted_text="",
            extraction_status=DocumentExtractionStatus.UNSUPPORTED,
            extraction_error="This PDF has no text layer.",
        )

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.FAILED
        assert outcome.error is not None
        assert "text layer" in outcome.error

    async def test_the_chunk_cap_truncates_rather_than_filling_the_table(self) -> None:
        service, repository = build(
            config=PolicyLibrarySettings(max_chunks_per_document=3, chunk_tokens=20)
        )
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.chunks == 3
        assert len(repository.rows[document.id]) == 3


class TestVectorInsertion:
    async def test_passages_are_embedded_and_written_to_the_policy_collection(self) -> None:
        embeddings = FakeEmbeddingProvider(dimension=32)
        vectors = FakePolicyVectorStore()
        service, repository = build(embeddings=embeddings, vectors=vectors)
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.EMBEDDED
        assert outcome.embedded == outcome.chunks
        assert len(vectors.points) == outcome.chunks
        assert vectors.dimension == 32
        for row in repository.rows[document.id]:
            assert row.vector_point_id == point_id_for(row.chunk_ref)
            assert row.embedding_model == "fake-embedding"

    async def test_the_payload_carries_filter_keys_and_no_passage_text(self) -> None:
        """Postgres owns the excerpt; the payload owns what a filtered search needs.

        A fourth copy of policy text in a second datastore with its own backup and
        access-control story is a cost that buys no read.
        """
        vectors = FakePolicyVectorStore()
        service, _ = build(embeddings=FakeEmbeddingProvider(dimension=16), vectors=vectors)
        document = FakeDocument()

        await service.ingest(document)

        payload = next(iter(vectors.points.values()))[1]
        assert payload["policy_number"] == "CP-4471-88210"
        assert payload["line_of_business"] == "property"
        assert payload["policy_document_id"] == str(document.id)
        assert not any("insures against" in str(value) for value in payload.values())

    async def test_re_ingesting_overwrites_points_rather_than_duplicating_them(self) -> None:
        """Deterministic point ids exist to produce exactly this.

        A store that appended would leave a second copy of every clause that nothing
        can ever name again — findable, quotable, and impossible to remove.
        """
        vectors = FakePolicyVectorStore()
        service, _ = build(embeddings=FakeEmbeddingProvider(dimension=16), vectors=vectors)
        document = FakeDocument()

        first = await service.ingest(document)
        second = await service.ingest(document, force=True)

        assert second.chunks == first.chunks
        assert len(vectors.points) == first.chunks

    async def test_stale_points_from_a_re_chunk_are_deleted_first(self) -> None:
        """A wording that now yields fewer passages must not leave orphans behind."""
        vectors = FakePolicyVectorStore()
        service, _ = build(embeddings=FakeEmbeddingProvider(dimension=16), vectors=vectors)
        document = FakeDocument()
        await service.ingest(document)
        many = len(vectors.points)

        coarse, _ = build(
            embeddings=FakeEmbeddingProvider(dimension=16),
            vectors=vectors,
            config=PolicyLibrarySettings(chunk_tokens=4_000),
        )
        document.ingest_fingerprint = None
        await coarse.ingest(document, force=True)

        assert len(vectors.points) < many

    async def test_no_embedding_provider_is_chunked_not_failed(self) -> None:
        """The passages exist and are keyword-searchable. Nothing is wrong here."""
        service, repository = build()
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.CHUNKED
        assert outcome.chunks > 0
        assert outcome.embedded == 0
        assert outcome.error is not None
        assert "keyword" in outcome.error
        assert all(row.vector_point_id is None for row in repository.rows[document.id])

    async def test_an_unreachable_vector_store_is_transient_and_recorded(self) -> None:
        vectors = FakePolicyVectorStore(fail_on_upsert=True)
        service, _ = build(embeddings=FakeEmbeddingProvider(dimension=16), vectors=vectors)
        document = FakeDocument()

        with pytest.raises(TransientIngestError):
            await service.ingest(document)

        # The row records the attempt, which is what makes the retry bounded — and the
        # fingerprint is *not* stamped, or the retry would skip itself as done.
        assert document.ingest_status == PolicyIngestStatus.FAILED
        assert document.ingest_attempts == 1
        assert document.ingest_fingerprint is None

    async def test_an_unreachable_embedding_provider_is_transient(self) -> None:
        service, _ = build(
            embeddings=FailingEmbeddingProvider(retryable=True, dimension=16),
            vectors=FakePolicyVectorStore(),
        )

        with pytest.raises(TransientIngestError):
            await service.ingest(FakeDocument())

    async def test_a_permanent_provider_failure_is_recorded_and_not_raised(self) -> None:
        """A permanent outcome is a row an administrator reads, not one a worker retries."""
        service, _ = build(
            embeddings=FailingEmbeddingProvider(retryable=False, dimension=16),
            vectors=FakePolicyVectorStore(),
        )
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.FAILED
        assert document.ingest_fingerprint is not None  # stamped: do not redo this


class TestIdempotency:
    async def test_a_second_run_on_an_unchanged_wording_does_no_work(self) -> None:
        embeddings = FakeEmbeddingProvider(dimension=16)
        vectors = FakePolicyVectorStore()
        service, repository = build(embeddings=embeddings, vectors=vectors)
        document = FakeDocument()

        await service.ingest(document)
        calls, upserts, replaced = (
            embeddings.document_calls,
            vectors.upsert_calls,
            repository.replace_calls,
        )

        second = await service.ingest(document)

        assert second.reused is True
        assert embeddings.document_calls == calls
        assert vectors.upsert_calls == upserts
        assert repository.replace_calls == replaced

    async def test_the_fingerprint_describes_the_provider_not_the_configuration(self) -> None:
        """The model comes from the provider, never from the configured name.

        A fingerprint exists to describe what actually produced the stored vectors. Taking
        the configured *name* instead means any deployment where the two diverge — a
        gateway aliasing one model to another, a stub, a plain misconfiguration — computes
        a fingerprint that matches, treats a stale index as current, and never re-embeds.
        The failure is silent and permanent, which is the worst shape a caching bug takes.
        """
        document = FakeDocument()

        one, _ = build(
            embeddings=FakeEmbeddingProvider(dimension=16), vectors=FakePolicyVectorStore()
        )
        two, _ = build(
            embeddings=FakeEmbeddingProvider(dimension=32), vectors=FakePolicyVectorStore()
        )

        # Same configuration on both; different providers. The fingerprints must differ,
        # because the vectors would.
        assert one.fingerprint(document) != two.fingerprint(document)

    async def test_a_different_provider_makes_a_stored_index_stale(self) -> None:
        """The end an administrator sees: re-ingesting after a model change does the work."""
        document = FakeDocument()
        first, _ = build(
            embeddings=FakeEmbeddingProvider(dimension=16), vectors=FakePolicyVectorStore()
        )
        await first.ingest(document)
        assert document.ingest_status == PolicyIngestStatus.EMBEDDED

        second, repository = build(
            embeddings=FakeEmbeddingProvider(dimension=64), vectors=FakePolicyVectorStore()
        )
        outcome = await second.ingest(document)

        assert outcome.reused is False
        assert repository.replace_calls == 1

    async def test_a_chunk_parameter_change_invalidates_the_fingerprint(self) -> None:
        """A re-run after a chunk-size change *does* redo the work.

        The passages genuinely would be different, and that is the same test rather
        than an exception to it.
        """
        service, _ = build()
        document = FakeDocument()
        await service.ingest(document)

        coarser, repository = build(config=PolicyLibrarySettings(chunk_tokens=900))
        outcome = await coarser.ingest(document)

        assert outcome.reused is False
        assert repository.replace_calls == 1

    async def test_the_policy_fingerprint_is_independent_of_the_claim_one(self) -> None:
        """Changing the claim chunker must not re-ingest the policy library.

        Two separate signatures, which is the whole reason `PolicyLibrarySettings` carries
        its own rather than borrowing `docint.index_signature`.
        """
        document = FakeDocument()
        mine, _ = build()

        theirs, _ = build(embedding_config=DocumentIntelligenceSettings(chunk_tokens=999))

        assert theirs.fingerprint(document) == mine.fingerprint(document)

    async def test_a_document_chunked_without_vectors_is_not_current_once_a_provider_exists(
        self,
    ) -> None:
        """Otherwise a library ends up permanently half-indexed and matching on half of itself."""
        service, _ = build()
        document = FakeDocument()
        await service.ingest(document)
        assert document.ingest_status == PolicyIngestStatus.CHUNKED

        with_provider, _ = build(
            embeddings=FakeEmbeddingProvider(dimension=16), vectors=FakePolicyVectorStore()
        )
        outcome = await with_provider.ingest(document)

        assert outcome.reused is False
        assert outcome.status == PolicyIngestStatus.EMBEDDED

    async def test_attempts_are_bounded(self) -> None:
        service, _ = build(config=PolicyLibrarySettings(ingest_max_attempts=2))
        document = FakeDocument(ingest_attempts=2)

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.FAILED
        assert outcome.error is not None
        assert "abandoned" in outcome.error

    async def test_force_overrides_the_attempt_ceiling(self) -> None:
        service, _ = build(config=PolicyLibrarySettings(ingest_max_attempts=2))
        document = FakeDocument(ingest_attempts=5)

        outcome = await service.ingest(document, force=True)

        assert outcome.status == PolicyIngestStatus.CHUNKED

    async def test_a_disabled_library_ingests_nothing(self) -> None:
        service, repository = build(config=PolicyLibrarySettings(enabled=False))
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.chunks == 0
        assert repository.replace_calls == 0


class TestFactsAndLinkage:
    async def test_the_declarations_are_landed_on_the_row(self) -> None:
        """Read once here rather than on every match.

        The difference between a match costing a database query and costing a re-parse
        of every PDF in the library.
        """
        service, _ = build()
        document = FakeDocument()

        await service.ingest(document)

        assert document.policy_number == "CP-4471-88210"
        assert document.insured_name == "Harborline Cold Storage & Logistics, LLC"
        assert document.broker_name == "Talbot & Rennick Insurance Brokers, Inc."
        assert document.line_of_business == "property"
        assert document.effective_date is not None
        assert "labels" in document.extracted_metadata

    async def test_a_number_that_resolves_links_the_document_to_the_book(self) -> None:
        policy_id = uuid.uuid4()
        book = FakePolicyBook({"cp447188210": policy_id})
        service, repository = build(policies=book)
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.linked_policy_id == policy_id
        assert document.policy_id == policy_id
        # The chunks' denormalised column moves with it, or a filtered search drifts.
        assert all(row.policy_id == policy_id for row in repository.rows[document.id])

    async def test_a_number_that_resolves_to_nothing_leaves_the_document_unlinked(self) -> None:
        """A first-class state, not a failure. The wording is still matchable.

        Nothing here creates a policy: a mis-read digit must not be able to put a claim
        on a contract that does not exist.
        """
        book = FakePolicyBook({})
        service, _ = build(policies=book)
        document = FakeDocument()

        outcome = await service.ingest(document)

        assert outcome.status == PolicyIngestStatus.CHUNKED
        assert outcome.linked_policy_id is None
        assert document.policy_id is None
        assert book.lookups == ["CP-4471-88210"]
