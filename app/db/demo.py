"""The document-intelligence demo, end to end.

Ingests a notification pack from `demo-data/` — a broker's covering email and its
attachments — and runs the **real** pipeline over them: the notification body is
written out as a document, every document is read per page, cut into passages,
embedded and indexed where that is configured, the configured dataset is run
against the passages retrieval selects, and each value is written back onto the
claim record with the passage it was read from.

Nothing here writes a value. Everything printed at the end was extracted by the
same code the API runs, which is the point — a demo that asserted its own answers
would prove only that this file can type.

    uv run python -m app.db.demo                      # the default pack
    uv run python -m app.db.demo --list-packs         # what is available
    uv run python -m app.db.demo --pack verity-health-cyber
    uv run python -m app.db.demo --force              # re-read even if unchanged
    uv run python -m app.db.demo --reset              # delete the case and start again

A pack is any directory under `demo-data/` containing a `broker-notification.eml`.
Its attachments are every other file in the folder, so adding a document to a
scenario is a matter of dropping it in.

Exits non-zero when the pipeline did not produce what the review screen needs —
values, citations, and an evidence payload that resolves to a page. That makes it
a check rather than a report, so it can be run in anger after a change.

Requires Postgres and the migrations applied. Qdrant and an embedding provider
are optional: without them retrieval runs on Postgres full text alone and every
value is still cited. A model provider **is** required for the dataset path —
without `CWB_AI_API_KEY` the deterministic reader runs instead, and it cannot
cite a passage because it never read one. The run says so rather than pretending.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.api.deps.services import build_pipeline
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine, init_engine, session_scope
from app.models.extraction import ExtractedValue, ExtractionRun
from app.models.fnol import FNOLCase, FNOLDocument, FNOLDocumentChunk
from app.repositories.audit import AuditRepository
from app.repositories.chunks import DocumentChunkRepository
from app.repositories.extraction import ExtractionRunRepository, ExtractionSchemaRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.reference import ReferenceRepository
from app.services.ai.factory import get_ai_provider
from app.services.documents.service import DocumentProcessingService
from app.services.extraction.locate import EvidenceLocator
from app.services.extraction.registry import seed_builtin_schemas
from app.services.fnol.audit import AuditService
from app.services.fnol.ingestion import (
    FNOLIngestionService,
    IncomingAttachment,
    IncomingEmail,
)
from app.services.fnol.service import FNOLService
from app.services.intelligence.embedding import get_embedding_provider
from app.services.intelligence.vectors import get_vector_store

logger = get_logger(__name__)

DEMO_ROOT = Path(__file__).resolve().parent.parent.parent / "demo-data"

#: The pack run when none is named. The marine cargo scenario, which is the one
#: the surrounding documentation walks through.
DEFAULT_PACK = "document-intelligence"

#: The covering email in every pack. Its body becomes a document in its own
#: right, which is what lets a value quoted from it be cited rather than pasted
#: in.
EMAIL_FILENAME = "broker-notification.eml"

#: Content types by extension, matching `ALLOWED_EXTENSIONS` in
#: `app/services/documents/validation.py`. A pack may only contain files the
#: upload path would accept — a demo that ingested something a real officer
#: could not upload would be demonstrating the wrong thing.
CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".eml": "message/rfc822",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def available_packs() -> list[str]:
    """Every directory under `demo-data/` that looks like a notification pack."""
    return sorted(
        path.name
        for path in DEMO_ROOT.iterdir()
        if path.is_dir() and (path / EMAIL_FILENAME).is_file()
    )


def pack_attachments(directory: Path) -> list[tuple[Path, str]]:
    """The files a pack attaches, in name order.

    Discovered rather than listed, so adding a document to a pack is a matter of
    dropping it in the folder. The covering email is excluded because it is the
    notification rather than an attachment to it, and the README because it is
    for whoever opens the folder, not for the claims desk.
    """
    attachments: list[tuple[Path, str]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name in {EMAIL_FILENAME, "README.md"}:
            continue
        content_type = CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            logger.warning("demo_attachment_skipped", filename=path.name, reason="unknown_type")
            continue
        attachments.append((path, content_type))
    return attachments


#: Fields whose evidence is fetched and printed at the end.
#:
#: Chosen because each is answered by a *different* document and, for the first
#: two, on a different page of it — which is the property that separates a real
#: citation from a whole-document guess. The estimated loss in particular appears
#: in the covering email as well as on page 3 of the survey, so a citation that
#: names the survey is demonstrably not "the first place this string occurs".
SHOWCASE_FIELDS: tuple[str, ...] = (
    "policy.policy_number",
    "financial.estimated_loss",
    "loss.date_of_loss",
    "loss.cause_of_loss",
    "notification.reporter_email",
)


@dataclass(slots=True)
class DemoOutcome:
    """What the run produced, for the summary and the exit code."""

    reference: str
    documents: int
    documents_read: int
    chunks: int
    embedded: int
    run_status: str | None
    retrieval_strategy: str | None
    degraded: bool
    fields_total: int
    fields_extracted: int
    values_cited: int
    evidence_with_page: int
    evidence_with_rects: int
    problems: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def _read_email(directory: Path) -> IncomingEmail:
    """Parse the demo `.eml` into the shape a mailbox integration hands over.

    Read from the file rather than hard-coded here so the email a reader can
    open in a mail client is the same one the demo ingests — two copies of a
    fixture is two chances for the interesting one to be the stale one.
    """
    message = BytesParser(policy=policy.default).parsebytes(
        (directory / EMAIL_FILENAME).read_bytes()
    )

    body_part = message.get_body(preferencelist=("plain",))
    body = body_part.get_content() if body_part is not None else ""

    attachments = [
        IncomingAttachment(
            filename=path.name,
            content=path.read_bytes(),
            content_type=content_type,
        )
        for path, content_type in pack_attachments(directory)
    ]

    return IncomingEmail(
        sender=str(message["From"]),
        recipient=str(message["To"]),
        subject=str(message["Subject"]),
        body=body,
        message_id=str(message["Message-ID"]),
        # A fixed date would age into a "loss reported before it happened"
        # exception; the demo is about extraction, not about clock skew.
        received_at=datetime.now(UTC),
        cc=[part.strip() for part in str(message.get("Cc", "")).split(",") if part.strip()],
        attachments=attachments,
        from_broker=True,
    )


async def _reset(session: Any, message_id: str) -> None:
    """Delete the demo case so the next run starts from nothing."""
    case = (
        await session.execute(select(FNOLCase).where(FNOLCase.message_id == message_id))
    ).scalar_one_or_none()
    if case is None:
        return

    documents = (
        (await session.execute(select(FNOLDocument.id).where(FNOLDocument.fnol_case_id == case.id)))
        .scalars()
        .all()
    )
    if documents:
        await session.execute(
            delete(FNOLDocumentChunk).where(FNOLDocumentChunk.fnol_document_id.in_(documents))
        )
    await session.execute(delete(ExtractedValue).where(ExtractedValue.fnol_case_id == case.id))
    await session.execute(delete(ExtractionRun).where(ExtractionRun.fnol_case_id == case.id))
    await session.delete(case)
    await session.commit()
    print(f"reset: removed {case.reference}")


async def run_demo(
    *, pack: str = DEFAULT_PACK, force: bool = False, reset: bool = False
) -> DemoOutcome:
    """Ingest, index, extract and then report on what actually landed."""
    directory = DEMO_ROOT / pack
    if not (directory / EMAIL_FILENAME).is_file():
        raise SystemExit(
            f"No demo pack {pack!r} under demo-data/. Available: {', '.join(available_packs())}"
        )

    email = _read_email(directory)
    print(f"Pack: {pack} ({len(email.attachments)} attachments)")

    provider = get_ai_provider()
    embeddings = get_embedding_provider()
    vectors = get_vector_store()

    print("Providers")
    print(f"  model      : {'configured' if provider else 'NOT configured — deterministic reader'}")
    print(f"  embeddings : {'configured' if embeddings else 'not configured — keyword retrieval'}")
    print(f"  vectors    : {'configured' if vectors else 'not configured — keyword retrieval'}")
    print()

    async with session_scope() as session:
        if reset:
            await _reset(session, email.message_id)

        # The bundled dataset, created if this database has never seen it. The
        # pipeline needs one to run the schema-driven path at all.
        await seed_builtin_schemas(ExtractionSchemaRepository(session))
        await session.commit()

    async with session_scope() as session:
        cases = FNOLRepository(session)
        audit = AuditService(AuditRepository(session))
        ingestion = FNOLIngestionService(cases, ReferenceRepository(session), audit)
        fnol_service = FNOLService(cases, audit, documents=DocumentProcessingService())

        notification = ingestion.from_email(email)
        case, is_new = await ingestion.ingest(notification, actor="Demo")

        if is_new:
            await ingestion.record_supplied_provenance(case, notification)
            for attachment in email.attachments:
                await fnol_service.attach_document(
                    case,
                    filename=attachment.filename,
                    content=attachment.content,
                    content_type=attachment.content_type,
                    source="email_attachment",
                    actor="Demo",
                )
            print(f"ingested {case.reference} with {len(email.attachments)} attachments")
        else:
            print(f"reusing {case.reference} (already ingested)")

        reference = case.reference
        await session.commit()

    # The pipeline runs in its own session so the ingest above is durable before
    # a long model call starts, which is how the worker sequences it too.
    async with session_scope() as session:
        pipeline = build_pipeline(
            session, provider=provider, embeddings=embeddings, vectors=vectors
        )
        loaded = await FNOLRepository(session).get_by_reference(reference)
        if loaded is None:  # pragma: no cover - the ingest above just wrote it
            raise RuntimeError(f"{reference} vanished between ingest and pipeline")

        result = await pipeline.run(loaded, force=force)
        await session.commit()

        print(
            f"pipeline: state={loaded.processing_state} "
            f"retrieval_used={getattr(result, 'retrieval_used', 'n/a')}"
        )
        print()

    async with session_scope() as session:
        return await _report(session, reference)


async def _report(session: Any, reference: str) -> DemoOutcome:
    """Read back what the pipeline wrote, and check it is usable."""
    cases = FNOLRepository(session)
    chunks = DocumentChunkRepository(session)
    runs = ExtractionRunRepository(session)
    schemas = ExtractionSchemaRepository(session)

    case = await cases.get_by_reference(reference)
    if case is None:  # pragma: no cover - the caller just ran the pipeline over it
        raise RuntimeError(f"{reference} could not be read back")

    documents = list(await cases.list_documents(case.id))
    problems: list[str] = []

    print("Documents")
    total_chunks = 0
    total_embedded = 0
    read = 0
    for document in documents:
        count = await chunks.count_for_document(document.id)
        total_chunks += count
        total_embedded += document.embedded_chunk_count or 0
        if document.extraction_status == "extracted":
            read += 1
        print(
            f"  {document.filename:38s} {document.extraction_status:11s} "
            f"index={document.index_status:8s} pages={document.page_count or '-':>3} "
            f"passages={count:>3}"
        )
        if document.extraction_error:
            print(f"      ! {document.extraction_error}")
    print()

    schema = await schemas.get_default()
    if schema is None:
        problems.append("no default dataset is configured")
        return DemoOutcome(
            reference=reference,
            documents=len(documents),
            documents_read=read,
            chunks=total_chunks,
            embedded=total_embedded,
            run_status=None,
            retrieval_strategy=None,
            degraded=False,
            fields_total=0,
            fields_extracted=0,
            values_cited=0,
            evidence_with_page=0,
            evidence_with_rects=0,
            problems=problems,
        )

    run = await runs.latest_run(case.id, schema.id)
    values = await runs.list_values(case.id, schema.id)
    cited = [value for value in values if value.source_chunk_id is not None]

    print(f"Extraction run — dataset {schema.key} v{schema.version}")
    if run is None:
        problems.append("the dataset was never run against this notice")
        print("  no run recorded")
    else:
        print(
            f"  status={run.status} extracted={run.fields_extracted}/{run.fields_total} "
            f"needing review={run.fields_needing_review} strategy={run.retrieval_strategy} "
            f"degraded={run.degraded} llm_calls={run.llm_calls}"
        )
        if run.error:
            print(f"  ! {run.error}")
    print()

    print(f"Values with a citation — {len(cited)} of {len(values)}")
    filenames = {document.id: document.filename for document in documents}
    for value in sorted(values, key=lambda item: item.field_key):
        if value.value_text is None:
            continue
        source = (
            filenames.get(value.source_document_id, "—")
            if value.source_document_id is not None
            else "—"
        )
        page = f"p.{value.page_number}" if value.page_number else "  —"
        mark = "*" if value.source_chunk_id else " "
        print(
            f"  {mark} {value.field_key:34s} {str(value.value_text)[:40]:42s} "
            f"{source[:34]:36s} {page}"
        )
    print()

    # --- The click-through, which is what the review screen actually calls ---
    #
    # Resolved through the same `EvidenceLocator` the evidence endpoint uses,
    # rather than by reading the cached rectangles off the value row: the cache
    # is populated *by* that endpoint, so a demo that read it would report on
    # nothing until somebody had already clicked.
    locator = EvidenceLocator(DocumentProcessingService())
    with_page = 0
    with_rects = 0

    print("Evidence — what the review screen gets when a field is clicked")
    for field_key in SHOWCASE_FIELDS:
        found = next((item for item in values if item.field_key == field_key), None)
        if found is None or found.value_text is None:
            print(f"  {field_key:34s} not extracted")
            continue

        source_document = (
            await cases.get_document(found.source_document_id)
            if found.source_document_id is not None
            else None
        )
        if source_document is None:
            print(f"  {field_key:34s} no source document recorded")
            continue

        chunk = (
            await chunks.get(found.source_chunk_id) if found.source_chunk_id is not None else None
        )
        evidence = await locator.resolve_evidence(
            source_document, chunk, quote=found.quote, value=found.value_text
        )

        if evidence.page_number is not None:
            with_page += 1
        if evidence.rects:
            with_rects += 1

        print(
            f"  {field_key:34s} {evidence.strategy:16s} "
            f"{source_document.filename[:34]:36s} "
            f"page={evidence.page_number or '-'} rects={len(evidence.rects)}"
        )
        if evidence.text:
            print(f"      “{evidence.text[:96]}”")
        if evidence.note:
            print(f"      note: {evidence.note}")
    print()

    # --- What has to be true for the screen to work --------------------------
    if read == 0:
        problems.append("no document could be read")
    if total_chunks == 0:
        problems.append("no passages were written, so nothing can be cited")
    if not values:
        problems.append("the dataset produced no values")
    if not cited:
        problems.append(
            "no value carries a citation — extraction review will show every field "
            "without a source. With no CWB_AI_API_KEY this is expected: the "
            "deterministic reader cannot cite a passage."
        )
    if cited and with_page == 0:
        problems.append("no evidence payload resolved to a page")

    return DemoOutcome(
        reference=reference,
        documents=len(documents),
        documents_read=read,
        chunks=total_chunks,
        embedded=total_embedded,
        run_status=run.status if run else None,
        retrieval_strategy=run.retrieval_strategy if run else None,
        degraded=bool(run.degraded) if run else False,
        fields_total=run.fields_total if run else 0,
        fields_extracted=run.fields_extracted if run else 0,
        values_cited=len(cited),
        evidence_with_page=with_page,
        evidence_with_rects=with_rects,
        problems=problems,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack",
        default=DEFAULT_PACK,
        help=f"which demo-data pack to ingest (default: {DEFAULT_PACK})",
    )
    parser.add_argument(
        "--list-packs", action="store_true", help="print the packs available and exit"
    )
    parser.add_argument("--force", action="store_true", help="re-read even if nothing changed")
    parser.add_argument("--reset", action="store_true", help="delete the demo case first")
    args = parser.parse_args()

    if args.list_packs:
        for name in available_packs():
            print(name)
        return

    configure_logging(settings)

    # The pipeline's own logging is verbose and would bury the report. The run is
    # a demonstration; its logs are on stderr for anyone who wants them.
    async def _run() -> DemoOutcome:
        await init_engine(settings)
        try:
            return await run_demo(pack=args.pack, force=args.force, reset=args.reset)
        finally:
            await dispose_engine()

    outcome = asyncio.run(_run())

    print("─" * 78)
    print(
        f"{outcome.reference}: {outcome.documents_read}/{outcome.documents} documents read, "
        f"{outcome.chunks} passages ({outcome.embedded} embedded), "
        f"{outcome.fields_extracted}/{outcome.fields_total} fields, "
        f"{outcome.values_cited} cited, {outcome.evidence_with_rects} drawable highlights"
    )

    if outcome.ok:
        print("Demo complete. Open the notice in the intake screen to check it against source.")
        sys.exit(0)

    print()
    print("Incomplete:")
    for problem in outcome.problems:
        print(f"  - {problem}")
    sys.exit(1)


if __name__ == "__main__":
    main()
