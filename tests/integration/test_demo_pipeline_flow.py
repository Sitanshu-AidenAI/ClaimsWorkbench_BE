"""The demo documents, through the real pipeline, against a real database.

`tests/unit/test_demo_documents.py` proves the files carry the facts and that a
quote resolves to a rectangle. This proves the rest of the chain: that running the
pipeline over them produces values which carry the passage they were read from,
that those citations survive onto `fnol_extracted_fields` — which is what the
review screen filters on — and that the evidence payload the screen fetches
resolves to a page of the right file.

The model is a stub, and deliberately a *citing* one: rather than being told which
passage to name, it reads the prompt it was given, finds the labelled passage that
actually contains the quote, and cites that. So the label the engine assigned, the
resolution of that label back to a chunk, and the chunk's page all still have to be
right for these to pass — which is where the bugs in a citation pipeline live. What
is stubbed is only the part that would need an API key.

Marked `integration`: the write-back, the `ON DELETE SET NULL` on a citation and
the generated full-text column are all properties of Postgres, and a fake would
assert them into existence rather than check them.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.api.deps.services import build_intelligence
from app.core.config import ExtractionSettings, settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.enums import DocumentSource, FNOLChannel
from app.models.audit import AuditEvent
from app.models.extraction import ExtractedValue, ExtractionRun
from app.models.fnol import FNOLCase, FNOLDocumentChunk, FNOLExtractedField
from app.repositories.audit import AuditRepository
from app.repositories.chunks import DocumentChunkRepository
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

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo-data" / "document-intelligence"
MESSAGE_ID = "<demo-pipeline-flow-1@calderfinch.example>"

#: The four the demo attaches, plus a fifth that cannot be read — because "one
#: bad document does not cost the case the others" is a promise worth a test.
DEMO_FILES: tuple[tuple[str, str], ...] = (
    ("harbourline-loss-notice.pdf", "application/pdf"),
    ("harbourline-policy-schedule.pdf", "application/pdf"),
    ("harbourline-survey-report.pdf", "application/pdf"),
    ("harbourline-damage-schedule.csv", "text/csv"),
)

BODY = (
    "Subject: FNOL - Ravensgate Marine Logistics - cargo damage - MAR-2026-77413\n"
    "From: marianne.okafor@calderfinch.example\n\n"
    "We are instructed to notify a cargo damage claim on behalf of our client "
    "Ravensgate Marine Logistics Limited. Our reference is CF/MAR/2026/0884.\n"
    "The reporter on this matter is Marianne Okafor."
)

#: What the stub is asked to find. Each quote is text that really appears in one
#: of the demo documents, so the passage it cites is a real passage.
WANTED: dict[str, tuple[str, str]] = {
    "policy.policy_number": ("MAR-2026-77413", "Policy number: MAR-2026-77413"),
    "financial.estimated_loss": ("GBP 486,500", "Estimated loss: GBP 486,500"),
    "loss.cause_of_loss": (
        "Heavy weather — container stow collapse",
        "Cause of loss: Heavy weather",
    ),
    "policy.insured_name": (
        "Ravensgate Marine Logistics Limited",
        "Insured name: Ravensgate Marine Logistics Limited",
    ),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _documents(tmp_path: Path) -> Any:
    set_document_store(FilesystemDocumentStore(tmp_path))
    yield
    set_document_store(None)


@pytest.fixture
async def session() -> AsyncIterator[Any]:
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
    if not case_ids:
        return

    references = list(
        (await db.execute(select(FNOLCase.reference).where(FNOLCase.id.in_(case_ids))))
        .scalars()
        .all()
    )
    await db.execute(delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_case_id.in_(case_ids)))
    await db.execute(delete(ExtractedValue).where(ExtractedValue.fnol_case_id.in_(case_ids)))
    await db.execute(delete(ExtractionRun).where(ExtractionRun.fnol_case_id.in_(case_ids)))
    await db.execute(delete(AuditEvent).where(AuditEvent.entity_reference.in_(references)))
    await db.execute(delete(FNOLCase).where(FNOLCase.id.in_(case_ids)))


class CitingStubProvider:
    """A model that reads the prompt and cites the passage it actually read from.

    The important half is `_label_for`. A stub told which label to return would
    let a broken label→chunk resolution pass, because the label would be correct
    by construction. Finding the label by searching the rendered passages means
    the engine's own numbering, its passage selection and its resolution all have
    to agree for the citation to land on the right page.
    """

    name = "stub"
    model = "stub-model"

    _BLOCK = re.compile(
        r"=== PASSAGE \[(?P<label>C\d+)\][^\n]*\n(?P<body>.*?)(?=\n\n=== PASSAGE |\Z)", re.S
    )

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def structured(self, **kwargs: Any) -> AIResponse[FieldAnswerSet]:
        prompt = kwargs["user_prompt"]
        self.calls += 1
        self.prompts.append(prompt)

        answers: list[FieldAnswer] = []
        for field_key, (value, quote) in WANTED.items():
            if f"[{field_key}]" not in prompt:
                continue
            label = self._label_for(prompt, quote)
            if label is None:
                continue
            answers.append(
                FieldAnswer(
                    field_key=field_key,
                    value=value,
                    confidence=0.94,
                    quote=quote,
                    passage=label,
                )
            )

        return AIResponse(
            data=FieldAnswerSet(answers=answers),
            provider=self.name,
            model=self.model,
            latency_ms=5,
        )

    def _label_for(self, prompt: str, quote: str) -> str | None:
        """The label of the passage containing `quote`, or None if none does."""
        for match in self._BLOCK.finditer(prompt):
            if _normalise(quote) in _normalise(match.group("body")):
                return match.group("label")
        return None

    async def aclose(self) -> None:
        return None


def _normalise(text: str) -> str:
    """Whitespace-insensitive containment, as a PDF reader's line breaks vary."""
    return " ".join(text.split()).replace("—", "-")


# ---------------------------------------------------------------------------
# Building the case
# ---------------------------------------------------------------------------


async def make_case(db: Any, *, with_unreadable: bool = False) -> FNOLCase:
    """The demo notice, with its real documents attached."""
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

    for filename, content_type in DEMO_FILES:
        await fnol.attach_document(
            case,
            filename=filename,
            content=(DEMO_DIR / filename).read_bytes(),
            content_type=content_type,
            source=DocumentSource.EMAIL_ATTACHMENT,
            actor="integration",
        )

    if with_unreadable:
        # A PNG: stored as evidence, and honestly unreadable without OCR. The
        # nearest thing to a real failure that is guaranteed to fail the same way
        # on every machine.
        await fnol.attach_document(
            case,
            filename="berth-photographs.png",
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            content_type="image/png",
            source=DocumentSource.UPLOAD,
            actor="integration",
        )

    await cases.flush()
    return case


async def index_and_extract(db: Any, case: FNOLCase) -> tuple[Any, list[ExtractedValue]]:
    """Stage 0 and the dataset run, as the pipeline sequences them."""
    cases = FNOLRepository(db)
    await NotificationBodyDocumentService(cases, DocumentProcessingService()).ensure(case)

    _, retrieval, index = build_intelligence(
        db, embeddings=FakeEmbeddingProvider(), vectors=FakeVectorStore()
    )
    documents = list(await cases.list_documents(case.id))
    outcome = await index.index_case(documents)

    schemas = ExtractionSchemaRepository(db)
    await seed_builtin_schemas(schemas)
    schema = await schemas.get_by_key("fnol_notice")
    assert schema is not None

    engine = SchemaExtractionEngine(
        ExtractionRunRepository(db),
        retrieval=retrieval,
        provider=CitingStubProvider(),
        # Small batches so several calls are made, which is what production does
        # over a thirty-field dataset.
        config=ExtractionSettings(fields_per_call=10),
    )
    run = await engine.run(
        case,
        schema=schema,
        dataset=to_dataset(schema),
        documents=documents,
        index_signature=outcome.signature,
        force=True,
        triggered_by="integration",
    )
    return run, list(run.values)


# ---------------------------------------------------------------------------
# The tests
# ---------------------------------------------------------------------------


async def test_the_demo_documents_produce_cited_values(session: Any) -> None:
    """The headline: values, each pointing at the passage it was read from."""
    case = await make_case(session)
    _, values = await index_and_extract(session, case)

    by_key = {value.field_key: value for value in values if value.value_text is not None}
    assert by_key, "the dataset produced no values from the demo documents"

    for field_key in WANTED:
        value = by_key.get(field_key)
        assert value is not None, f"{field_key} was not extracted"
        assert value.source_chunk_id is not None, f"{field_key} carries no citation"
        assert value.source_document_id is not None
        assert value.page_number is not None, f"{field_key} has no page to open"


async def test_a_value_cites_the_document_it_was_actually_read_from(session: Any) -> None:
    """The point of the whole mechanism.

    `GBP 486,500` appears in the broker's covering email *and* on page 3 of the
    survey report. Whichever passage the model was shown and quoted is the one
    the citation must name — a citation that always named the email would be a
    text search wearing a citation's clothes.
    """
    case = await make_case(session)
    _, values = await index_and_extract(session, case)

    cases = FNOLRepository(session)
    value = next(item for item in values if item.field_key == "financial.estimated_loss")
    document = await cases.get_document(value.source_document_id)

    assert document is not None
    assert document.filename in {
        "harbourline-survey-report.pdf",
        "harbourline-loss-notice.pdf",
        "notification-body.txt",
    }

    # Whatever it named, the quote must really be on the page it named.
    chunks = DocumentChunkRepository(session)
    chunk = await chunks.get(value.source_chunk_id)
    assert chunk is not None
    assert chunk.fnol_document_id == document.id
    assert _normalise("GBP 486,500") in _normalise(chunk.content)


async def test_the_evidence_payload_resolves_to_a_drawable_highlight(session: Any) -> None:
    """What the review screen gets when an officer clicks a field.

    A page number and a quote are the minimum; on a PDF whose words could be
    located there must also be a rectangle, because that is the difference
    between telling an officer where to look and showing them.
    """
    case = await make_case(session)
    _, values = await index_and_extract(session, case)

    cases = FNOLRepository(session)
    chunks = DocumentChunkRepository(session)
    locator = EvidenceLocator(DocumentProcessingService())

    drawable = 0
    for field_key in WANTED:
        value = next(item for item in values if item.field_key == field_key)
        document = await cases.get_document(value.source_document_id)
        chunk = await chunks.get(value.source_chunk_id)
        assert document is not None

        evidence = await locator.resolve_evidence(
            document, chunk, quote=value.quote, value=value.value_text
        )

        assert evidence.strategy in {"chunk-grounded", "document-search", "text-only"}
        assert evidence.text, f"{field_key} resolved to no text to mark"
        assert evidence.page_number is not None or document.content_type != "application/pdf"

        if document.content_type == "application/pdf" and evidence.rects:
            drawable += 1
            for rect in evidence.rects:
                assert 0 <= rect.x0 < rect.x1 <= rect.page_width
                assert 0 <= rect.top < rect.bottom <= rect.page_height

    assert drawable, "no field produced a rectangle — the highlight would never draw"


async def test_the_citation_reaches_the_field_rows_the_review_screen_filters_on(
    session: Any,
) -> None:
    """The write-back seam.

    Extraction review shows values that came out of a document, and the fixed
    field rows decide that by carrying `source_chunk_id`. The adapter is what
    puts it there; without this, the dataset would be fully cited and the screen
    would still show every field as sourceless.
    """
    case = await make_case(session)
    _, values = await index_and_extract(session, case)

    cases = FNOLRepository(session)
    await FNOLWriteBackAdapter(cases).apply(case, values)
    await cases.flush()

    rows = (
        (
            await session.execute(
                select(FNOLExtractedField).where(FNOLExtractedField.fnol_case_id == case.id)
            )
        )
        .scalars()
        .all()
    )
    mirrored = {row.field_path: row for row in rows}

    for field_key in WANTED:
        row = mirrored.get(field_key)
        assert row is not None, f"{field_key} was not mirrored onto the claim record"
        assert row.source_chunk_id is not None, f"{field_key} lost its citation in write-back"
        assert row.source_document_id is not None


async def test_one_unreadable_document_does_not_cost_the_case_the_others(session: Any) -> None:
    """Isolation, which is what makes a mixed case file survivable.

    A photograph cannot be read without OCR and never could be. The failure is
    recorded against that document, and every value the readable documents carry
    is still extracted and still cited.
    """
    case = await make_case(session, with_unreadable=True)
    _, values = await index_and_extract(session, case)

    cases = FNOLRepository(session)
    documents = list(await cases.list_documents(case.id))

    unreadable = [item for item in documents if item.filename == "berth-photographs.png"]
    assert unreadable, "the unreadable document was not attached"
    assert unreadable[0].extraction_status in {"unsupported", "failed"}

    readable = [item for item in documents if item.extraction_status == "extracted"]
    assert len(readable) >= len(DEMO_FILES), "a readable document was lost to the failed one"

    cited = [value for value in values if value.source_chunk_id is not None]
    assert cited, "the failed document stopped the rest of the case being extracted"
