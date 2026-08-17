"""Reading a notice against a configured dataset.

One run is: retrieve per field, batch the fields, ask the model once per batch,
and write a value per field with the passage it was read from.

Three decisions shape everything here.

**Retrieval is per field, extraction is per batch.** Retrieval per field is where
the accuracy is — a field's own description, written in a document's vocabulary,
finds the paragraph a shared section query misses. Extraction per field is where
the *cost* is, and it is avoidable: fields whose retrieval landed on overlapping
passages can be answered in one call over the union of them. So a thirty-field
dataset costs thirty cheap searches and three model calls, not thirty of each.

**The notification body is always in the prompt.** It is now a document like any
other and is retrieved like any other — but it is also the notice itself, it is
small, and a retrieval miss on it would be this mechanism's one unforgivable
failure. Its passages are prepended to every batch regardless of what retrieval
thought, bounded by `body_chunks_always_included`.

**A batch failing is not a run failing.** One batch of ten fields whose model call
times out leaves the other twenty fields extracted and the run `partial`. The
alternative — losing twenty good values because a thirty-first was unlucky — is
the behaviour that makes an officer stop trusting the screen.

Nothing here knows what a claim is. Mapping a dataset's values onto the claim
record is `app.services.fnol.adapter`, which is the only module that knows both.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import ExtractionSettings, settings
from app.core.logging import get_logger
from app.domain.enums import DocumentSource, ExtractionRunStatus, FieldSource
from app.models.extraction import (
    ExtractedValue,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaField,
)
from app.models.fnol import FNOLCase, FNOLDocument, FNOLDocumentChunk
from app.repositories.extraction import REUSABLE_RUN_STATUSES, ExtractionRunRepository
from app.services.ai.base import AIProvider, AIProviderError
from app.services.extraction.prompts import (
    SYSTEM_PROMPT,
    FieldAnswer,
    FieldAnswerSet,
    render_fields,
    render_user_prompt,
)
from app.services.extraction.schema import DatasetSchema, FieldSpec, stores_typed_value
from app.services.intelligence.retrieval import RetrievalResult, RetrievalService, RetrievedChunk

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Passage:
    """One passage as it was shown to the model, under the label it was shown as."""

    label: str
    chunk: FNOLDocumentChunk
    filename: str
    score: float


@dataclass(slots=True)
class RunOutcome:
    """What one run did, for the caller and for the API."""

    run: ExtractionRun
    values: list[ExtractedValue] = field(default_factory=list)
    reused: bool = False
    #: Set when the dataset could not be run at all — no schema, no fields, the
    #: module switched off. Not an error; the pipeline carries on without values.
    skipped_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.run.status in (ExtractionRunStatus.COMPLETED, ExtractionRunStatus.PARTIAL)


@dataclass(slots=True)
class _Batch:
    """A set of fields answered by one model call, and the passages they share."""

    specs: list[FieldSpec]
    passages: list[Passage]

    def by_label(self) -> dict[str, Passage]:
        return {passage.label: passage for passage in self.passages}


class FieldVectorCache:
    """A dataset's cached query embeddings, kept on the field rows.

    A field's retrieval query is fixed until somebody edits the field, so
    embedding it on every run of every notice is a network round trip bought back
    for the price of a column. The cache key is a hash of the query text and the
    model that embedded it — so editing a description or switching models
    invalidates it without anyone having to remember to.
    """

    def __init__(self, schema: ExtractionSchema | None) -> None:
        self._rows: dict[str, ExtractionSchemaField] = (
            {row.key: row for row in schema.fields} if schema is not None else {}
        )

    @staticmethod
    def digest(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()

    def get(self, key: str, query: str, model: str) -> list[float] | None:
        row = self._rows.get(key)
        if row is None or not row.query_embedding:
            return None
        if row.query_embedding_model != model:
            return None
        if row.query_embedding_hash != self.digest(query):
            return None
        return list(row.query_embedding)

    def put(self, key: str, query: str, model: str, vector: list[float]) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        row.query_embedding = list(vector)
        row.query_embedding_model = model
        row.query_embedding_hash = self.digest(query)


class SchemaExtractionEngine:
    """Runs a configured dataset against one notice."""

    def __init__(
        self,
        runs: ExtractionRunRepository,
        *,
        retrieval: RetrievalService,
        provider: AIProvider | None,
        config: ExtractionSettings | None = None,
    ) -> None:
        self._runs = runs
        self._retrieval = retrieval
        self._provider = provider
        self._config = config or settings.extraction

    # -- Public surface ------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether this engine can read a dataset at all.

        A dataset is an arbitrary set of questions, so answering one needs a
        model. The deterministic reader in `app.domain.heuristics` answers the
        fixed FNOL schema and only that one, and cannot stand in.

        The pipeline asks this before choosing a path: a deployment with no API
        key keeps the deterministic reading it has always had, rather than a
        dataset run that correctly reports having read nothing.
        """
        return self._provider is not None

    async def run(
        self,
        case: FNOLCase,
        *,
        schema: ExtractionSchema,
        dataset: DatasetSchema,
        documents: list[FNOLDocument],
        index_signature: str = "",
        force: bool = False,
        triggered_by: str | None = None,
    ) -> RunOutcome:
        """Extract `dataset` from `case`, reusing the last run when nothing moved."""
        fingerprint = self.fingerprint(case, dataset, documents, index_signature)

        if not force:
            reusable = await self._reusable(case.id, schema.id, fingerprint)
            if reusable is not None:
                logger.info(
                    "extraction_run_reused",
                    reference=case.reference,
                    schema=dataset.key,
                    run_id=str(reusable.id),
                )
                values = list(await self._runs.list_values(case.id, schema.id))
                return RunOutcome(run=reusable, values=values, reused=True)

        run = self._runs.add_run(
            ExtractionRun(
                fnol_case_id=case.id,
                schema_id=schema.id,
                schema_key=dataset.key,
                schema_version=dataset.version,
                status=ExtractionRunStatus.RUNNING,
                fingerprint=fingerprint,
                fields_total=len(dataset.fields),
                started_at=datetime.now(UTC),
                triggered_by=triggered_by,
            )
        )
        await self._runs.flush()

        if not dataset.fields:
            return self._settle_empty(run, "This dataset has no enabled fields.")
        if self._provider is None:
            # The deterministic reader in `app.domain.heuristics` answers the FNOL
            # schema and only the FNOL schema, so it cannot stand in for an
            # arbitrary dataset. Saying so is better than returning nothing and
            # letting a reviewer wonder which of the two happened.
            return self._settle_empty(
                run, "No model provider is configured, so no dataset fields were read."
            )

        try:
            return await self._execute(
                case, run=run, schema=schema, dataset=dataset, documents=documents
            )
        except Exception as exc:
            logger.exception("extraction_run_failed", reference=case.reference, schema=dataset.key)
            run.status = ExtractionRunStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = datetime.now(UTC)
            return RunOutcome(run=run)

    @staticmethod
    def fingerprint(
        case: FNOLCase,
        dataset: DatasetSchema,
        documents: list[FNOLDocument],
        index_signature: str = "",
    ) -> str:
        """Everything a run's answers depend on, hashed.

        The dataset's *questions* are in here, not just its version number: an
        administrator who rewrites a field's description has changed what the
        model is asked, and a run that kept its old answer would be showing a
        value extracted against a question nobody asks any more.
        """
        digest = hashlib.sha256()
        digest.update((case.source_body or "").encode("utf-8", errors="ignore"))
        digest.update(b"\x00")
        digest.update(f"{dataset.key}:{dataset.version}".encode())
        for spec in dataset.fields:
            digest.update(b"\x00")
            digest.update(
                f"{spec.key}|{spec.data_type}|{spec.query}|{spec.extraction_hint or ''}".encode(
                    "utf-8", errors="ignore"
                )
            )
        for signature in sorted(
            f"{document.checksum_sha256 or ''}:{document.index_fingerprint or ''}"
            for document in documents
        ):
            digest.update(b"\x00")
            digest.update(signature.encode("utf-8"))
        if index_signature:
            digest.update(b"\x00")
            digest.update(index_signature.encode("utf-8"))
        return digest.hexdigest()

    # -- The work ------------------------------------------------------------

    async def _execute(
        self,
        case: FNOLCase,
        *,
        run: ExtractionRun,
        schema: ExtractionSchema,
        dataset: DatasetSchema,
        documents: list[FNOLDocument],
    ) -> RunOutcome:
        filenames = {document.id: document.filename for document in documents}
        body_document_ids = {
            document.id
            for document in documents
            if document.source == DocumentSource.NOTIFICATION_BODY
        }

        retrieved = await self._retrieve(case.id, schema, dataset)
        run.chunks_available = max(
            (result.chunks_available for result in retrieved.values()), default=0
        )
        run.retrieval_strategy = _dominant_strategy(retrieved.values())
        run.degraded = any(result.degraded for result in retrieved.values())

        body = await self._body_passages(body_document_ids)
        batches = self._batch(dataset, retrieved, body, filenames)

        answers, calls, failures, latency, model = await self._ask(batches, channel=case.channel)

        run.llm_calls = calls
        run.latency_ms = latency
        run.provider = getattr(self._provider, "name", None)
        run.model = model

        labels = {spec.key: batch.by_label() for batch in batches for spec in batch.specs}
        values = await self._persist(
            case, run=run, schema=schema, dataset=dataset, answers=answers, labels=labels
        )

        run.fields_extracted = sum(1 for value in values if value.value_text is not None)
        run.fields_needing_review = sum(1 for value in values if value.needs_review)
        run.fields_failed = failures
        run.status = ExtractionRunStatus.PARTIAL if failures else ExtractionRunStatus.COMPLETED
        if failures:
            run.error = (
                f"{failures} of {len(batches)} model calls did not answer; "
                "the fields they covered were not read."
            )
        run.completed_at = datetime.now(UTC)

        logger.info(
            "extraction_run_completed",
            reference=case.reference,
            schema=dataset.key,
            fields=run.fields_total,
            extracted=run.fields_extracted,
            review=run.fields_needing_review,
            calls=calls,
            failed_calls=failures,
        )
        return RunOutcome(run=run, values=values)

    async def _retrieve(
        self, case_id: uuid.UUID, schema: ExtractionSchema, dataset: DatasetSchema
    ) -> dict[str, RetrievalResult]:
        """One search per field, concurrently, with the query vectors cached."""
        specs = list(dataset.fields)
        queries = [spec.query for spec in specs]
        vectors = await self._query_vectors(schema, specs)

        results = await self._retrieval.search_many(
            case_id, queries, limit=self._config.passages_per_field, vectors=vectors
        )
        return {spec.key: result for spec, result in zip(specs, results, strict=True)}

    async def _query_vectors(
        self, schema: ExtractionSchema, specs: list[FieldSpec]
    ) -> list[list[float] | None]:
        """Each field's query embedding, from the cache or freshly computed."""
        embeddings = self._retrieval.embeddings
        if embeddings is None:
            return [None] * len(specs)

        cache = FieldVectorCache(schema)
        model = embeddings.model
        vectors: list[list[float] | None] = []
        missing: list[int] = []

        for index, spec in enumerate(specs):
            cached = cache.get(spec.key, spec.query, model)
            vectors.append(cached)
            if cached is None:
                missing.append(index)

        if not missing:
            return vectors

        try:
            computed = await embeddings.embed_documents([specs[index].query for index in missing])
        except Exception as exc:
            logger.info("extraction_query_embedding_unavailable", error=type(exc).__name__)
            return vectors

        for index, vector in zip(missing, computed, strict=True):
            vectors[index] = vector
            cache.put(specs[index].key, specs[index].query, model, vector)
        return vectors

    async def _body_passages(self, body_document_ids: set[uuid.UUID]) -> list[RetrievedChunk]:
        """The notification body's passages, whatever retrieval thought of them."""
        limit = self._config.body_chunks_always_included
        if not body_document_ids or limit <= 0:
            return []

        collected: list[RetrievedChunk] = []
        for document_id in sorted(body_document_ids, key=str):
            rows = await self._retrieval.chunks.list_for_document(document_id, limit=limit)
            # Scored at 1.0 so that where the body and a document both answer a
            # field, the body's passage is never the one dropped by the per-batch
            # passage cap. Which of the two the *model* prefers is the model's
            # decision, and the system prompt tells it to prefer the document.
            collected.extend(RetrievedChunk(chunk=row, score=1.0) for row in rows)
        return collected[:limit]

    def _batch(
        self,
        dataset: DatasetSchema,
        retrieved: dict[str, RetrievalResult],
        body: list[RetrievedChunk],
        filenames: dict[uuid.UUID, str],
    ) -> list[_Batch]:
        """Group fields into model calls, and gather the passages each call needs.

        Fields are batched in their configured order, which groups a panel's
        fields together — and a panel's fields are the ones a document tends to
        state in one place, so their retrieved passages overlap and the union
        stays small. Grouping by score or by similarity was considered and
        rejected: it makes the batch composition depend on the documents, so two
        runs of the same dataset produce differently-shaped prompts and become
        much harder to reason about when one of them is wrong.
        """
        size = max(1, self._config.fields_per_call)
        specs = list(dataset.fields)
        batches: list[_Batch] = []

        for start in range(0, len(specs), size):
            window = specs[start : start + size]
            hits: dict[str, RetrievedChunk] = {}

            for chunk in body:
                hits.setdefault(chunk.chunk_ref, chunk)
            for spec in window:
                for hit in retrieved.get(spec.key, RetrievalResult()).hits:
                    existing = hits.get(hit.chunk_ref)
                    if existing is None or hit.score > existing.score:
                        hits[hit.chunk_ref] = hit

            ordered = sorted(hits.values(), key=lambda hit: hit.score, reverse=True)
            ordered = ordered[: self._config.max_passages_per_call]

            passages: list[Passage] = []
            budget = self._config.max_passage_characters
            for position, hit in enumerate(ordered, start=1):
                if budget - len(hit.chunk.content) <= 0:
                    break
                budget -= len(hit.chunk.content)
                passages.append(
                    Passage(
                        label=f"C{position}",
                        chunk=hit.chunk,
                        filename=filenames.get(hit.chunk.fnol_document_id, "attachment"),
                        score=hit.score,
                    )
                )
            batches.append(_Batch(specs=window, passages=passages))

        return batches

    async def _ask(
        self, batches: list[_Batch], *, channel: str
    ) -> tuple[dict[str, FieldAnswer], int, int, int, str | None]:
        """One model call per batch, bounded. `(answers, calls, failures, ms, model)`.

        A batch that fails is counted and skipped. Its fields simply have no
        answer, which is the same state as a field the passages did not state —
        and the run reports `partial` so the difference is visible where it
        matters, on the run rather than on each field.
        """
        provider = self._provider
        if provider is None:  # pragma: no cover — guarded by the caller
            return {}, 0, 0, 0, None

        gate = asyncio.Semaphore(max(1, self._config.call_concurrency))

        async def one(batch: _Batch) -> tuple[list[FieldAnswer], int, str | None] | None:
            if not batch.passages:
                # Nothing was retrieved for any field in this batch. Asking a
                # model to extract from an empty prompt returns invention.
                return [], 0, None
            async with gate:
                try:
                    response = await provider.structured(
                        schema=FieldAnswerSet,
                        system_prompt=SYSTEM_PROMPT,
                        user_prompt=render_user_prompt(
                            channel=channel,
                            fields=render_fields(batch.specs),
                            passages=_render_passages(batch.passages),
                        ),
                        schema_name="dataset_extraction",
                    )
                except AIProviderError as exc:
                    logger.warning(
                        "extraction_batch_failed",
                        fields=[spec.key for spec in batch.specs],
                        error=str(exc),
                    )
                    return None
            return response.data.answers, response.latency_ms, response.model

        results = await asyncio.gather(*(one(batch) for batch in batches))

        answers: dict[str, FieldAnswer] = {}
        calls = failures = latency = 0
        model: str | None = None

        for batch, result in zip(batches, results, strict=True):
            if result is None:
                failures += 1
                continue
            found, elapsed, used = result
            if batch.passages:
                calls += 1
            latency += elapsed
            model = model or used
            wanted = {spec.key for spec in batch.specs}
            for answer in found:
                # A model answering for a field it was not asked about in this
                # batch is answering from the field list of a different call it
                # cannot see. Dropped rather than stored against a batch whose
                # passages it was not shown — the label would resolve wrongly.
                if answer.field_key in wanted and answer.present:
                    answers[answer.field_key] = answer

        return answers, calls, failures, latency, model

    async def _persist(
        self,
        case: FNOLCase,
        *,
        run: ExtractionRun,
        schema: ExtractionSchema,
        dataset: DatasetSchema,
        answers: dict[str, FieldAnswer],
        labels: dict[str, dict[str, Passage]],
    ) -> list[ExtractedValue]:
        """Write one row per field, preserving anything a human has corrected."""
        existing = {row.field_key: row for row in await self._runs.list_values(case.id, schema.id)}
        threshold = dataset.review_threshold
        values: list[ExtractedValue] = []

        for spec in dataset.fields:
            row = existing.get(spec.key)
            if row is None:
                row = self._runs.add_value(
                    ExtractedValue(
                        fnol_case_id=case.id,
                        schema_id=schema.id,
                        field_key=spec.key,
                    )
                )

            # Metadata follows the schema even for a corrected value: renaming a
            # field's label must rename it everywhere, and that is not a change
            # to what anybody typed.
            row.label = spec.label
            row.group_label = spec.group_label
            row.data_type = spec.data_type

            if row.human_modified:
                # An officer's answer outranks the model's, permanently. The
                # second place this rule is enforced — the first is the API that
                # accepted the correction — because losing it is the one bug that
                # must not be possible.
                values.append(row)
                continue

            self._apply_answer(
                row,
                spec=spec,
                answer=answers.get(spec.key),
                passages=labels.get(spec.key, {}),
                run_id=run.id,
                threshold=threshold,
            )
            values.append(row)

        await self._runs.prune_values(
            case.id, schema.id, keep=[spec.key for spec in dataset.fields]
        )
        await self._runs.flush()
        return values

    def _apply_answer(
        self,
        row: ExtractedValue,
        *,
        spec: FieldSpec,
        answer: FieldAnswer | None,
        passages: dict[str, Passage],
        run_id: uuid.UUID,
        threshold: float,
    ) -> None:
        """Write one answer onto its row, or clear the row when there was none."""
        row.run_id = run_id
        row.source = FieldSource.AI
        # Rectangles are resolved from the quote against the document's geometry,
        # so a new quote makes a cached rectangle wrong. Cleared here rather than
        # at read time, where the staleness would be invisible.
        row.rects = None
        row.highlight_note = None

        if answer is None or not answer.present:
            row.value_text = None
            row.value_json = None
            row.confidence = None
            row.needs_review = spec.required
            row.validation_error = None
            row.quote = None
            row.source_document_id = None
            row.source_chunk_id = None
            row.page_number = None
            row.section_label = None
            row.char_start = None
            row.char_end = None
            return

        coerced, error = spec.coerce(answer.value)
        passage = passages.get(answer.passage or "")

        row.value_text = answer.value
        row.value_json = coerced if stores_typed_value(spec.data_type) else None
        row.validation_error = error
        row.quote = answer.quote
        row.confidence = self._confidence(answer.confidence, passage)
        # A value that would not coerce is shown, and flagged. It is real text a
        # document really states, and hiding it because our parser disagreed with
        # its date format helps nobody.
        row.needs_review = bool(error) or (row.confidence or 0.0) < threshold

        if passage is None:
            # The model cited a label it was not shown, or cited nothing. The
            # value stands; the citation does not. Provenance pointing at a
            # passage the model never read is worse than none.
            row.source_document_id = None
            row.source_chunk_id = None
            row.page_number = None
            row.section_label = None
            row.char_start = None
            row.char_end = None
            return

        chunk = passage.chunk
        row.source_document_id = chunk.fnol_document_id
        row.source_chunk_id = chunk.id
        row.page_number = chunk.page_number
        row.section_label = chunk.section_label
        row.char_start = chunk.char_start
        row.char_end = chunk.char_end

    def _confidence(self, reported: float, passage: Passage | None) -> float:
        """Blend what the model claims with how well retrieval scored its source.

        A model is the better judge of whether it read a value correctly; the
        retrieval score is the better judge of whether it was looking in the
        right place. Neither is sufficient: a confident model reading a passage
        that matched nothing is exactly the failure this guards against.
        """
        weight = min(max(self._config.llm_confidence_weight, 0.0), 1.0)
        retrieval = passage.score if passage is not None else 0.0
        return round(weight * reported + (1.0 - weight) * retrieval, 4)

    # -- Helpers -------------------------------------------------------------

    async def _reusable(
        self, case_id: uuid.UUID, schema_id: uuid.UUID, fingerprint: str
    ) -> ExtractionRun | None:
        latest = await self._runs.latest_run(case_id, schema_id)
        if latest is None:
            return None
        if latest.fingerprint != fingerprint:
            return None
        if latest.status not in REUSABLE_RUN_STATUSES:
            return None
        return latest

    def _settle_empty(self, run: ExtractionRun, reason: str) -> RunOutcome:
        run.status = ExtractionRunStatus.SKIPPED
        run.error = reason
        run.completed_at = datetime.now(UTC)
        return RunOutcome(run=run, skipped_reason=reason)


def _render_passages(passages: list[Passage]) -> str:
    """The passages, labelled the way the model is told to cite them.

    The location line is for the model as much as for a reader: told which file
    and page a passage came from, a model asked to quote its evidence produces
    quotes that can actually be found there.
    """
    blocks: list[str] = []
    for passage in passages:
        where = [passage.filename]
        if passage.chunk.page_number is not None:
            where.append(f"page {passage.chunk.page_number}")
        if passage.chunk.section_label:
            where.append(passage.chunk.section_label)
        blocks.append(
            f"=== PASSAGE [{passage.label}] — {', '.join(where)} ===\n{passage.chunk.content}"
        )
    return "\n\n".join(blocks)


def _dominant_strategy(results: Any) -> str:
    """How retrieval mostly answered, for the run record.

    `hybrid-rrf` wins when any search managed it, because the interesting
    question a reader has is "was the vector index working", and one search that
    used it answers yes.
    """
    strategies = {result.strategy for result in results}
    for preferred in ("hybrid-rrf", "semantic", "keyword"):
        if preferred in strategies:
            return preferred
    return next(iter(strategies), "none")


__all__ = ["FieldVectorCache", "Passage", "RunOutcome", "SchemaExtractionEngine"]
