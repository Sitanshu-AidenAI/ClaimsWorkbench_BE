"""A configured dataset read off a real notice, against a real database.

An email arrives with a survey report attached. The body becomes a document, both
documents are chunked and indexed, a dataset seeded from the bundled definition
is run against them, and every value lands with the passage it was read from —
which then resolves to a page and a rectangle.

Marked `integration` because the parts a fake cannot prove are exactly the parts
worth proving here:

* the partial unique index really allows only one default dataset;
* `extracted_values`' unique key really makes a re-run an update rather than a
  second row;
* `source_chunk_id`'s `ON DELETE SET NULL` really keeps the *value* when a
  re-index replaces the passage;
* the write-back really reaches `fnol_cases` and `fnol_extracted_fields`;
* an unchanged notice really costs zero model calls on the second run.

The model provider is a stub — the point is the persistence and the wiring, and a
test needing an API key would not run.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.api.deps.services import build_intelligence
from app.core.config import ExtractionSettings, settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import (
    DocumentSource,
    ExtractionRunStatus,
    ExtractionSchemaStatus,
    FNOLChannel,
)
from app.models.audit import AuditEvent
from app.models.extraction import (
    ExtractedValue,
    ExtractionRun,
    ExtractionSchema,
)
from app.models.fnol import FNOLCase, FNOLDocument, FNOLDocumentChunk, FNOLExtractedField
from app.repositories.audit import AuditRepository
from app.repositories.extraction import ExtractionRunRepository, ExtractionSchemaRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.reference import ReferenceRepository
from app.services.ai.base import AIResponse
from app.services.documents.service import DocumentProcessingService
from app.services.documents.store import FilesystemDocumentStore, set_document_store
from app.services.extraction.engine import SchemaExtractionEngine
from app.services.extraction.locate import EvidenceLocator
from app.services.extraction.prompts import FieldAnswer, FieldAnswerSet
from app.services.extraction.registry import seed_builtin_schemas, to_dataset
from app.services.fnol.adapter import FNOLWriteBackAdapter
from app.services.fnol.audit import AuditService
from app.services.fnol.body import NotificationBodyDocumentService
from app.services.fnol.ingestion import FNOLIngestionService, IncomingNotification
from app.services.fnol.service import FNOLService
from tests.fakes import FakeEmbeddingProvider, FakeVectorStore

pytestmark = pytest.mark.integration

MESSAGE_ID = "<schema-extraction-flow-1@testbrokers.test>"
TEST_SCHEMA_KEY = "integration_test_dataset"

SURVEY_PAGES = [
    "SURVEY REPORT\nUnit 7 Harborview Estate\nPrepared for Harborview Logistics Limited",
    "Policy number: SCHEMA-2026-0001\n"
    "Date of loss: 08 March 2026\n"
    "Cause of loss: escape of water from a failed riser joint",
    "Estimated loss: GBP 128,000\nRepair estimate: GBP 96,400",
]

BODY = (
    "Subject: Water damage at Harborview Unit 7\n"
    "From: alina.behrens@testbrokers.test\n\n"
    "We are instructed to notify a claim for Harborview Logistics Limited.\n"
    "The reporter on this matter is Alina Behrens."
)


def build_pdf(pages: list[str]) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    for body in pages:
        offset = 800
        for line in body.splitlines():
            document.drawString(60, offset, line)
            offset -= 20
        document.showPage()
    document.save()
    return buffer.getvalue()


SURVEY_PDF = build_pdf(SURVEY_PAGES)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> AsyncIterator[None]:
    set_document_store(FilesystemDocumentStore(tmp_path))
    yield
    set_document_store(None)


@pytest.fixture
async def session() -> AsyncIterator[Any]:
    """A session, with this test's rows removed before *and* after it runs.

    Cleaned up in a session of its own on the way out: a test that failed
    mid-transaction leaves its session unusable, and a teardown that shared it
    would fail too — leaving rows behind that break every test after it, which
    reads as a cascade of unrelated failures.
    """
    factory = get_session_factory()
    async with factory() as setup:
        await _cleanup(setup)
        await setup.commit()

    async with factory() as db:
        yield db

    async with factory() as teardown:
        await _cleanup(teardown)
        await teardown.commit()


async def _cleanup(db: Any) -> None:
    case_ids = list(
        (await db.execute(select(FNOLCase.id).where(FNOLCase.message_id == MESSAGE_ID)))
        .scalars()
        .all()
    )
    if case_ids:
        references = list(
            (await db.execute(select(FNOLCase.reference).where(FNOLCase.id.in_(case_ids))))
            .scalars()
            .all()
        )
        await db.execute(
            delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_case_id.in_(case_ids))
        )
        await db.execute(delete(AuditEvent).where(AuditEvent.entity_reference.in_(references)))
        await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))

    await db.execute(delete(ExtractionSchema).where(ExtractionSchema.key == TEST_SCHEMA_KEY))


class StubBatchProvider:
    """Answers whatever the test queued, and records what it was asked."""

    name = "stub"
    model = "stub-model"

    def __init__(self, answers: dict[str, FieldAnswer]) -> None:
        self._answers = answers
        self.prompts: list[str] = []
        self.calls = 0

    async def structured(self, **kwargs: Any) -> AIResponse[FieldAnswerSet]:
        prompt = kwargs["user_prompt"]
        self.prompts.append(prompt)
        self.calls += 1
        return AIResponse(
            data=FieldAnswerSet(
                answers=[
                    answer
                    for key, answer in self._answers.items()
                    if f"[{key}]" in prompt and _label_present(prompt, answer.passage)
                ]
            ),
            provider=self.name,
            model=self.model,
            latency_ms=5,
        )

    async def aclose(self) -> None:
        return None


def _label_present(prompt: str, label: str | None) -> bool:
    return label is None or f"PASSAGE [{label}]" in prompt


async def make_dataset(db: Any) -> ExtractionSchema:
    """A small dataset whose keys match the claim record, so write-back applies."""
    schemas = ExtractionSchemaRepository(db)
    schema = ExtractionSchema(
        key=TEST_SCHEMA_KEY,
        name="Integration test dataset",
        version=1,
        status=ExtractionSchemaStatus.ACTIVE,
        is_builtin=False,
        is_default=False,
        review_threshold=0.6,
    )
    from app.models.extraction import ExtractionSchemaField

    schema.fields = [
        ExtractionSchemaField(
            key="policy.policy_number",
            label="Policy number",
            description="The policy or certificate number the risk is written under.",
            data_type="string",
            group_label="Policy",
            required=True,
            position=0,
        ),
        ExtractionSchemaField(
            key="financial.estimated_loss",
            label="Estimated loss",
            description="The estimated value of the loss being claimed.",
            data_type="money",
            group_label="Financial",
            position=1,
        ),
        ExtractionSchemaField(
            key="notification.reporter_name",
            label="Reported by",
            description="The name of the person who reported this loss.",
            data_type="string",
            group_label="Notification",
            position=2,
        ),
    ]
    schemas.add(schema)
    await schemas.flush()
    return schema


async def make_case(db: Any) -> FNOLCase:
    cases = FNOLRepository(db)
    audit = AuditService(AuditRepository(db))
    ingestion = FNOLIngestionService(cases, ReferenceRepository(db), audit)
    fnol = FNOLService(cases, audit, documents=DocumentProcessingService())

    case, _ = await ingestion.ingest(
        IncomingNotification(
            channel=FNOLChannel.BROKER_EMAIL,
            received_at=datetime.now(UTC),
            body=BODY,
            message_id=MESSAGE_ID,
            source_metadata={},
        ),
        actor="integration",
    )
    await fnol.attach_document(
        case,
        filename="survey-report.pdf",
        content=SURVEY_PDF,
        content_type="application/pdf",
        source=DocumentSource.EMAIL_ATTACHMENT,
        actor="integration",
    )
    await cases.flush()
    return case


async def prepare(db: Any, case: FNOLCase) -> tuple[list[FNOLDocument], str]:
    """Body document, then indexing — the pipeline's stage 0, run directly."""
    cases = FNOLRepository(db)
    body = NotificationBodyDocumentService(cases, DocumentProcessingService())
    await body.ensure(case)

    _, _, index = build_intelligence(
        db, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
    )
    documents = list(await cases.list_documents(case.id))
    outcome = await index.index_case(documents)
    return documents, outcome.signature


def make_engine(db: Any, provider: Any, *, config: ExtractionSettings | None = None) -> Any:
    _, retrieval, _ = build_intelligence(
        db, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
    )
    return SchemaExtractionEngine(
        ExtractionRunRepository(db),
        retrieval=retrieval,
        provider=provider,
        config=config or ExtractionSettings(fields_per_call=10),
    )


ANSWERS = {
    "policy.policy_number": FieldAnswer(
        field_key="policy.policy_number",
        value="SCHEMA-2026-0001",
        confidence=0.95,
        quote="Policy number: SCHEMA-2026-0001",
        passage=None,
    ),
    "financial.estimated_loss": FieldAnswer(
        field_key="financial.estimated_loss",
        value="GBP 128,000",
        confidence=0.9,
        quote="Estimated loss: GBP 128,000",
        passage=None,
    ),
}


def with_passage(answer: FieldAnswer, label: str) -> FieldAnswer:
    return answer.model_copy(update={"passage": label})


@pytest.fixture
async def api(session: Any) -> AsyncIterator[Any]:
    """The real application, over the test's own session and a stub provider.

    Built here rather than in `conftest` because these tests are the only ones
    that need the HTTP surface *and* a real database at once — every other
    integration test drives the services directly, and every other API test runs
    with no database at all.
    """
    from collections.abc import AsyncIterator as _AsyncIterator

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from prometheus_client import CollectorRegistry

    import app.main as app_main
    from app.api.deps.auth import get_current_principal, get_verifier
    from app.api.deps.db import get_session
    from app.api.deps.services import (
        get_ai_provider_dependency,
        get_embedding_provider_dependency,
        get_vector_store_dependency,
    )
    from app.core.security import Principal

    application: FastAPI = app_main.create_app(metrics_registry=CollectorRegistry())

    async def _session() -> _AsyncIterator[Any]:
        yield session

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_verifier] = lambda: None
    application.dependency_overrides[get_current_principal] = lambda: Principal(
        subject="00000000-0000-0000-0000-000000000099",
        username="api-test",
        email="api-test@example.test",
        full_name="API Test",
        realm_roles=frozenset({"fnol-officer", "claims-admin", "business-admin"}),
    )
    application.dependency_overrides[get_ai_provider_dependency] = lambda: _AnswerFirstPassage(
        ANSWERS
    )
    application.dependency_overrides[get_embedding_provider_dependency] = lambda: (
        FakeEmbeddingProvider()
    )
    application.dependency_overrides[get_vector_store_dependency] = lambda: FakeVectorStore()

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as http:
        yield http

    application.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSeeding:
    async def test_the_bundled_datasets_seed_once_and_stay_put(self, session: Any) -> None:
        schemas = ExtractionSchemaRepository(session)
        await seed_builtin_schemas(schemas)
        await session.commit()

        first = await schemas.get_by_key("fnol_notice")
        assert first is not None
        assert first.is_default is True
        assert len(first.fields) >= 25

        # An administrator rewrites a description; a second seed must not undo it.
        target = next(field for field in first.fields if field.key == "policy.policy_number")
        target.description = "An administrator's own wording for this question."
        await session.commit()

        result = await seed_builtin_schemas(schemas)
        await session.commit()

        assert result == {"schemas": 0, "fields": 0}
        again = await schemas.get_by_key("fnol_notice")
        assert again is not None
        reread = next(field for field in again.fields if field.key == "policy.policy_number")
        assert reread.description == "An administrator's own wording for this question."

    async def test_only_one_dataset_can_be_the_default(self, session: Any) -> None:
        """Enforced by Postgres, not by a service.

        Two concurrent edits must not both win and leave a desk whose notices are
        read against whichever row a query happened to return first.
        """
        schemas = ExtractionSchemaRepository(session)
        await seed_builtin_schemas(schemas)
        await session.commit()

        rival = ExtractionSchema(key=TEST_SCHEMA_KEY, name="Rival", version=1, is_default=True)
        schemas.add(rival)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


class TestTheWholeFlow:
    async def test_a_dataset_is_read_off_the_notice_and_every_value_is_cited(
        self, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        # Answer with whichever passage label the prompt actually printed, so the
        # test asserts the citation *chain* rather than a hard-coded label.
        provider = _AnswerFirstPassage(ANSWERS)
        engine = make_engine(session, provider)

        outcome = await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()

        assert outcome.run.status == ExtractionRunStatus.COMPLETED
        assert outcome.run.fields_total == 3
        assert outcome.run.fields_extracted == 2

        by_key = {value.field_key: value for value in outcome.values}
        policy = by_key["policy.policy_number"]
        assert policy.value_text == "SCHEMA-2026-0001"
        assert policy.source_chunk_id is not None
        assert policy.source_document_id is not None
        assert policy.page_number is not None

        amount = by_key["financial.estimated_loss"]
        assert amount.value_json == 12_800_000

        # A field the passages did not state is present and empty, not absent.
        assert by_key["notification.reporter_name"].value_text is None

    async def test_the_body_is_a_document_and_is_in_every_prompt(self, session: Any) -> None:
        """A value read from the broker's own email must be citable like any other."""
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        body = [
            document
            for document in documents
            if document.source == DocumentSource.NOTIFICATION_BODY
        ]
        assert len(body) == 1
        assert body[0].chunk_count > 0
        assert body[0].page_offsets == [[0, len(BODY)]]

        provider = _AnswerFirstPassage({})
        engine = make_engine(session, provider, config=ExtractionSettings(fields_per_call=1))
        await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )

        assert len(provider.prompts) == 3
        for prompt in provider.prompts:
            assert "Alina Behrens" in prompt

    async def test_a_re_run_updates_the_values_rather_than_adding_a_second_set(
        self, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        engine = make_engine(session, _AnswerFirstPassage(ANSWERS))
        await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()

        await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
            force=True,
        )
        await session.commit()

        rows = list(
            (
                await session.execute(
                    select(ExtractedValue).where(ExtractedValue.fnol_case_id == case.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3

    async def test_an_unchanged_notice_costs_no_model_call_on_the_second_run(
        self, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        provider = _AnswerFirstPassage(ANSWERS)
        engine = make_engine(session, provider)

        await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()
        first = provider.calls
        assert first > 0

        second = await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        assert provider.calls == first
        assert second.reused is True

        runs = list(
            (
                await session.execute(
                    select(ExtractionRun).where(ExtractionRun.fnol_case_id == case.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(runs) == 1

    async def test_the_values_reach_the_claim_record(self, session: Any) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        engine = make_engine(session, _AnswerFirstPassage(ANSWERS))
        outcome = await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await FNOLWriteBackAdapter(FNOLRepository(session)).apply(case, outcome.values)
        await session.commit()

        assert case.policy_number == "SCHEMA-2026-0001"
        assert case.estimated_loss_minor == 12_800_000

        rows = {
            row.field_path: row
            for row in (
                await session.execute(
                    select(FNOLExtractedField).where(FNOLExtractedField.fnol_case_id == case.id)
                )
            )
            .scalars()
            .all()
        }
        assert rows["policy.policy_number"].value_text == "SCHEMA-2026-0001"
        assert rows["policy.policy_number"].source_chunk_id is not None

    async def test_a_re_index_costs_the_citation_and_keeps_the_value(self, session: Any) -> None:
        """`ON DELETE SET NULL`, proved against the real constraint.

        An officer would rather read "SCHEMA-2026-0001, source no longer
        available" than find the field empty because a paragraph was re-cut.
        """
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        engine = make_engine(session, _AnswerFirstPassage(ANSWERS))
        outcome = await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()

        value = next(v for v in outcome.values if v.field_key == "policy.policy_number")
        assert value.source_chunk_id is not None

        await session.execute(
            delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_case_id == case.id)
        )
        await session.commit()
        await session.refresh(value)

        assert value.source_chunk_id is None
        assert value.value_text == "SCHEMA-2026-0001"

    async def test_a_citation_resolves_to_a_page_and_a_rectangle(self, session: Any) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        engine = make_engine(session, _AnswerFirstPassage(ANSWERS))
        outcome = await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()

        value = next(v for v in outcome.values if v.field_key == "policy.policy_number")
        document = await FNOLRepository(session).get_document(value.source_document_id)
        assert document is not None

        chunk = await session.get(FNOLDocumentChunk, value.source_chunk_id)
        location = await EvidenceLocator(DocumentProcessingService()).resolve_evidence(
            document, chunk, quote=value.quote, value=value.value_text
        )

        assert location.strategy == "chunk-grounded"
        assert location.page_number is not None
        if document.content_type == "application/pdf":
            assert location.rects, location.note
            assert location.rects[0].page_width > 0

    async def test_deleting_a_case_removes_its_runs_and_values(self, session: Any) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        documents, signature = await prepare(session, case)
        await session.commit()

        engine = make_engine(session, _AnswerFirstPassage(ANSWERS))
        await engine.run(
            case,
            schema=schema,
            dataset=to_dataset(schema),
            documents=documents,
            index_signature=signature,
        )
        await session.commit()
        case_id = case.id

        await session.execute(delete(FNOLCase).where(FNOLCase.id == case_id))
        await session.commit()

        remaining = (
            (
                await session.execute(
                    select(ExtractedValue).where(ExtractedValue.fnol_case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
        assert list(remaining) == []


class _AnswerFirstPassage:
    """Answers citing a passage the prompt actually printed, preferring the report.

    Reading the label back out of the prompt — rather than hard-coding `C1` —
    means the test exercises the same chain the model does: label printed, label
    echoed, label resolved to a row. Preferring the attachment over the
    notification body mirrors what the system prompt tells a real model to do,
    and it is what makes the rectangle assertions meaningful: the body has no
    page geometry, so a citation into it can only ever be text-only.
    """

    name = "stub"
    model = "stub-model"

    #: The filename that appears in the passage header of the attachment.
    prefers = "survey-report.pdf"

    def __init__(self, answers: dict[str, FieldAnswer]) -> None:
        self._answers = answers
        self.prompts: list[str] = []
        self.calls = 0

    async def structured(self, **kwargs: Any) -> AIResponse[FieldAnswerSet]:
        prompt = kwargs["user_prompt"]
        self.prompts.append(prompt)
        self.calls += 1

        label = self._label(prompt)
        answers = [
            answer.model_copy(update={"passage": label})
            for key, answer in self._answers.items()
            if f"[{key}]" in prompt
        ]
        return AIResponse(
            data=FieldAnswerSet(answers=answers),
            provider=self.name,
            model=self.model,
            latency_ms=5,
        )

    def _label(self, prompt: str) -> str | None:
        import re

        headers = re.findall(r"PASSAGE \[(C\d+)\] — ([^\n=]+)", prompt)
        for label, where in headers:
            if self.prefers in where:
                return label
        return headers[0][0] if headers else None

    async def aclose(self) -> None:
        return None


class TestTheApi:
    """The routes the frontend actually calls, over a real database.

    One class rather than one per endpoint, because the interesting assertions
    are about the sequence: configure a dataset, run it, read the values, click
    through to the evidence, correct one, and find the correction survives the
    next run.
    """

    async def test_the_bundled_dataset_is_listed_with_its_groups(
        self, api: Any, session: Any
    ) -> None:
        await seed_builtin_schemas(ExtractionSchemaRepository(session))
        await session.commit()

        response = await api.get("/api/v1/extraction/schemas")
        assert response.status_code == 200

        datasets = {item["key"]: item for item in response.json()["items"]}
        fnol = datasets["fnol_notice"]
        assert fnol["is_default"] is True
        assert fnol["is_builtin"] is True
        assert fnol["field_count"] >= 25
        # The groups drive the review screen's panels, in order.
        assert fnol["groups"][:2] == ["Notification", "Policy"]
        assert all(field["description"] for field in fnol["fields"])

    async def test_a_dataset_can_be_created_and_its_fields_replaced(self, api: Any) -> None:
        created = await api.post(
            "/api/v1/extraction/schemas",
            json={
                "key": TEST_SCHEMA_KEY,
                "name": "Integration test dataset",
                "review_threshold": 0.5,
                "fields": [
                    {
                        "key": "policy.policy_number",
                        "label": "Policy number",
                        "description": "The policy or certificate number the risk sits under.",
                        "data_type": "string",
                        "group_label": "Policy",
                        "required": True,
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["version"] == 1
        assert created.json()["field_count"] == 1

        replaced = await api.put(
            f"/api/v1/extraction/schemas/{TEST_SCHEMA_KEY}/fields",
            json={
                "fields": [
                    {
                        "key": "vessel.imo_number",
                        "label": "IMO number",
                        "description": "The vessel's seven-digit IMO identification number.",
                        "data_type": "string",
                        "group_label": "Vessel",
                    },
                    {
                        "key": "policy.policy_number",
                        "label": "Policy number",
                        "description": "The policy or certificate number the risk sits under.",
                        "data_type": "string",
                        "group_label": "Policy",
                    },
                ]
            },
        )
        assert replaced.status_code == 200, replaced.text
        body = replaced.json()
        # Bumped, so every open notice re-reads against the new questions.
        assert body["version"] == 2
        assert [field["key"] for field in body["fields"]] == [
            "vessel.imo_number",
            "policy.policy_number",
        ]
        assert body["groups"] == ["Vessel", "Policy"]

    async def test_a_duplicate_key_is_refused(self, api: Any) -> None:
        payload = {"key": TEST_SCHEMA_KEY, "name": "First", "fields": []}
        assert (await api.post("/api/v1/extraction/schemas", json=payload)).status_code == 201
        clash = await api.post("/api/v1/extraction/schemas", json={**payload, "name": "Second"})
        assert clash.status_code == 409

    async def test_an_unknown_data_type_is_refused_with_the_list(self, api: Any) -> None:
        response = await api.post(
            "/api/v1/extraction/schemas",
            json={
                "key": TEST_SCHEMA_KEY,
                "name": "X",
                "fields": [
                    {
                        "key": "a.b",
                        "label": "A",
                        "description": "Something.",
                        "data_type": "quaternion",
                    }
                ],
            },
        )
        assert response.status_code == 422
        assert "quaternion" in response.text

    async def test_the_builtin_dataset_cannot_be_deleted(self, api: Any, session: Any) -> None:
        await seed_builtin_schemas(ExtractionSchemaRepository(session))
        await session.commit()

        response = await api.delete("/api/v1/extraction/schemas/fnol_notice")
        assert response.status_code == 422
        assert "ships with the product" in response.text

    async def test_running_a_dataset_returns_the_values_with_their_evidence(
        self, api: Any, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        response = await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["dataset"]["key"] == TEST_SCHEMA_KEY
        assert body["run"]["status"] in {"completed", "partial"}
        assert body["run"]["fields_total"] == 3

        values = {item["field_key"]: item for item in body["values"]}
        policy = values["policy.policy_number"]
        assert policy["value"] == "SCHEMA-2026-0001"
        assert policy["source_chunk_id"] is not None
        assert policy["source_document_filename"] == "survey-report.pdf"
        assert policy["required"] is True
        assert values["financial.estimated_loss"]["typed_value"] == 12_800_000

    async def test_the_evidence_endpoint_returns_a_page_and_rectangles(
        self, api: Any, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )

        response = await api.get(
            f"/api/v1/fnol/{case.reference}/extraction/values/policy.policy_number/evidence",
            params={"schema_key": schema.key},
        )
        assert response.status_code == 200, response.text
        evidence = response.json()

        assert evidence["strategy"] == "chunk-grounded"
        assert evidence["filename"] == "survey-report.pdf"
        assert evidence["content_type"] == "application/pdf"
        assert evidence["page_number"] is not None
        assert evidence["text"]
        assert evidence["char_start"] is not None
        assert evidence["rects"], evidence["note"]

        rect = evidence["rects"][0]
        # Everything a viewer needs to scale without a second request.
        assert set(rect) == {
            "page_number",
            "x0",
            "top",
            "x1",
            "bottom",
            "page_width",
            "page_height",
        }
        assert rect["page_width"] > 0

    async def test_evidence_for_a_value_with_no_source_is_answered_not_errored(
        self, api: Any, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )

        response = await api.get(
            f"/api/v1/fnol/{case.reference}/extraction/values/notification.reporter_name/evidence",
            params={"schema_key": schema.key},
        )
        assert response.status_code == 200
        assert response.json()["strategy"] == "none"
        assert response.json()["note"]

    async def test_locate_returns_every_occurrence_for_stepping_through(
        self, api: Any, session: Any
    ) -> None:
        case = await make_case(session)
        await make_dataset(session)
        await session.commit()

        documents = list(await FNOLRepository(session).list_documents(case.id))
        report = next(d for d in documents if d.filename == "survey-report.pdf")

        response = await api.post(
            f"/api/v1/fnol/{case.reference}/documents/{report.id}/locate",
            json={"text": "Policy number"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["filename"] == "survey-report.pdf"
        assert body["total"] >= 1
        assert body["items"][0]["page_number"] is not None
        assert body["items"][0]["snippet"]

    async def test_a_correction_sticks_and_survives_the_next_run(
        self, api: Any, session: Any
    ) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )

        corrected = await api.patch(
            f"/api/v1/fnol/{case.reference}/extraction/values/policy.policy_number",
            params={"schema_key": schema.key},
            json={"value": "CORRECTED-BY-A-PERSON", "reason": "The slip says otherwise."},
        )
        assert corrected.status_code == 200, corrected.text
        body = corrected.json()
        assert body["value"] == "CORRECTED-BY-A-PERSON"
        assert body["human_modified"] is True
        assert body["original_value"] == "SCHEMA-2026-0001"
        assert body["source"] == "human"
        # A person's answer has no model confidence and no passage behind it.
        assert body["confidence"] is None
        assert body["source_chunk_id"] is None

        await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )

        again = await api.get(
            f"/api/v1/fnol/{case.reference}/extraction", params={"schema_key": schema.key}
        )
        values = {item["field_key"]: item for item in again.json()["values"]}
        assert values["policy.policy_number"]["value"] == "CORRECTED-BY-A-PERSON"
        assert values["policy.policy_number"]["human_modified"] is True

    async def test_a_correction_reaches_the_claim_record(self, api: Any, session: Any) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        await api.post(
            f"/api/v1/fnol/{case.reference}/extraction",
            json={"schema_key": schema.key, "force": True, "run_pipeline": False},
        )
        await api.patch(
            f"/api/v1/fnol/{case.reference}/extraction/values/policy.policy_number",
            params={"schema_key": schema.key},
            json={"value": "CORRECTED-BY-A-PERSON"},
        )

        await session.refresh(case)
        assert case.policy_number == "CORRECTED-BY-A-PERSON"

    async def test_reading_a_notice_nobody_has_run_says_so(self, api: Any, session: Any) -> None:
        case = await make_case(session)
        schema = await make_dataset(session)
        await session.commit()

        response = await api.get(
            f"/api/v1/fnol/{case.reference}/extraction", params={"schema_key": schema.key}
        )
        assert response.status_code == 200
        assert response.json()["run"] is None
        assert response.json()["note"]
        # The dataset still travels, so the screen can draw empty panels.
        assert response.json()["dataset"]["field_count"] == 3
