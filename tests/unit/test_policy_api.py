"""The policy library endpoints: authorisation, validation, and the edges.

The context is overridden with in-memory doubles rather than a database, because what
is being tested is the *edge* — who may call it, what it refuses, what shape it returns
and what status code it returns — and every one of those is decided before any real
query would run.

Two groups earn their place particularly:

* **Authorisation refuses first.** The session double raises on any attribute access,
  so a route that started work before checking a role turns these into loud errors
  rather than quietly passing against a database that happens to be up. Uploading a
  wording changes what *every* future notice is matched against, which is why it is
  gated more narrowly than reading a claim.
* **`202`, not `201`.** The library entry exists and the passages do not yet. Returning
  `201` would tell the client the resource it asked for — a matchable wording — is
  ready, and it is not.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps.auth import get_current_principal, get_verifier
from app.api.deps.db import get_session
from app.api.deps.services import PolicyLibraryContext, build_policy_context
from app.core.errors import NotFoundError
from app.core.security import Principal
from app.domain import policy_matching as engine
from app.domain.enums import DocumentExtractionStatus, PolicyIngestStatus, Role

DOCUMENT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
POLICY_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")


class UnusableSession:
    """Fails if a handler gets past the authorisation gate.

    Bound to `get_session`, which is the dependency an *unauthorised* request would
    reach if a route ever started work before checking a role. Authorised requests get
    `RecordingSession` instead, through the overridden context.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"The request reached the database (`session.{name}`) despite being unauthorised."
        )


class RecordingSession:
    """A session that counts commits and does nothing else.

    Routes commit at the edge, which is the boundary that makes a multi-service
    operation atomic — so a double that refused to commit would fail every mutation
    for the wrong reason. Counting instead lets a case assert that the commit happened
    *before* the worker was enqueued.
    """

    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@dataclass
class FakeRow:
    """Stands in for a `PolicyDocument` row, with the fields the mapper reads."""

    id: uuid.UUID = DOCUMENT_ID
    filename: str = "POL-CP-4471-88210_Harborline.pdf"
    content_type: str = "application/pdf"
    size_bytes: int = 812_311
    page_count: int | None = 4
    uploaded_by: str | None = "Test User"
    created_at: datetime = field(default_factory=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    policy_number: str | None = "CP-4471-88210"
    insured_name: str | None = "Harborline Cold Storage & Logistics, LLC"
    insurer_name: str | None = "Meridian Atlantic Insurance Company"
    broker_name: str | None = "Talbot & Rennick Insurance Brokers, Inc."
    policy_type: str | None = "Commercial Property Coverage Part"
    line_of_business: str | None = "property"
    effective_date: date | None = date(2025, 3, 1)
    expiry_date: date | None = date(2026, 3, 1)
    policy_id: uuid.UUID | None = POLICY_ID
    ingest_status: str = PolicyIngestStatus.EMBEDDED
    ingest_error: str | None = None
    ingest_attempts: int = 1
    ingested_at: datetime | None = field(default_factory=lambda: datetime(2026, 8, 19, tzinfo=UTC))
    chunk_count: int = 12
    embedded_chunk_count: int = 12
    extraction_status: str = DocumentExtractionStatus.EXTRACTED
    extraction_error: str | None = None
    text_characters: int = 24_100
    extracted_metadata: dict[str, Any] = field(
        default_factory=lambda: {"postcodes": ["21226"], "labels": {}}
    )
    storage_key: str = "policy-library/ab/key"
    extracted_text: str | None = "POLICY NUMBER: CP-4471-88210"


class FakeDocuments:
    def __init__(self, rows: list[FakeRow] | None = None) -> None:
        self.rows = rows if rows is not None else [FakeRow()]

    async def list_documents(
        self,
        *,
        statuses: Any = None,
        search: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[FakeRow], int]:
        rows = self.rows
        if statuses:
            rows = [row for row in rows if row.ingest_status in set(statuses)]
        if search:
            needle = search.lower()
            rows = [row for row in rows if needle in row.filename.lower()]
        return rows[offset : offset + limit], len(rows)

    async def status_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in PolicyIngestStatus}
        for row in self.rows:
            counts[str(row.ingest_status)] += 1
        return counts

    async def count_chunks(self) -> int:
        return sum(row.chunk_count for row in self.rows)

    async def get(self, document_id: uuid.UUID) -> FakeRow | None:
        return next((row for row in self.rows if row.id == document_id), None)


class FakeLibrary:
    def __init__(self, documents: FakeDocuments) -> None:
        self._documents = documents
        self.uploaded: list[tuple[str, int]] = []
        self.removed: list[uuid.UUID] = []
        self.queued: list[uuid.UUID] = []
        self.reject: Exception | None = None

    async def upload(
        self,
        *,
        filename: str,
        content: bytes,
        declared_content_type: str | None = None,
        actor: str | None = None,
    ) -> Any:
        del declared_content_type, actor
        if self.reject is not None:
            raise self.reject
        self.uploaded.append((filename, len(content)))
        row = FakeRow(
            id=uuid.uuid4(),
            filename=filename,
            ingest_status=PolicyIngestStatus.PENDING,
            chunk_count=0,
            embedded_chunk_count=0,
            ingested_at=None,
        )
        self._documents.rows.append(row)
        from app.services.policies.library import UploadReceipt

        return UploadReceipt(document=row, duplicate=False)  # type: ignore[arg-type]

    def mark_queued(self, document: Any) -> None:
        document.ingest_status = PolicyIngestStatus.QUEUED
        self.queued.append(document.id)

    async def get(self, document_id: uuid.UUID) -> FakeRow:
        row = await self._documents.get(document_id)
        if row is None:
            raise NotFoundError("That policy document could not be found.")
        return row

    async def content(self, document: Any) -> bytes:
        del document
        return b"%PDF-1.7\nbody"

    async def remove(self, document: Any) -> None:
        self.removed.append(document.id)
        self._documents.rows = [row for row in self._documents.rows if row.id != document.id]


class FakeIngestion:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, bool]] = []

    async def ingest(self, document: Any, *, force: bool = False) -> Any:
        from app.services.policies.ingestion import IngestOutcome

        self.calls.append((document.id, force))
        document.ingest_status = PolicyIngestStatus.EMBEDDED
        return IngestOutcome(
            document_id=document.id,
            status=PolicyIngestStatus.EMBEDDED,
            chunks=12,
            embedded=12,
            reused=not force,
            linked_policy_id=POLICY_ID,
        )


class FakeMatching:
    def __init__(self, result: engine.PolicyMatchResult | None = None) -> None:
        self.result = result or _match_result()
        self.cases: list[str] = []
        self.notices: list[engine.NoticeQuery] = []

    async def match_case(self, case: Any) -> engine.PolicyMatchResult:
        self.cases.append(case.reference)
        return self.result

    async def match(self, notice: engine.NoticeQuery) -> engine.PolicyMatchResult:
        self.notices.append(notice)
        return self.result


class FakeCases:
    def __init__(self, references: dict[str, Any] | None = None) -> None:
        self.references = references or {}

    async def get_by_reference(self, reference: str) -> Any:
        return self.references.get(reference)


def _match_result() -> engine.PolicyMatchResult:
    facts = engine.PolicyDocumentFacts(
        document_id=DOCUMENT_ID,
        filename="POL-CP-4471-88210_Harborline.pdf",
        policy_id=POLICY_ID,
        policy_number="CP-4471-88210",
        insured_name="Harborline Cold Storage & Logistics, LLC",
        insurer_name="Meridian Atlantic Insurance Company",
        broker_name="Talbot & Rennick Insurance Brokers, Inc.",
        policy_type="Commercial Property Coverage Part",
        line_of_business="property",
        effective_date=date(2025, 3, 1),
        expiry_date=date(2026, 3, 1),
    )
    return engine.match(
        engine.NoticeQuery(
            policy_number="CP-4471-88210",
            insured_name="Harborline Cold Storage & Logistics, LLC",
            date_of_loss=date(2025, 9, 12),
            line_of_business="property",
        ),
        [
            engine.RetrievedPolicy(
                facts=facts,
                retrieval_score=0.94,
                excerpts=(
                    engine.Excerpt(
                        chunk_ref=f"{DOCUMENT_ID}:00003",
                        content="SECTION I - COVERED CAUSES OF LOSS.",
                        score=0.94,
                        page_number=2,
                        facet=engine.MatchFacet.PERIL,
                    ),
                ),
                facets_hit=frozenset({engine.MatchFacet.IDENTITY, engine.MatchFacet.PERIL}),
                chunks_matched=1,
            )
        ],
        strategy="hybrid-rrf",
        chunks_considered=18,
        facets_queried=(engine.MatchFacet.IDENTITY, engine.MatchFacet.PERIL),
    )


@dataclass
class Doubles:
    documents: FakeDocuments
    library: FakeLibrary
    ingestion: FakeIngestion
    matching: FakeMatching
    cases: FakeCases
    session: RecordingSession


def principal_with(*roles: str) -> Principal:
    return Principal(
        subject="00000000-0000-0000-0000-000000000042",
        username="test-user",
        email="test-user@example.com",
        full_name="Test User",
        realm_roles=frozenset(roles),
    )


@pytest.fixture
def doubles() -> Doubles:
    documents = FakeDocuments()
    return Doubles(
        documents=documents,
        library=FakeLibrary(documents),
        ingestion=FakeIngestion(),
        matching=FakeMatching(),
        cases=FakeCases({"FNOL-2026-0001": type("Case", (), {"reference": "FNOL-2026-0001"})()}),
        session=RecordingSession(),
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, doubles: Doubles) -> Iterator[FastAPI]:
    from prometheus_client import CollectorRegistry

    import app.main as app_main

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    for target in (
        "init_engine",
        "dispose_engine",
        "init_pool",
        "close_pool",
        "init_redis",
        "close_redis",
    ):
        monkeypatch.setattr(app_main, target, _noop)

    # The enqueue is a Celery call. Stubbed so the upload cases do not need a broker,
    # and recorded so the "queued" flag can be asserted.
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr(
        "app.api.v1.routes.policies._enqueue",
        lambda document_id: bool(enqueued.append(document_id)) or True,
    )

    application = app_main.create_app(metrics_registry=CollectorRegistry())

    async def _session() -> AsyncIterator[UnusableSession]:
        yield UnusableSession()

    def _context() -> PolicyLibraryContext:
        return PolicyLibraryContext(
            session=doubles.session,  # type: ignore[arg-type]
            documents=doubles.documents,  # type: ignore[arg-type]
            policies=None,  # type: ignore[arg-type]
            cases=doubles.cases,  # type: ignore[arg-type]
            library=doubles.library,  # type: ignore[arg-type]
            ingestion=doubles.ingestion,  # type: ignore[arg-type]
            retrieval=None,  # type: ignore[arg-type]
            matching=doubles.matching,  # type: ignore[arg-type]
            semantic_enabled=True,
        )

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_verifier] = lambda: None
    application.dependency_overrides[build_policy_context] = _context
    application.state.enqueued = enqueued
    yield application
    application.dependency_overrides.clear()


async def client_for(application: FastAPI, principal: Principal | None) -> AsyncClient:
    """A client that resolves to `principal`, or one carrying no credentials at all.

    The unauthenticated case sends **no** `Authorization` header rather than a bogus
    one. A header present but unverifiable is a different path — it reaches the token
    verifier, which is stubbed to `None` here — and the assertion this fixture exists to
    support is the simpler one: a caller with no credentials is refused.
    """
    if principal is None:
        application.dependency_overrides.pop(get_current_principal, None)
        return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")

    application.dependency_overrides[get_current_principal] = lambda: principal
    return AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    )


ADMIN = (Role.CLAIMS_ADMIN.value,)
OFFICER = (Role.FNOL_OFFICER.value,)
HANDLER = (Role.CLAIMS_HANDLER.value,)


class TestAuthorisation:
    async def test_reading_the_library_is_open_to_everyone_who_reads_intake(self) -> None:
        # Covered by the listing cases below; asserted here as the intent.
        from app.domain.enums import POLICY_LIBRARY_READ_ROLES, POLICY_LIBRARY_WRITE_ROLES

        assert Role.FNOL_OFFICER.value in POLICY_LIBRARY_READ_ROLES
        assert Role.CLAIMS_HANDLER.value in POLICY_LIBRARY_READ_ROLES
        # Writing is narrower: uploading changes what every future notice matches on.
        assert Role.FNOL_OFFICER.value not in POLICY_LIBRARY_WRITE_ROLES
        assert Role.CLAIMS_HANDLER.value not in POLICY_LIBRARY_WRITE_ROLES

    async def test_an_unauthenticated_caller_is_refused(self, api: FastAPI) -> None:
        async with await client_for(api, None) as client:
            response = await client.get("/api/v1/policies/documents")

        assert response.status_code == 401

    async def test_an_officer_may_read_but_not_upload(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*OFFICER)) as client:
            assert (await client.get("/api/v1/policies/documents")).status_code == 200

            refused = await client.post(
                "/api/v1/policies/documents",
                files={"file": ("policy.pdf", b"%PDF-1.7\nx", "application/pdf")},
            )

        assert refused.status_code == 403

    async def test_an_officer_may_not_delete_or_reingest(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*OFFICER)) as client:
            assert (
                await client.delete(f"/api/v1/policies/documents/{DOCUMENT_ID}")
            ).status_code == 403
            assert (
                await client.post(f"/api/v1/policies/documents/{DOCUMENT_ID}/reingest")
            ).status_code == 403

    async def test_a_handler_may_read_the_matches_for_a_case(self, api: FastAPI) -> None:
        """The review screen shows an officer which wordings a notice retrieved."""
        async with await client_for(api, principal_with(*HANDLER)) as client:
            response = await client.get("/api/v1/policies/matches/FNOL-2026-0001")

        assert response.status_code == 200

    async def test_an_unrelated_role_is_refused_everywhere(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("marketing")) as client:
            assert (await client.get("/api/v1/policies/documents")).status_code == 403
            assert (await client.get("/api/v1/policies/matches/FNOL-2026-0001")).status_code == 403


class TestListing:
    async def test_the_list_carries_the_counts_the_status_strip_needs(self, api: FastAPI) -> None:
        """Over the whole library rather than the page.

        They are what tells the client whether to keep polling, and a count of what
        happens to be on page two would stop it early.
        """
        async with await client_for(api, principal_with(*ADMIN)) as client:
            body = (await client.get("/api/v1/policies/documents")).json()

        assert body["total"] == 1
        assert body["page"] == 1
        assert body["chunk_total"] == 12
        assert body["semantic_search_enabled"] is True
        assert body["status_counts"]["embedded"] == 1
        assert body["status_counts"]["pending"] == 0

    async def test_a_row_publishes_its_metadata_and_both_chunk_counts(self, api: FastAPI) -> None:
        """Both counts, always. Their difference is the whole story when vectors lag."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            item = (await client.get("/api/v1/policies/documents")).json()["items"][0]

        assert item["policy_number"] == "CP-4471-88210"
        assert item["insured_name"] == "Harborline Cold Storage & Logistics, LLC"
        assert item["line_of_business"] == "property"
        assert item["effective_date"] == "2025-03-01"
        assert item["chunk_count"] == 12
        assert item["embedded_chunk_count"] == 12
        assert item["policy_id"] == str(POLICY_ID)

    async def test_the_full_wording_text_is_never_published(self, api: FastAPI) -> None:
        """A carrier's wording is not something a list endpoint leaks by adding a column."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            payload = (await client.get("/api/v1/policies/documents")).text

        assert "extracted_text" not in payload
        assert "storage_key" not in payload

    async def test_the_status_filter_and_search_narrow_the_list(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            filtered = await client.get("/api/v1/policies/documents?status=failed")
            searched = await client.get("/api/v1/policies/documents?search=harborline")
            missed = await client.get("/api/v1/policies/documents?search=nothing")

        assert filtered.json()["items"] == []
        assert len(searched.json()["items"]) == 1
        assert missed.json()["items"] == []

    async def test_an_invalid_status_is_refused_rather_than_ignored(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get("/api/v1/policies/documents?status=banana")

        assert response.status_code == 422

    async def test_the_page_size_is_bounded(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            assert (await client.get("/api/v1/policies/documents?page_size=500")).status_code == 422
            assert (await client.get("/api/v1/policies/documents?page=0")).status_code == 422


class TestUpload:
    async def test_an_accepted_upload_returns_202_and_queues_a_worker(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        """`202`, not `201`: the entry exists and the passages do not yet."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/documents",
                files={"file": ("wording.pdf", b"%PDF-1.7\nbody", "application/pdf")},
            )

        assert response.status_code == 202
        body = response.json()
        assert body["duplicate"] is False
        assert body["queued"] is True
        assert body["document"]["ingest_status"] == PolicyIngestStatus.QUEUED
        assert doubles.library.uploaded == [("wording.pdf", 13)]
        assert api.state.enqueued  # the worker was asked, after the commit

    async def test_a_rejected_upload_is_a_422_with_a_readable_sentence(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        from app.services.policies.library import PolicyUploadRejected

        doubles.library.reject = PolicyUploadRejected(
            "wording.docx is not a PDF. Policy documents are accepted as PDF."
        )

        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/documents",
                files={"file": ("wording.docx", b"PK\x03\x04", "application/octet-stream")},
            )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "policy_document_rejected"
        assert "not a PDF" in response.json()["error"]["message"]

    async def test_a_full_library_is_a_409(self, api: FastAPI, doubles: Doubles) -> None:
        from app.core.errors import ConflictError

        doubles.library.reject = ConflictError(
            "The policy library is limited to 5000 documents.", code="policy_library_full"
        )

        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/documents",
                files={"file": ("wording.pdf", b"%PDF-1.7\nx", "application/pdf")},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "policy_library_full"

    async def test_a_request_with_no_file_is_refused(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post("/api/v1/policies/documents")

        assert response.status_code == 422


class TestStatusAndContent:
    async def test_one_document_s_status_is_readable(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get(f"/api/v1/policies/documents/{DOCUMENT_ID}")

        assert response.status_code == 200
        assert response.json()["ingest_status"] == PolicyIngestStatus.EMBEDDED

    async def test_an_unknown_document_is_a_404(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get(f"/api/v1/policies/documents/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_a_malformed_id_is_a_422_not_a_500(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get("/api/v1/policies/documents/not-a-uuid")

        assert response.status_code == 422

    async def test_the_pdf_is_served_inline_under_its_sanitised_name(self, api: FastAPI) -> None:
        """Inline, because the point of reaching this is to read page 14 beside the excerpt."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get(f"/api/v1/policies/documents/{DOCUMENT_ID}/content")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert "POL-CP-4471-88210_Harborline.pdf" in response.headers["content-disposition"]


class TestReingestAndDelete:
    async def test_reingest_runs_inline_and_reports_the_outcome(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        """An administrator pressing retry expects an outcome, not another poll."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(f"/api/v1/policies/documents/{DOCUMENT_ID}/reingest")

        assert response.status_code == 200
        body = response.json()
        assert body["chunks"] == 12
        assert body["reused"] is True
        assert body["linked_policy_id"] == str(POLICY_ID)
        assert doubles.ingestion.calls == [(DOCUMENT_ID, False)]

    async def test_force_is_passed_through(self, api: FastAPI, doubles: Doubles) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            await client.post(f"/api/v1/policies/documents/{DOCUMENT_ID}/reingest?force=true")

        assert doubles.ingestion.calls == [(DOCUMENT_ID, True)]

    async def test_delete_returns_204_and_removes_the_entry(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.delete(f"/api/v1/policies/documents/{DOCUMENT_ID}")

        assert response.status_code == 204
        assert doubles.library.removed == [DOCUMENT_ID]

    async def test_deleting_an_unknown_document_is_a_404(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.delete(f"/api/v1/policies/documents/{uuid.uuid4()}")

        assert response.status_code == 404


class TestMatching:
    async def test_a_case_s_matches_are_returned_with_their_working(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get("/api/v1/policies/matches/FNOL-2026-0001")

        assert response.status_code == 200
        body = response.json()
        assert body["fnol_reference"] == "FNOL-2026-0001"
        assert body["strategy"] == "hybrid-rrf"
        assert body["engine_version"]
        assert doubles.matching.cases == ["FNOL-2026-0001"]

        best = body["matches"][0]
        assert best["policy_number"] == "CP-4471-88210"
        assert best["confidence"] == "exact"
        assert best["rank"] == 1
        assert best["reasons"]
        # Both scores, separately: one is "reads like this loss", one is "is the contract".
        assert best["retrieval_score"] != best["corroboration_score"]
        assert best["excerpts"][0]["page_number"] == 2
        assert best["excerpts"][0]["facet"] == "peril"
        assert best["facets_matched"] == ["identity", "peril"]
        assert {signal["signal"] for signal in best["signals"]} >= {
            "policy_number",
            "insured_name",
            "policy_period",
        }

    async def test_every_signal_is_published_including_the_silent_ones(self, api: FastAPI) -> None:
        """`missing` is a gap an officer can fill; `not_compared` does not apply here."""
        async with await client_for(api, principal_with(*ADMIN)) as client:
            body = (await client.get("/api/v1/policies/matches/FNOL-2026-0001")).json()

        outcomes = {signal["signal"]: signal["outcome"] for signal in body["matches"][0]["signals"]}
        assert outcomes["policy_number"] == "match"
        assert outcomes["risk_location"] == "missing"

    async def test_an_unknown_reference_is_a_404(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.get("/api/v1/policies/matches/FNOL-NOPE")

        assert response.status_code == 404

    async def test_matching_by_reference_through_the_post_form(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/match", json={"fnol_reference": "FNOL-2026-0001"}
            )

        assert response.status_code == 200
        assert response.json()["fnol_reference"] == "FNOL-2026-0001"
        assert doubles.matching.cases == ["FNOL-2026-0001"]

    async def test_matching_by_values_needs_no_notice(self, api: FastAPI, doubles: Doubles) -> None:
        """What makes a freshly loaded library testable.

        An administrator checks that twelve wordings retrieve sensibly by typing an
        insured name and a cause of loss, without first creating a notice.
        """
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/match",
                json={
                    "insured_name": "Harborline Cold Storage",
                    "cause_of_loss": "Ammonia release",
                    "date_of_loss": "2025-09-12",
                },
            )

        assert response.status_code == 200
        assert response.json()["fnol_reference"] is None
        assert doubles.matching.notices
        sent = doubles.matching.notices[0]
        assert sent.insured_name == "Harborline Cold Storage"
        assert sent.date_of_loss == date(2025, 9, 12)

    async def test_an_empty_query_is_refused_rather_than_matched_against_everything(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post("/api/v1/policies/match", json={})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "policy_match_query_empty"
        assert doubles.matching.notices == []

    async def test_a_free_text_query_becomes_the_loss_description(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            await client.post(
                "/api/v1/policies/match", json={"query": "ammonia leak at a cold store"}
            )

        assert doubles.matching.notices[0].loss_description == "ammonia leak at a cold store"

    async def test_an_unknown_reference_on_the_post_form_is_a_404(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with(*ADMIN)) as client:
            response = await client.post(
                "/api/v1/policies/match", json={"fnol_reference": "FNOL-NOPE"}
            )

        assert response.status_code == 404

    async def test_a_library_with_nothing_to_say_returns_an_empty_answer(
        self, api: FastAPI, doubles: Doubles
    ) -> None:
        """Not an error, and not a guess. `strategy` says why it was empty."""
        doubles.matching.result = engine.PolicyMatchResult(strategy="none")

        async with await client_for(api, principal_with(*ADMIN)) as client:
            body = (await client.get("/api/v1/policies/matches/FNOL-2026-0001")).json()

        assert body["matches"] == []
        assert body["rejected"] == []
        assert body["strategy"] == "none"
        assert body["recommended_document_id"] is None
        assert body["ambiguous"] is False
