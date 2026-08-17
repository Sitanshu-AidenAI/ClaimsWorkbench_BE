"""Which passages answer which FNOL field, and how a citation resolves back.

This is the one module in the document-intelligence path that knows what a claim
notification contains. `app/services/intelligence/` deliberately does not: it knows
documents, passages and vectors, so the claim workbench can search a document without
inheriting intake's vocabulary. The translation lives here.

It does three jobs.

**Before extraction** — turn the fields the pipeline wants into retrieval queries,
run them, and assemble the passages into a prompt fragment with a short label on each
that the model is told to cite. The queries are grouped by section rather than written
per field: one search for "date and time of loss" serves `loss.date_of_loss` and
`loss.time_of_loss`, and a model reading one passage answers both more consistently
than two searches and two answers would.

**During extraction** — resolve the label the model echoed back to the passage it was
shown, so `fnol_extracted_fields.source_document_id` and `source_chunk_id` can be
written. A label that resolves to nothing is dropped rather than stored: provenance
pointing at a passage the model was never shown is worse than no provenance, and it is
the same posture the classification service already takes toward a hallucinated line
of business.

**After extraction** — answer "where did this value come from" for one field, which is
what the review screen calls when an officer clicks a value.

Why the query map lives here and not in `app/domain/rules.py`: that module is a
carrier-configurable business surface — which fields a carrier requires changes with
the book it writes. A retrieval query is a prompt-engineering artefact tied to this
implementation, and it belongs beside the module's other field tables (`_FIELD_META`
in `extraction.py`, `_EDITABLE` in `service.py`, `FIELD_READERS` in
`app/domain/assessment.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import DocumentIntelligenceSettings, settings
from app.core.logging import get_logger
from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.services.intelligence.retrieval import RetrievalService, RetrievedChunk

logger = get_logger(__name__)

#: The retrieval queries for each field section, phrased the way a document phrases
#: the thing rather than the way the schema names it — "where the loss occurred"
#: retrieves an address block that "loss_location" does not.
#:
#: Grouped by the section of `_FIELD_META` in `extraction.py`. A module-level check in
#: that file asserts every section here exists, so a new field cannot be added with no
#: query group covering it.
SECTION_QUERIES: dict[str, tuple[str, ...]] = {
    "policy": (
        "policy number certificate number policy reference",
        "name of the insured policyholder",
        "policy period effective date expiry date",
    ),
    "loss": (
        "date and time of loss date of incident",
        "address where the loss occurred risk location site",
        "cause of loss peril how the damage happened",
        "description of the incident narrative of loss",
        "injuries casualties persons hurt fatalities",
        "property vehicle vessel plant machinery affected damaged",
    ),
    "financial": (
        "estimated loss amount value of the claim",
        "repair estimate quotation cost to reinstate",
    ),
    "notification": (
        "reported by contact name broker handler",
        "contact email address telephone number",
    ),
    "additional": (
        "police reference crime number incident number",
        "authorities attending fire brigade local authority",
    ),
    "parties": ("claimant third party witness name and contact details",),
    "documents": ("enclosed attached supporting documents schedule report invoice photographs",),
}


@dataclass(frozen=True, slots=True)
class Citation:
    """One passage as it was shown to the model."""

    label: str
    chunk_id: uuid.UUID
    chunk_ref: str
    document_id: uuid.UUID
    filename: str
    page_number: int | None
    section_label: str | None
    content: str
    char_start: int
    char_end: int
    score: float


@dataclass(slots=True)
class EvidenceBundle:
    """The passages retrieved for one extraction, and how to cite them.

    `prompt` is the rendered fragment; `by_label` is how a label in the model's answer
    becomes a database row. Both are built here, together, because a label that appears
    in one and not the other is a citation that silently fails to resolve.
    """

    prompt: str = ""
    by_label: dict[str, Citation] = field(default_factory=dict)
    strategy: str = "none"
    degraded: bool = False
    chunks_available: int = 0
    #: What was searched and what came back, for the analysis row on the case. This is
    #: the record that answers "why did the model read that" after the fact.
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def used(self) -> bool:
        return bool(self.by_label)

    def resolve(self, label: str | None) -> Citation | None:
        """The passage a label names, or `None` if the model invented the label."""
        if not label:
            return None
        return self.by_label.get(label.strip().upper())


class FNOLEvidenceService:
    """Retrieval in FNOL's vocabulary."""

    def __init__(
        self,
        retrieval: RetrievalService,
        *,
        config: DocumentIntelligenceSettings | None = None,
    ) -> None:
        self._retrieval = retrieval
        self._config = config or settings.docint

    async def gather(self, case_id: uuid.UUID, documents: list[FNOLDocument]) -> EvidenceBundle:
        """Retrieve the passages an extraction should read.

        Returns an empty bundle — meaning "send the whole corpus, as before" — when
        retrieval is off, when the case has too few passages for selecting among them
        to be worth the risk, or when nothing scored above the floor. Retrieval
        narrows the prompt; it never gates it.
        """
        if not self._config.enabled or not self._config.retrieval_enabled:
            return EvidenceBundle()

        sections = list(SECTION_QUERIES)
        queries = [query for section in sections for query in SECTION_QUERIES[section]]
        owners = [section for section in sections for _ in SECTION_QUERIES[section]]

        results = await self._retrieval.search_many(case_id, queries)
        available = max((result.chunks_available for result in results), default=0)

        if available < self._config.retrieval_min_chunks:
            # Selecting six passages out of eight is overhead with a downside and no
            # upside: the whole corpus already fits, and narrowing it can only lose
            # something.
            logger.info(
                "evidence_corpus_too_small",
                case_id=str(case_id),
                chunks=available,
                minimum=self._config.retrieval_min_chunks,
            )
            return EvidenceBundle(chunks_available=available, strategy="whole-corpus")

        return self._assemble(results, owners, documents, available)

    def _assemble(
        self,
        results: list[Any],
        owners: list[str],
        documents: list[FNOLDocument],
        available: int,
    ) -> EvidenceBundle:
        """Dedupe, budget and render the retrieved passages."""
        filenames = {document.id: document.filename for document in documents}

        #: Best score per passage, plus which sections asked for it. A passage three
        #: sections wanted appears once in the prompt, not three times.
        best: dict[str, tuple[RetrievedChunk, set[str]]] = {}
        strategies: set[str] = set()
        degraded = False

        for result, section in zip(results, owners, strict=True):
            strategies.add(result.strategy)
            degraded = degraded or result.degraded
            for hit in result.hits:
                existing = best.get(hit.chunk_ref)
                if existing is None:
                    best[hit.chunk_ref] = (hit, {section})
                else:
                    existing[1].add(section)
                    if hit.score > existing[0].score:
                        best[hit.chunk_ref] = (hit, existing[1])

        ordered = sorted(best.values(), key=lambda item: item[0].score, reverse=True)
        ordered = ordered[: self._config.max_evidence_chunks]

        by_label: dict[str, Citation] = {}
        blocks: list[str] = []
        budget = settings.ai.max_input_characters // 2
        sections_trace: dict[str, list[dict[str, Any]]] = {}

        for position, (hit, asked_by) in enumerate(ordered, start=1):
            chunk: FNOLDocumentChunk = hit.chunk
            if budget - len(chunk.content) <= 0:
                break
            budget -= len(chunk.content)

            label = f"C{position}"
            filename = filenames.get(chunk.fnol_document_id, "attachment")
            citation = Citation(
                label=label,
                chunk_id=chunk.id,
                chunk_ref=chunk.chunk_ref,
                document_id=chunk.fnol_document_id,
                filename=filename,
                page_number=chunk.page_number,
                section_label=chunk.section_label,
                content=chunk.content,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                score=hit.score,
            )
            by_label[label] = citation
            blocks.append(_render(citation, asked_by))

            for section in sorted(asked_by):
                sections_trace.setdefault(section, []).append(
                    {
                        "label": label,
                        "chunk_ref": chunk.chunk_ref,
                        "document_id": str(chunk.fnol_document_id),
                        "page_number": chunk.page_number,
                        "score": hit.score,
                        "semantic_score": hit.semantic_score,
                        "keyword_score": hit.keyword_score,
                    }
                )

        if not by_label:
            return EvidenceBundle(
                chunks_available=available, strategy="whole-corpus", degraded=degraded
            )

        strategy = "hybrid-rrf" if "hybrid-rrf" in strategies else next(iter(strategies), "none")
        return EvidenceBundle(
            prompt="\n\n".join(blocks),
            by_label=by_label,
            strategy=strategy,
            degraded=degraded,
            chunks_available=available,
            trace={
                "strategy": strategy,
                "strategy_version": 1,
                "degraded": degraded,
                "chunks_available": available,
                "passages_shown": len(by_label),
                "sections": sections_trace,
            },
        )


def _render(citation: Citation, asked_by: set[str]) -> str:
    """One passage, labelled the way the model is told to cite it.

    The location line is for the model's benefit as much as the reader's: told which
    file and page a passage is from, a model asked to quote its evidence produces
    quotes that can actually be found there.
    """
    where = [citation.filename]
    if citation.page_number is not None:
        where.append(f"page {citation.page_number}")
    if citation.section_label:
        where.append(citation.section_label)

    return (
        f"=== PASSAGE [{citation.label}] — {', '.join(where)} "
        f"(relevant to: {', '.join(sorted(asked_by))}) ===\n"
        f"{citation.content}"
    )


__all__ = ["SECTION_QUERIES", "Citation", "EvidenceBundle", "FNOLEvidenceService"]
