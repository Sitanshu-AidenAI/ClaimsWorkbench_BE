"""Matching an FNOL case against the policy library.

The orchestration around `app/domain/policy_matching.py`, and it does the three
things the pure engine deliberately cannot: read the notice, retrieve, and hydrate
the facts behind what came back.

**Where this sits relative to policy identification.** `PolicyIdentificationService`
matches a notice against the *book* and its answer is authoritative: it writes
`fnol_policy_matches`, and confirming one of its candidates binds a contract. This
service matches the notice against the *wordings* and its answer is **advisory**. It
writes nothing to the case. That is not a limitation being apologised for — it is the
safety property:

> A wording is a document an administrator uploaded. Letting a retrieval score bind a
> policy would mean a PDF whose declarations page read badly could attach a claim to
> the wrong contract, with no book row ever consulted.

So the two are complementary and their union is the point. Identification says which
row; this says which wording answers the loss, and quotes the clauses. Where they
agree, an officer has corroboration from two independent methods. Where they
disagree, that disagreement is the most useful thing on the screen, and neither
silently wins.

**The safety rule, restated.** Candidates come *from* the library. A policy number
that appears nowhere in the library cannot become a match — it can only fail to be
one — because every candidate here is a `policy_documents` row that retrieval
returned.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from app.core.config import PolicyLibrarySettings, settings
from app.core.logging import get_logger
from app.domain import policy_matching as engine
from app.models.policy_document import PolicyDocument
from app.repositories.policy_document import PolicyDocumentRepository
from app.services.policies.retrieval import FacetedRetrieval, PolicyRetrievalService

logger = get_logger(__name__)


class PolicyMatchingService:
    def __init__(
        self,
        documents: PolicyDocumentRepository,
        retrieval: PolicyRetrievalService,
        *,
        config: PolicyLibrarySettings | None = None,
    ) -> None:
        self._documents = documents
        self._retrieval = retrieval
        self._config = config or settings.policy_library

    async def match_case(self, case: Any) -> engine.PolicyMatchResult:
        """Rank the library against one FNOL case."""
        return await self.match(notice_query(case))

    async def match(self, notice: engine.NoticeQuery) -> engine.PolicyMatchResult:
        """Rank the library against a notice, and say why each wording ranked."""
        if not self._config.enabled:
            return engine.PolicyMatchResult(strategy="disabled")

        started = time.monotonic()

        # The line of business is passed as a *filter* rather than scored into the
        # query, because a wording of the wrong line is not a weaker answer — it is
        # not an answer. `line_is_compatible` makes the filter generous on purpose:
        # a wrong score is visible on the card and an officer can overrule it, and a
        # wrong filter removes the policy from the screen and nobody learns it was
        # there. So this narrows only when the notice states a line at all.
        retrieved = await self._retrieval.search_for_notice(notice, line_of_business=None)
        if not retrieved.by_document:
            logger.info(
                "policy_document_match_empty",
                reference=notice.reference,
                strategy=retrieved.strategy,
                degraded=retrieved.degraded,
            )
            return engine.PolicyMatchResult(
                strategy=retrieved.strategy,
                degraded=retrieved.degraded,
                signals_available=notice.identifying_values,
                facets_queried=retrieved.facets_queried,
            )

        candidates = await self._hydrate(notice, retrieved)
        result = engine.match(
            notice,
            candidates,
            config=self._config,
            strategy=retrieved.strategy,
            degraded=retrieved.degraded,
            chunks_considered=retrieved.chunks_considered,
            facets_queried=retrieved.facets_queried,
        )

        logger.info(
            "policy_document_matched",
            reference=notice.reference,
            compared=result.documents_compared,
            matches=len(result.matches),
            rejected=len(result.rejected),
            recommended=(str(result.recommended.document_id) if result.recommended else None),
            ambiguous=result.ambiguous,
            strategy=result.strategy,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return result

    async def score_one(
        self, notice: engine.NoticeQuery, document: PolicyDocument
    ) -> tuple[list[engine.SignalResult], float | None]:
        """Score one wording against a notice, with no retrieval.

        For the officer who found a wording in the library by hand. It goes through
        the same comparators the ranked ones did, so what they are shown about a
        policy they chose is what they would have been shown had the engine ranked it.
        """
        return engine.compare(notice, document_facts(document))

    # -- Internals -----------------------------------------------------------

    async def _hydrate(
        self, notice: engine.NoticeQuery, retrieved: FacetedRetrieval
    ) -> list[engine.RetrievedPolicy]:
        """Turn grouped hits into the engine's inputs.

        One query for every document retrieval returned, whatever backend found them.
        This is the cost of the payload-holds-no-text decision, and it is one indexed
        lookup against microseconds of network round trip to the store itself.
        """
        rows = {row.id: row for row in await self._documents.list_by_ids(retrieved.document_ids)}

        # Normalised against the best document's best passage, so the configured match
        # thresholds mean the same thing whichever backends answered — the same
        # argument the fusion step makes one layer down, applied at document level
        # because that is the level the bands are set on.
        peak = max(
            (hit.score for hits in retrieved.by_document.values() for hit in hits),
            default=0.0,
        )

        candidates: list[engine.RetrievedPolicy] = []
        for document_id, hits in retrieved.by_document.items():
            row = rows.get(document_id)
            if row is None:
                # A passage whose document has been deleted since the search. Skipped
                # rather than surfaced as a match with no wording behind it.
                logger.info("policy_match_hit_without_document", document_id=str(document_id))
                continue

            # The document's own score: its best passage, lifted by how consistently
            # its other passages agreed. `max` alone lets one lucky paragraph carry a
            # wording; a mean alone punishes a long policy for the thirty-eight pages
            # that had nothing to do with this loss. The damped sum of the rest is the
            # compromise, and it is capped so it can only ever corroborate the peak.
            best = hits[0].score
            rest = hits[1:]
            support = sum(hit.score for hit in rest) / len(rest) if rest else 0.0
            raw = min(1.0, best + 0.18 * support)
            relative = raw / peak if peak else 0.0

            candidates.append(
                engine.RetrievedPolicy(
                    facts=document_facts(row),
                    retrieval_score=round(min(1.0, relative), 6),
                    excerpts=tuple(
                        engine.Excerpt(
                            chunk_ref=hit.chunk.chunk_ref,
                            content=_trim(hit.chunk.content),
                            score=hit.score,
                            page_number=hit.chunk.page_number,
                            section_label=hit.chunk.section_label,
                            facet=hit.facet,
                        )
                        for hit in hits
                    ),
                    facets_hit=frozenset(retrieved.facets_by_document.get(document_id, set())),
                    chunks_matched=len(hits),
                )
            )

        del notice  # Read by the engine, not here. Kept in the signature for symmetry.
        return candidates


def document_facts(document: PolicyDocument) -> engine.PolicyDocumentFacts:
    """The engine's projection of one policy document row.

    A projection rather than the row, so the engine stays pure and a test can build
    one from a dictionary. The metadata bag is read defensively — it is JSONB written
    by an earlier version of the reader, and a list that arrived as something else must
    not take a match down.
    """
    metadata = document.extracted_metadata or {}
    return engine.PolicyDocumentFacts(
        document_id=document.id,
        filename=document.filename,
        policy_id=document.policy_id,
        policy_number=document.policy_number,
        insured_name=document.insured_name,
        insurer_name=document.insurer_name,
        broker_name=document.broker_name,
        policy_type=document.policy_type,
        line_of_business=document.line_of_business,
        effective_date=document.effective_date,
        expiry_date=document.expiry_date,
        page_count=document.page_count,
        additional_insureds=_strings(metadata.get("additional_insureds")),
        postcodes=_strings(metadata.get("postcodes")),
        locations=_strings(metadata.get("locations")),
        limits=_strings(metadata.get("limits")),
    )


def notice_query(case: Any) -> engine.NoticeQuery:
    """Build the retrieval and comparison query from an FNOL case.

    Reads the case's **columns**, which is the same seam
    `PolicyIdentificationService.notice_signals` reads and deliberately so: a value
    becomes a matching signal once it has been promoted to a column, and until then it
    is extracted, stored, cited and shown but not matched on. Two matchers reading two
    different sources would be two answers to "what did the notice say".

    The date of loss is narrowed to a day. Cover turns on a day, and comparing a
    timestamp against a policy period would make a loss at 00:30 on the inception date
    depend on the timezone the notice was parsed in.
    """
    loss_day = case.date_of_loss.date() if getattr(case, "date_of_loss", None) else None
    return engine.NoticeQuery(
        policy_number=getattr(case, "policy_number", None),
        insured_name=getattr(case, "insured_name", None),
        insured_organisation=getattr(case, "insured_organisation", None),
        broker_name=getattr(case, "broker_name", None),
        broker_reference=getattr(case, "broker_reference", None),
        project_name=getattr(case, "project_name", None),
        contract_number=getattr(case, "contract_number", None),
        loss_location=getattr(case, "loss_location", None),
        risk_location=getattr(case, "risk_location", None),
        loss_postcode=getattr(case, "loss_postcode", None),
        cause_of_loss=getattr(case, "cause_of_loss", None),
        loss_description=getattr(case, "loss_description", None),
        claim_type=getattr(case, "claim_type", None),
        line_of_business=getattr(case, "line_of_business", None),
        date_of_loss=loss_day,
        reference=getattr(case, "reference", None),
    )


#: Characters of a passage shown as an excerpt. A whole policy chunk is ~1,900
#: characters, which is a wall of text on a candidate card; this is a paragraph, which
#: is what an officer reads before opening the page.
EXCERPT_CHARACTERS = 520


def _trim(content: str) -> str:
    """An excerpt, cut on a word boundary and marked when it was cut.

    The ellipsis matters. An excerpt silently truncated mid-clause reads as though the
    policy stops there, and on an exclusion that is the difference between the clause
    and its exception.
    """
    text = " ".join(content.split())
    if len(text) <= EXCERPT_CHARACTERS:
        return text
    cut = text.rfind(" ", 0, EXCERPT_CHARACTERS)
    return f"{text[: cut if cut > 0 else EXCERPT_CHARACTERS].rstrip()}…"


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def linked_policy_ids(result: engine.PolicyMatchResult) -> list[uuid.UUID]:
    """The book rows behind the matched wordings, for a caller that wants them.

    Used to answer "does the wording match agree with the identification engine",
    which is the comparison worth drawing and the one neither module can make alone.
    """
    return [item.facts.policy_id for item in result.matches if item.facts.policy_id is not None]


__all__ = [
    "EXCERPT_CHARACTERS",
    "PolicyMatchingService",
    "document_facts",
    "linked_policy_ids",
    "notice_query",
]
