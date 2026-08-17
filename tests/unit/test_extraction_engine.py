"""The engine: retrieval per field, batching, citation, and what survives a re-run.

Fakes throughout — no database, no provider, no vector store. The properties
under test are arithmetic and control flow, and every one of them is a rule the
rest of the feature relies on without re-checking:

* a field's own description is what is searched for;
* a batch's prompt carries only passages, labelled, and every field it must answer;
* a label the model invented resolves to nothing, and the value survives anyway;
* one batch failing costs that batch and nothing else;
* an officer's correction is never overwritten;
* the notification body is in every prompt whatever retrieval thought.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import ExtractionSettings
from app.domain.enums import DocumentSource, ExtractionRunStatus, FieldSource
from app.models.extraction import ExtractedValue, ExtractionRun, ExtractionSchema
from app.services.ai.base import AIProviderError, AIResponse
from app.services.extraction.engine import FieldVectorCache, SchemaExtractionEngine
from app.services.extraction.prompts import FieldAnswer, FieldAnswerSet
from app.services.extraction.schema import DatasetSchema, FieldSpec
from app.services.intelligence.retrieval import RetrievalResult, RetrievedChunk

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeChunk:
    """A passage, with the fields the engine and the citation path read."""

    def __init__(
        self,
        *,
        ref: str,
        content: str,
        document_id: uuid.UUID,
        page: int | None = 1,
        start: int = 0,
    ) -> None:
        self.id = uuid.uuid4()
        self.chunk_ref = ref
        self.content = content
        self.fnol_document_id = document_id
        self.page_number = page
        self.section_label = None
        self.char_start = start
        self.char_end = start + len(content)


class FakeDocument:
    def __init__(self, *, filename: str, source: str = DocumentSource.EMAIL_ATTACHMENT) -> None:
        self.id = uuid.uuid4()
        self.filename = filename
        self.source = source
        self.checksum_sha256 = f"sum-{filename}"
        self.index_fingerprint = f"idx-{filename}"


class FakeCase:
    def __init__(self, body: str = "A broker writes.") -> None:
        self.id = uuid.uuid4()
        self.reference = "FNOL-2026-000999"
        self.channel = "broker_email"
        self.source_body = body


class FakeChunkRepository:
    def __init__(self, by_document: dict[uuid.UUID, list[FakeChunk]] | None = None) -> None:
        self._by_document = by_document or {}

    async def list_for_document(
        self, document_id: uuid.UUID, *, offset: int = 0, limit: int | None = None
    ) -> list[FakeChunk]:
        rows = self._by_document.get(document_id, [])[offset:]
        return rows[:limit] if limit is not None else rows


class FakeRetrieval:
    """Answers each query with whatever the test mapped it to."""

    def __init__(
        self,
        results: dict[str, list[FakeChunk]] | None = None,
        *,
        chunks: FakeChunkRepository | None = None,
        embeddings: Any = None,
        strategy: str = "hybrid-rrf",
    ) -> None:
        self._results = results or {}
        self._chunks = chunks or FakeChunkRepository()
        self._embeddings = embeddings
        self._strategy = strategy
        self.queries: list[str] = []
        self.vectors_seen: list[list[float] | None] = []

    @property
    def chunks(self) -> FakeChunkRepository:
        return self._chunks

    @property
    def embeddings(self) -> Any:
        return self._embeddings

    async def search_many(
        self,
        case_id: uuid.UUID,
        queries: list[str],
        *,
        limit: int | None = None,
        vectors: list[list[float] | None] | None = None,
    ) -> list[RetrievalResult]:
        del case_id, limit
        self.queries.extend(queries)
        self.vectors_seen.extend(vectors or [None] * len(queries))
        return [
            RetrievalResult(
                hits=[
                    RetrievedChunk(chunk=chunk, score=1.0 - (index * 0.1))  # type: ignore[arg-type]
                    for index, chunk in enumerate(self._match(query))
                ],
                strategy=self._strategy,
                chunks_available=8,
            )
            for query in queries
        ]

    def _match(self, query: str) -> list[FakeChunk]:
        for needle, chunks in self._results.items():
            if needle.lower() in query.lower():
                return chunks
        return []


class FakeRunRepository:
    def __init__(self) -> None:
        self.runs: list[ExtractionRun] = []
        self.values: list[ExtractedValue] = []
        self.pruned: list[str] = []

    async def flush(self) -> None:
        return None

    def add_run(self, run: ExtractionRun) -> ExtractionRun:
        run.id = uuid.uuid4()
        self.runs.append(run)
        return run

    async def latest_run(self, case_id: uuid.UUID, schema_id: uuid.UUID) -> ExtractionRun | None:
        del case_id, schema_id
        return self.runs[-1] if self.runs else None

    async def list_values(self, case_id: uuid.UUID, schema_id: uuid.UUID) -> list[ExtractedValue]:
        del case_id, schema_id
        return list(self.values)

    def add_value(self, value: ExtractedValue) -> ExtractedValue:
        value.id = uuid.uuid4()
        self.values.append(value)
        return value

    async def prune_values(
        self, case_id: uuid.UUID, schema_id: uuid.UUID, *, keep: list[str]
    ) -> int:
        del case_id, schema_id
        removed = [value for value in self.values if value.field_key not in keep]
        self.values = [value for value in self.values if value.field_key in keep]
        self.pruned.extend(value.field_key for value in removed)
        return len(removed)


class BatchProvider:
    """Answers each call with the answers the test queued for it."""

    name = "stub"
    model = "stub-model"

    def __init__(self, *batches: list[FieldAnswer] | Exception) -> None:
        self._batches = list(batches)
        self.prompts: list[str] = []
        self.calls = 0

    async def structured(self, **kwargs: Any) -> AIResponse[FieldAnswerSet]:
        self.prompts.append(kwargs["user_prompt"])
        answers = self._batches[self.calls] if self.calls < len(self._batches) else []
        self.calls += 1
        if isinstance(answers, Exception):
            raise answers
        return AIResponse(
            data=FieldAnswerSet(answers=answers),
            provider=self.name,
            model=self.model,
            latency_ms=7,
        )

    async def aclose(self) -> None:
        return None


class FakeEmbeddings:
    model = "fake-embed"
    dimension = 3

    def __init__(self) -> None:
        self.documents_embedded: list[str] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents_embedded.extend(texts)
        return [[float(len(text)), 1.0, 0.0] for text in texts]

    async def embed_query(self, text: str) -> list[float]:  # pragma: no cover — unused here
        return [float(len(text)), 1.0, 0.0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_dataset(*specs: FieldSpec, threshold: float = 0.6) -> DatasetSchema:
    return DatasetSchema(
        key="fnol_notice", name="FNOL notice", version=1, review_threshold=threshold, fields=specs
    )


def make_schema() -> ExtractionSchema:
    schema = ExtractionSchema(key="fnol_notice", name="FNOL notice", version=1)
    schema.id = uuid.uuid4()
    schema.fields = []
    return schema


POLICY = FieldSpec(
    key="policy.policy_number",
    label="Policy number",
    description="The policy or certificate number the risk is written under.",
    data_type="string",
    group_label="Policy",
)
LOSS_DATE = FieldSpec(
    key="loss.date_of_loss",
    label="Date of loss",
    description="The date the loss or incident happened.",
    data_type="datetime",
    group_label="Loss",
)
AMOUNT = FieldSpec(
    key="financial.estimated_loss",
    label="Estimated loss",
    description="The estimated value of the loss being claimed.",
    data_type="money",
    group_label="Financial",
    required=True,
)


def build(
    *,
    provider: Any,
    retrieval: Any,
    runs: FakeRunRepository | None = None,
    config: ExtractionSettings | None = None,
) -> tuple[SchemaExtractionEngine, FakeRunRepository]:
    repository = runs or FakeRunRepository()
    engine = SchemaExtractionEngine(
        repository,  # type: ignore[arg-type]
        retrieval=retrieval,
        provider=provider,
        config=config or ExtractionSettings(fields_per_call=10),
    )
    return engine, repository


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetrieval:
    async def test_each_field_is_searched_for_with_its_own_description(self) -> None:
        """The accuracy mechanism, asserted directly.

        A shared section query finds the paragraph a whole panel is stated in; a
        field's own description finds the line *that field* is stated on. If this
        ever regresses to one query per group, recall on an oddly-worded field
        goes with it and nothing else in the suite would notice.
        """
        document = FakeDocument(filename="slip.pdf")
        retrieval = FakeRetrieval(
            {"policy": [FakeChunk(ref="c1", content="Policy: X", document_id=document.id)]}
        )
        engine, _ = build(provider=BatchProvider([]), retrieval=retrieval)

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE),
            documents=[document],  # type: ignore[list-item]
        )

        assert len(retrieval.queries) == 2
        assert any("certificate number" in query for query in retrieval.queries)
        assert any(
            "the date the loss or incident happened" in query.lower() for query in retrieval.queries
        )

    async def test_a_query_vector_is_computed_once_and_cached_on_the_field(self) -> None:
        """A field's query is fixed until somebody edits it.

        Without the cache, a forty-field dataset costs forty embedding round
        trips on every run of every notice — which is the difference between a
        feature that scales and one that gets a deployment rate-limited.
        """
        from app.models.extraction import ExtractionSchemaField

        schema = make_schema()
        row = ExtractionSchemaField(
            key=POLICY.key, label=POLICY.label, description=POLICY.description
        )
        schema.fields = [row]

        embeddings = FakeEmbeddings()
        retrieval = FakeRetrieval(embeddings=embeddings)
        engine, _ = build(provider=BatchProvider([]), retrieval=retrieval)

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=schema,
            dataset=make_dataset(POLICY),
            documents=[],
        )

        assert len(embeddings.documents_embedded) == 1
        assert row.query_embedding is not None
        assert row.query_embedding_model == "fake-embed"

        # A second run reads the cache rather than the provider.
        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=schema,
            dataset=make_dataset(POLICY),
            documents=[],
            force=True,
        )
        assert len(embeddings.documents_embedded) == 1

    def test_the_cache_is_invalidated_by_an_edited_description(self) -> None:
        from app.models.extraction import ExtractionSchemaField

        schema = make_schema()
        row = ExtractionSchemaField(key="a", label="A", description="original")
        schema.fields = [row]
        cache = FieldVectorCache(schema)

        cache.put("a", "original", "fake-embed", [1.0])
        assert cache.get("a", "original", "fake-embed") == [1.0]
        assert cache.get("a", "rewritten", "fake-embed") is None
        assert cache.get("a", "original", "a-different-model") is None


class TestPrompting:
    async def test_the_prompt_carries_every_field_and_every_passage_labelled(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="Policy number: CP-2026-4471", document_id=document.id)
        provider = BatchProvider([])
        engine, _ = build(
            provider=provider, retrieval=FakeRetrieval({"policy": [chunk], "loss": [chunk]})
        )

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE),
            documents=[document],  # type: ignore[list-item]
        )

        prompt = provider.prompts[0]
        assert "[policy.policy_number]" in prompt
        assert "[loss.date_of_loss]" in prompt
        assert "=== PASSAGE [C1] — slip.pdf, page 1 ===" in prompt
        assert "Policy number: CP-2026-4471" in prompt

    async def test_a_passage_three_fields_wanted_appears_once(self) -> None:
        """Deduplication is what makes batching cheaper than one call per field."""
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(
            ref="shared", content="Everything is on this page.", document_id=document.id
        )
        provider = BatchProvider([])
        engine, _ = build(
            provider=provider,
            retrieval=FakeRetrieval({"policy": [chunk], "loss": [chunk], "estimated": [chunk]}),
        )

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE, AMOUNT),
            documents=[document],  # type: ignore[list-item]
        )

        assert provider.prompts[0].count("Everything is on this page.") == 1

    async def test_the_notification_body_is_in_every_batch_whatever_retrieval_thought(
        self,
    ) -> None:
        """The one failure this mechanism must not be able to cause.

        The body is the notice itself. It is now a document and is retrieved like
        one — but a retrieval miss on it would mean reading a claim notification
        without reading the email that raised it, so it is prepended regardless.
        """
        body = FakeDocument(
            filename="notification-body.txt", source=DocumentSource.NOTIFICATION_BODY
        )
        attachment = FakeDocument(filename="slip.pdf")
        body_chunk = FakeChunk(
            ref="body-1", content="We notify a claim for water damage.", document_id=body.id
        )
        slip_chunk = FakeChunk(ref="slip-1", content="Policy: CP-1", document_id=attachment.id)

        provider = BatchProvider([], [])
        engine, _ = build(
            provider=provider,
            retrieval=FakeRetrieval(
                # Retrieval finds only the slip, never the body.
                {"policy": [slip_chunk]},
                chunks=FakeChunkRepository({body.id: [body_chunk]}),
            ),
            config=ExtractionSettings(fields_per_call=1),
        )

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE),
            documents=[body, attachment],  # type: ignore[list-item]
        )

        assert len(provider.prompts) == 2
        for prompt in provider.prompts:
            assert "We notify a claim for water damage." in prompt

    async def test_fields_are_split_into_batches_of_the_configured_size(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="text", document_id=document.id)
        provider = BatchProvider([], [], [])
        engine, _ = build(
            provider=provider,
            retrieval=FakeRetrieval({"": [chunk]}),
            config=ExtractionSettings(fields_per_call=1),
        )

        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE, AMOUNT),
            documents=[document],  # type: ignore[list-item]
        )
        assert provider.calls == 3

    async def test_a_batch_with_no_passages_is_not_asked(self) -> None:
        """An empty prompt returns invention, and invention is what we refuse."""
        provider = BatchProvider([])
        engine, _ = build(provider=provider, retrieval=FakeRetrieval({}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[],
        )
        assert provider.calls == 0
        assert outcome.run.llm_calls == 0


class TestValuesAndCitations:
    async def test_a_cited_answer_becomes_a_value_pointing_at_its_passage(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(
            ref="c1",
            content="Policy number: CP-2026-4471",
            document_id=document.id,
            page=4,
            start=120,
        )
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="policy.policy_number",
                    value="CP-2026-4471",
                    confidence=1.0,
                    quote="Policy number: CP-2026-4471",
                    passage="C1",
                )
            ]
        )
        engine, _ = build(provider=provider, retrieval=FakeRetrieval({"policy": [chunk]}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )

        value = outcome.values[0]
        assert value.value_text == "CP-2026-4471"
        assert value.source_chunk_id == chunk.id
        assert value.source_document_id == document.id
        assert value.page_number == 4
        assert value.char_start == 120
        assert value.quote == "Policy number: CP-2026-4471"
        assert outcome.run.status == ExtractionRunStatus.COMPLETED
        assert outcome.run.fields_extracted == 1

    async def test_a_label_the_model_invented_costs_the_citation_and_not_the_value(self) -> None:
        """Provenance pointing at a passage nobody was shown is worse than none.

        The value is kept because a model can read a value correctly and mislabel
        where it came from; the citation is dropped because a reviewer clicking
        "show me where" must never be taken to the wrong paragraph.
        """
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="Policy number: CP-2026-4471", document_id=document.id)
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="policy.policy_number",
                    value="CP-2026-4471",
                    confidence=0.9,
                    quote="CP-2026-4471",
                    passage="C99",
                )
            ]
        )
        engine, _ = build(provider=provider, retrieval=FakeRetrieval({"policy": [chunk]}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )

        value = outcome.values[0]
        assert value.value_text == "CP-2026-4471"
        assert value.source_chunk_id is None
        assert value.page_number is None

    async def test_an_answer_for_a_field_this_batch_was_not_asked_about_is_dropped(self) -> None:
        """Its labels belong to a different call's passages and would resolve wrongly."""
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="text", document_id=document.id)
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="something.invented",
                    value="nonsense",
                    confidence=1.0,
                    quote=None,
                    passage="C1",
                )
            ]
        )
        engine, _ = build(provider=provider, retrieval=FakeRetrieval({"policy": [chunk]}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )
        assert [value.field_key for value in outcome.values] == ["policy.policy_number"]
        assert outcome.values[0].value_text is None

    async def test_a_value_that_will_not_coerce_is_stored_and_flagged(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="Date of loss: sometime", document_id=document.id)
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="loss.date_of_loss",
                    value="sometime in the spring",
                    confidence=0.9,
                    quote="sometime",
                    passage="C1",
                )
            ]
        )
        engine, _ = build(provider=provider, retrieval=FakeRetrieval({"date": [chunk]}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(LOSS_DATE),
            documents=[document],  # type: ignore[list-item]
        )
        value = outcome.values[0]
        assert value.value_text == "sometime in the spring"
        assert value.value_json is None
        assert value.validation_error is not None
        assert value.needs_review is True

    async def test_confidence_blends_the_model_with_the_retrieval_score(self) -> None:
        """Neither number alone is enough.

        A confident model reading a passage that matched nothing is precisely the
        failure this guards against, and the model's own score cannot see it.
        """
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="text", document_id=document.id)
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="policy.policy_number",
                    value="CP-1",
                    confidence=1.0,
                    quote=None,
                    passage="C1",
                )
            ]
        )
        engine, _ = build(
            provider=provider,
            retrieval=FakeRetrieval({"policy": [chunk]}),
            config=ExtractionSettings(llm_confidence_weight=0.7),
        )

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )
        # 0.7 * 1.0 + 0.3 * 1.0 (the top hit's score)
        assert outcome.values[0].confidence == pytest.approx(1.0)

    async def test_a_required_field_with_no_answer_is_flagged_for_review(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="text", document_id=document.id)
        engine, _ = build(provider=BatchProvider([]), retrieval=FakeRetrieval({"": [chunk]}))

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(AMOUNT),
            documents=[document],  # type: ignore[list-item]
        )
        assert outcome.values[0].value_text is None
        assert outcome.values[0].needs_review is True


class TestFailureAndReuse:
    async def test_one_failed_batch_costs_that_batch_and_nothing_else(self) -> None:
        """Losing twenty good values to one unlucky call is how a screen loses trust."""
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="text", document_id=document.id)
        provider = BatchProvider(
            AIProviderError("the provider timed out", retryable=True),
            [
                FieldAnswer(
                    field_key="loss.date_of_loss",
                    value="08 March 2026",
                    confidence=1.0,
                    quote="08 March 2026",
                    passage="C1",
                )
            ],
        )
        engine, _ = build(
            provider=provider,
            retrieval=FakeRetrieval({"": [chunk]}),
            config=ExtractionSettings(fields_per_call=1, call_concurrency=1),
        )

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY, LOSS_DATE),
            documents=[document],  # type: ignore[list-item]
        )

        assert outcome.run.status == ExtractionRunStatus.PARTIAL
        assert outcome.run.fields_failed == 1
        assert outcome.run.error is not None
        by_key = {value.field_key: value for value in outcome.values}
        assert by_key["loss.date_of_loss"].value_text == "08 March 2026"
        assert by_key["policy.policy_number"].value_text is None

    async def test_a_run_with_no_provider_is_skipped_not_failed(self) -> None:
        """The deterministic reader answers the FNOL schema and only that one.

        It cannot stand in for an arbitrary dataset, and saying so is better than
        returning nothing and letting a reviewer guess which happened.
        """
        engine, _ = build(provider=None, retrieval=FakeRetrieval({}))
        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[],
        )
        assert outcome.run.status == ExtractionRunStatus.SKIPPED
        assert outcome.skipped_reason is not None
        assert "No model provider" in outcome.skipped_reason

    async def test_an_unchanged_notice_is_not_read_twice(self) -> None:
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="Policy: CP-1", document_id=document.id)
        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="policy.policy_number",
                    value="CP-1",
                    confidence=1.0,
                    quote="CP-1",
                    passage="C1",
                )
            ],
            [],
        )
        runs = FakeRunRepository()
        engine, _ = build(
            provider=provider, retrieval=FakeRetrieval({"policy": [chunk]}), runs=runs
        )
        case = FakeCase()

        first = await engine.run(
            case,  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )
        assert provider.calls == 1
        assert first.reused is False

        second = await engine.run(
            case,  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )
        assert provider.calls == 1
        assert second.reused is True
        assert len(runs.runs) == 1

    async def test_editing_a_field_description_forces_a_re_read(self) -> None:
        """The questions are in the fingerprint, not only the version number.

        An administrator who rewrites a description has changed what the model is
        asked. A run that kept its old answer would be showing a value extracted
        against a question nobody asks any more.
        """
        document = FakeDocument(filename="slip.pdf")
        case = FakeCase()
        original = make_dataset(POLICY)
        edited = make_dataset(
            FieldSpec(
                key=POLICY.key,
                label=POLICY.label,
                description="A completely different question.",
                group_label=POLICY.group_label,
            )
        )

        first = SchemaExtractionEngine.fingerprint(case, original, [document])  # type: ignore[arg-type]
        second = SchemaExtractionEngine.fingerprint(case, edited, [document])  # type: ignore[arg-type]
        assert first != second

    async def test_a_new_attachment_forces_a_re_read(self) -> None:
        case = FakeCase()
        dataset = make_dataset(POLICY)
        one = FakeDocument(filename="slip.pdf")
        two = FakeDocument(filename="survey.pdf")

        before = SchemaExtractionEngine.fingerprint(case, dataset, [one])  # type: ignore[arg-type]
        after = SchemaExtractionEngine.fingerprint(case, dataset, [one, two])  # type: ignore[arg-type]
        assert before != after

    async def test_the_document_order_does_not_move_the_fingerprint(self) -> None:
        case = FakeCase()
        dataset = make_dataset(POLICY)
        one = FakeDocument(filename="slip.pdf")
        two = FakeDocument(filename="survey.pdf")

        assert SchemaExtractionEngine.fingerprint(  # type: ignore[arg-type]
            case, dataset, [one, two]
        ) == SchemaExtractionEngine.fingerprint(case, dataset, [two, one])  # type: ignore[arg-type]


class TestHumanCorrections:
    async def test_a_corrected_value_is_never_overwritten_by_a_later_run(self) -> None:
        """The single most damaging bug this feature could have.

        Enforced in three places — here, in the correction endpoint, and in the
        write-back adapter — because an officer who finds their correction gone
        after a re-read stops correcting anything.
        """
        document = FakeDocument(filename="slip.pdf")
        chunk = FakeChunk(ref="c1", content="Policy: CP-WRONG", document_id=document.id)

        runs = FakeRunRepository()
        corrected = ExtractedValue(
            fnol_case_id=uuid.uuid4(),
            schema_id=uuid.uuid4(),
            field_key="policy.policy_number",
            label="Policy number",
            value_text="CP-CORRECTED-BY-A-PERSON",
            human_modified=True,
            source=FieldSource.HUMAN,
        )
        corrected.id = uuid.uuid4()
        runs.values.append(corrected)

        provider = BatchProvider(
            [
                FieldAnswer(
                    field_key="policy.policy_number",
                    value="CP-WRONG",
                    confidence=1.0,
                    quote="CP-WRONG",
                    passage="C1",
                )
            ]
        )
        engine, _ = build(
            provider=provider, retrieval=FakeRetrieval({"policy": [chunk]}), runs=runs
        )

        outcome = await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[document],  # type: ignore[list-item]
        )

        assert outcome.values[0].value_text == "CP-CORRECTED-BY-A-PERSON"
        assert outcome.values[0].human_modified is True

    async def test_a_field_removed_from_the_dataset_stops_being_shown(self) -> None:
        runs = FakeRunRepository()
        stale = ExtractedValue(
            fnol_case_id=uuid.uuid4(),
            schema_id=uuid.uuid4(),
            field_key="a.question.nobody.asks",
            label="Gone",
        )
        stale.id = uuid.uuid4()
        runs.values.append(stale)

        engine, _ = build(provider=BatchProvider([]), retrieval=FakeRetrieval({}), runs=runs)
        await engine.run(
            FakeCase(),  # type: ignore[arg-type]
            schema=make_schema(),
            dataset=make_dataset(POLICY),
            documents=[],
        )
        assert "a.question.nobody.asks" in runs.pruned
