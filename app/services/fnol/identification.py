"""Identifying the policy behind a notice, and recording the officer's decision.

The orchestration around `app.domain.policy_identification`, and it does four
things the pure engine deliberately cannot:

1. **Reads the notice.** Builds a `NoticeSignals` from the case's columns and the
   provenance rows behind them, so every signal arrives carrying the document and
   the passage it was read from. That is what makes a reason on the candidate card
   clickable rather than merely readable.
2. **Retrieves.** Asks the repository for a pool, following every reference the
   notice carries down all three of the columns it might actually be.
3. **Persists.** Ranked candidates become rows an officer can act on; the whole
   answer — including the near misses that fell below the line — becomes an
   analysis, because "why is my policy not in the list" needs an answer.
4. **Records the decision.** Confirmation and referral both. A referral is a
   decision that no policy could be identified, which is a different state from
   never having asked.

The rule the module exists to enforce, and it is the same rule the matching
services have always held: **an AI never introduces a policy.** Candidates come
*from* the repository, so a hallucinated policy number cannot become a policy
match — it can only fail to match one. Confirmation extends that rather than
weakening it: a policy an officer found by searching is scored and persisted as a
candidate *before* it can be bound, so "the bound policy is always one of this
case's candidates" stays true while manual search stops being a dead end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import FNOLSettings, settings
from app.core.logging import get_logger
from app.domain import policy_identification as engine
from app.domain.enums import (
    PolicyConfidence,
    PolicyIdentificationStatus,
    PolicyMatchStrength,
)
from app.domain.matching import normalise
from app.repositories.fnol import FNOLRepository
from app.repositories.policy import PolicyRepository

logger = get_logger(__name__)


#: Which `fnol_cases` column each signal reads, and which dataset field carries
#: its provenance. The one table that knows both, so adding a signal is a line
#: here rather than a search through the service.
#:
#: The keys are the engine's `NoticeSignals` fields; `column` is the case
#: attribute; `field_key` is the extraction dataset field whose citation the
#: review screen opens when an officer clicks the reason.
SIGNAL_SOURCES: tuple[tuple[str, str, str | None], ...] = (
    ("policy_number", "policy_number", "policy.policy_number"),
    ("broker_reference", "broker_reference", "policy.broker_reference"),
    ("insured_name", "insured_name", "policy.insured_name"),
    ("insured_organisation", "insured_organisation", "policy.insured_organisation"),
    ("broker_name", "broker_name", "policy.broker_name"),
    ("loss_location", "loss_location", "loss.loss_location"),
    ("risk_location", "risk_location", "loss.risk_location"),
    ("loss_postcode", "loss_postcode", "loss.loss_postcode"),
    ("project_name", "project_name", "project.project_name"),
    ("contract_number", "contract_number", "project.contract_number"),
    ("policy_period_stated", "policy_period_stated", "policy.policy_period_stated"),
    ("cause_of_loss", "cause_of_loss", "loss.cause_of_loss"),
)


@dataclass(slots=True)
class Citation:
    """Where one extracted value was read from. Enough to name it, not to draw it.

    Rectangles are deliberately absent: resolving them means downloading the file
    and measuring a page's word geometry, and doing that for a dozen signals on
    page load would be a dozen parses to answer a question nobody has asked yet.
    The review screen asks the evidence endpoint for the geometry of the one signal
    an officer actually clicks.
    """

    document_id: uuid.UUID | None = None
    page_number: int | None = None
    quote: str | None = None
    confidence: float | None = None


class PolicyIdentificationService:
    def __init__(
        self,
        policies: PolicyRepository,
        fnol: FNOLRepository,
        *,
        config: FNOLSettings | None = None,
    ) -> None:
        self._policies = policies
        self._fnol = fnol
        self._config = config or settings.fnol

    # -- Identification ------------------------------------------------------

    async def identify(self, case: Any) -> engine.IdentificationResult:
        """Rank the policy book against this notice and persist the working.

        Never binds a policy a person has not seen, with one exception that is not
        really one: an unambiguous match is written to `case.policy_id` so that
        coverage can say "review required" rather than "no policy located" about a
        notice which plainly has one. `policy_confirmed` stays false, and that —
        not `policy_id` — is what everything downstream treats as authoritative.
        """
        notice = await self.notice_signals(case)
        pool = await self._policies.find_identification_candidates(
            policy_number=_value(notice.policy_number),
            broker_reference=_value(notice.broker_reference),
            contract_number=_value(notice.contract_number),
            insured_name=_value(notice.insured_name),
            organisation=_value(notice.insured_organisation),
            broker_name=_value(notice.broker_name),
            broker_domain=_value(notice.broker_domain),
            insured_domain=_value(notice.insured_domain),
            project_name=_value(notice.project_name),
            postcode=_value(notice.loss_postcode),
            location=_value(notice.risk_location) or _value(notice.loss_location),
            country=case.loss_country,
            line_of_business=case.line_of_business,
            loss_date=notice.loss_day,
            limit=self._config.policy_identification_pool_limit,
        )

        facts = await self._facts_for(pool)
        result = engine.identify(
            notice,
            facts,
            config=self._config,
            confirmed_policy_id=case.policy_id if case.policy_confirmed else None,
            referred=case.policy_identification_status
            == PolicyIdentificationStatus.REFERRED,
        )

        await self._fnol.replace_policy_matches(
            case.id, [_candidate_row(candidate) for candidate in result.candidates]
        )

        case.policy_identification_ran_at = datetime.now(UTC)
        if not case.policy_confirmed:
            # A referral is a person's decision and outranks a re-run: a notice an
            # officer referred does not quietly go back to "needs review" because a
            # new attachment moved a score.
            if case.policy_identification_status != PolicyIdentificationStatus.REFERRED:
                case.policy_identification_status = result.status.value
            if result.recommended_policy_id is not None:
                case.policy_id = result.recommended_policy_id
            elif result.best is not None:
                case.policy_id = result.best.policy_id
            else:
                case.policy_id = None

        logger.info(
            "fnol_policy_identification",
            reference=case.reference,
            compared=result.policies_compared,
            candidates=len(result.candidates),
            near_misses=len(result.near_misses),
            status=result.status.value,
            strength=result.strength.value,
        )
        return result

    async def notice_signals(self, case: Any) -> engine.NoticeSignals:
        """What the notice said, with each value carrying its citation.

        Reads the case's columns rather than the extracted values, which is the
        seam that makes "what is a matching signal" structural: a value is a signal
        once it has been promoted to a column, and until then it is extracted,
        stored, cited and shown but not matched on. The provenance rows are joined
        in only to say *where* each column's value came from.
        """
        citations = await self._citations(case)

        values: dict[str, engine.SignalValue] = {}
        for name, column, field_key in SIGNAL_SOURCES:
            raw = getattr(case, column, None)
            text = str(raw).strip() if raw not in (None, "") else ""
            if not text:
                continue
            citation = citations.get(field_key or "", Citation())
            values[name] = engine.SignalValue(
                value=text,
                field_key=field_key,
                document_id=citation.document_id,
                page_number=citation.page_number,
                quote=citation.quote,
                confidence=citation.confidence,
            )

        loss_day = None
        if case.date_of_loss is not None:
            loss_day = case.date_of_loss.date()
            citation = citations.get("loss.date_of_loss", Citation())
            values["date_of_loss"] = engine.SignalValue(
                value=f"{loss_day:%d %b %Y}",
                field_key="loss.date_of_loss",
                document_id=citation.document_id,
                page_number=citation.page_number,
                quote=citation.quote,
                confidence=citation.confidence,
            )

        if case.line_of_business:
            values["line_of_business"] = engine.SignalValue(
                value=case.line_of_business, field_key=None
            )

        # Two domains, from two different parties, and neither is the other's
        # fallback. The sender is the broker on most commercial notices, so the
        # envelope's domain is broker evidence; the insured's domain has to come
        # from a party recorded as the insured, or it does not exist.
        sender = _sender_of(case)
        if sender:
            values["broker_domain"] = engine.SignalValue(value=sender, field_key=None)

        insured_email = await self._insured_email(case)
        if insured_email:
            values["insured_domain"] = engine.SignalValue(
                value=insured_email, field_key="parties.people"
            )

        return engine.NoticeSignals(
            **values,
            loss_day=loss_day,
            estimated_loss_minor=case.estimated_loss_minor,
        )

    # -- The officer's decision ----------------------------------------------

    async def confirm(
        self, case: Any, policy_id: uuid.UUID, *, actor: str, reason: str | None = None
    ) -> Any | None:
        """Bind the policy an officer chose, whether or not the engine found it.

        The policy has to be a **record in the database** — that is the rule, and
        it is not negotiable, because accepting an arbitrary id from a client would
        let a mistyped request attach a claim to somebody else's policy. It does
        *not* have to already be a ranked candidate: an officer who found the right
        policy in the book after the engine ranked the wrong ones must be able to
        use it. It is scored and persisted as a candidate first, so the record
        afterwards shows what the engine made of the policy the officer chose,
        which is exactly what an auditor asks.
        """
        policy = await self._policies.get(policy_id)
        if policy is None:
            return None

        match = await self._fnol.get_policy_match(case.id, policy_id)
        if match is None:
            await self._persist_officer_candidate(case, policy)

        now = datetime.now(UTC)
        for candidate in await self._fnol.list_policy_matches(case.id):
            candidate.selected = candidate.policy_id == policy_id
            candidate.selected_by = actor if candidate.selected else None
            candidate.selected_at = now if candidate.selected else None

        case.policy_id = policy_id
        case.policy_confirmed = True
        case.policy_confirmed_by = actor
        case.policy_confirmed_at = now
        case.policy_identification_status = PolicyIdentificationStatus.CONFIRMED
        case.policy_referral_reason = None
        case.policy_referred_by = None

        # The officer confirmed the policy, so the policy is now the authority on
        # the contract's own identity — not the broker's spelling of it. Only the
        # policy-bounded fields are taken: what the notice says about the *loss*
        # stands, because the notice is the authority on that and the policy has
        # nothing to say about it.
        case.policy_number = policy.policy_number
        if not case.insured_name:
            case.insured_name = policy.insured_name
        if not case.insured_organisation and policy.insured_organisation:
            case.insured_organisation = policy.insured_organisation
        if not case.broker_name and policy.broker_name:
            case.broker_name = policy.broker_name
        if not case.policy_type and policy.policy_type:
            case.policy_type = policy.policy_type

        logger.info(
            "fnol_policy_confirmed",
            reference=case.reference,
            policy_number=policy.policy_number,
            actor=actor,
            had_candidate=match is not None,
            reason=reason,
        )
        return policy

    async def refer(self, case: Any, *, actor: str, reason: str) -> None:
        """Record that no policy could be identified.

        A decision, not a gap. Nothing is bound, every candidate is deselected so
        the record does not imply a choice nobody made, and the reason is kept
        because the next person to open this notice needs to know what was already
        tried.
        """
        for candidate in await self._fnol.list_policy_matches(case.id):
            candidate.selected = False
            candidate.selected_by = None
            candidate.selected_at = None

        case.policy_id = None
        case.policy_confirmed = False
        case.policy_confirmed_by = None
        case.policy_confirmed_at = None
        case.policy_identification_status = PolicyIdentificationStatus.REFERRED
        case.policy_referral_reason = reason
        case.policy_referred_by = actor

        logger.info("fnol_policy_referred", reference=case.reference, actor=actor)

    async def reopen(self, case: Any) -> None:
        """Undo a referral, so the notice can be identified again."""
        case.policy_identification_status = PolicyIdentificationStatus.NOT_RUN
        case.policy_referral_reason = None
        case.policy_referred_by = None

    # -- Manual search -------------------------------------------------------

    async def search(self, case: Any, term: str, *, limit: int = 25) -> list[engine.CandidateMatch]:
        """The policy book, searched by hand and scored the same way.

        Scored rather than merely listed, which is the point: an officer comparing
        four policies found by hand needs the same per-signal working the ranked
        candidates carry, or the fallback is a worse tool than the thing it is
        falling back from. Nothing is persisted — searching is looking, and only
        confirming is deciding.
        """
        notice = await self.notice_signals(case)
        found = await self._policies.search(term, limit=limit)
        facts = await self._facts_for(found)
        scored = [engine.compare(notice, fact, config=self._config) for fact in facts]
        scored.sort(key=lambda candidate: candidate.score, reverse=True)
        selected = {
            match.policy_id
            for match in await self._fnol.list_policy_matches(case.id)
            if match.selected
        }
        for index, candidate in enumerate(scored):
            candidate.rank = index
            if candidate.policy_id in selected:
                candidate.recommendation_reason = "Confirmed as this notice's policy."
        return scored

    # -- Internals -----------------------------------------------------------

    async def _persist_officer_candidate(self, case: Any, policy: Any) -> None:
        """Score and store a policy an officer chose, so it becomes a candidate.

        Appended at the end of the ranking rather than inserted into it: the engine
        did not put it forward, and rewriting the ranks to pretend otherwise would
        lose the fact that a person overrode the machine.
        """
        notice = await self.notice_signals(case)
        facts = await self._facts_for([policy])
        candidate = engine.compare(notice, facts[0], config=self._config)
        existing = list(await self._fnol.list_policy_matches(case.id))
        candidate.rank = len(existing)
        candidate.recommendation_reason = (
            "Found in the policy book by an officer rather than ranked by the engine."
        )
        await self._fnol.upsert_policy_match(
            case.id, _candidate_row(candidate, origin="officer_search")
        )

    async def _facts_for(self, policies: Any) -> list[engine.PolicyFacts]:
        """Turn policy rows into the engine's projection, following renewal chains.

        The prior term is fetched here because it is the one fact about a policy
        that lives on another row, and the engine must not do IO. Fetched once per
        distinct prior policy rather than once per candidate.
        """
        rows = list(policies)
        prior_ids: set[uuid.UUID] = {
            row.prior_policy_id for row in rows if getattr(row, "prior_policy_id", None)
        }
        priors: dict[uuid.UUID, Any] = {}
        for prior_id in prior_ids:
            prior = await self._policies.get(prior_id)
            if prior is not None:
                priors[prior_id] = prior
        return [
            _facts(row, priors.get(row.prior_policy_id) if row.prior_policy_id else None)
            for row in rows
        ]

    async def _citations(self, case: Any) -> dict[str, Citation]:
        """`field_key -> where it was read from`, for every provenance row on the case.

        One pass over the rows the write-back adapter maintains. A value with no
        row — one supplied by the channel, or typed by an officer — simply has no
        citation, which the panel reports rather than papering over.
        """
        citations: dict[str, Citation] = {}
        for field in await self._fnol.list_fields(case.id):
            citations[field.field_path] = Citation(
                document_id=field.source_document_id,
                quote=field.evidence_snippet,
                confidence=float(field.confidence) if field.confidence is not None else None,
            )
        return citations

    async def _insured_email(self, case: Any) -> str | None:
        """An email address for the *insured*, or none.

        Never the reporter's. Most commercial notices are sent by a broker, so
        falling back to the sender here would compare the broking house's domain
        against the insured's and score a mismatch as a fact about the client.
        """
        for party in await self._fnol.list_parties(case.id):
            if party.role == "insured" and party.email:
                return str(party.email)
        return None


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def _facts(policy: Any, prior: Any | None) -> engine.PolicyFacts:
    locations = tuple(
        engine.PolicyLocationFacts(
            location_ref=entry.location_ref,
            description=entry.description,
            address=entry.address,
            postcode=entry.postcode,
            is_primary=bool(entry.is_primary),
            sum_insured_minor=entry.sum_insured_minor,
            deductible_minor=entry.deductible_minor,
        )
        for entry in getattr(policy, "locations_scheduled", []) or []
        if entry.address
    )
    return engine.PolicyFacts(
        policy_id=policy.id,
        policy_number=policy.policy_number,
        insured_name=policy.insured_name,
        line_of_business=policy.line_of_business,
        status=policy.status,
        effective_date=policy.effective_date,
        expiry_date=policy.expiry_date,
        insured_organisation=policy.insured_organisation,
        insured_email=policy.insured_email,
        insured_domain=policy.insured_domain or _domain(policy.insured_email),
        broker_name=policy.broker_name,
        broker_reference=policy.broker_reference,
        broker_domain=policy.broker_domain,
        insurer_name=policy.insurer_name,
        policy_type=policy.policy_type,
        primary_location=policy.primary_location,
        site_address=policy.site_address,
        locations=locations,
        project_name=policy.project_name,
        project_reference=policy.project_reference,
        contract_number=policy.contract_number,
        principal_name=policy.principal_name,
        contractor_name=policy.contractor_name,
        practical_completion_date=policy.practical_completion_date,
        maintenance_period_months=policy.maintenance_period_months,
        prior_term=(
            (prior.effective_date, prior.expiry_date) if prior is not None else None
        ),
        prior_policy_number=prior.policy_number if prior is not None else None,
        currency=policy.currency,
        limit_amount_minor=policy.limit_amount_minor,
        deductible_amount_minor=policy.deductible_amount_minor,
        perils_covered=tuple(policy.perils_covered or ()),
        exclusions=tuple(policy.exclusions or ()),
    )


def _candidate_row(candidate: engine.CandidateMatch, *, origin: str = "engine") -> dict[str, Any]:
    """One candidate as the repository writes it.

    `matched_on` is kept alongside `signals` rather than replaced by it. The flat
    score-per-signal form is what the queue, the audit trail and the stored
    analysis have always carried, and keeping both means the richer form can be
    added without a consumer having to be found and changed first.
    """
    return {
        "policy_id": candidate.policy_id,
        "match_strength": engine.STRENGTH_FOR_CONFIDENCE[candidate.confidence].value,
        "confidence": candidate.confidence.value,
        "score": round(candidate.score, 4),
        "rank": candidate.rank,
        "matched_on": {
            result.signal: round(result.score, 3)
            for result in candidate.signal_results
            if result.compared and result.score is not None
        },
        "signals": [result.as_dict() for result in candidate.signal_results],
        "warnings": [warning.as_dict() for warning in candidate.warnings],
        "display": candidate.display.as_dict(),
        "period_outcome": candidate.period_outcome.value,
        "reasoning": _prose(candidate),
        "origin": origin,
        "recommended": bool(candidate.recommendation_reason.startswith("Put forward")),
        "engine_version": engine.ENGINE_VERSION,
    }


def _prose(candidate: engine.CandidateMatch) -> str:
    """The sentence, derived from the list rather than written beside it.

    Kept for the audit log, the AI summary and anything reading the case over an
    API that predates the structured form. Derived so the prose and the structure
    can never disagree — a summary that says "matched on the policy number" beside
    a list that says the policy number mismatched is worse than no summary.
    """
    agreements = [result.label.lower() for result in candidate.agreements]
    failures = [
        result.label.lower()
        for result in candidate.signal_results
        if result.outcome.value == "mismatch"
    ]
    parts: list[str] = []
    if agreements:
        parts.append(f"Matched on {_join(agreements)}")
    if failures:
        parts.append(f"{'but ' if parts else ''}{_join(failures)} did not match")
    if not parts:
        return "Compared but nothing agreed; ranked on partial similarity alone."
    compared = len(candidate.compared_signals)
    total = len(candidate.signal_results)
    return f"{'; '.join(parts)}. {compared} of {total} signals could be compared."


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


def _value(signal: engine.SignalValue | None) -> str | None:
    return signal.value if signal is not None else None


def _domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return normalise(email).rsplit("@", 1)[-1] or None


def _sender_of(case: Any) -> str | None:
    """The envelope's sender domain — free, and always present on an email notice."""
    metadata = case.source_metadata or {}
    sender = metadata.get("sender") or metadata.get("from") or case.reporter_email
    return _domain(sender if isinstance(sender, str) else None)


#: For anything that still speaks in the coarse four-band vocabulary.
STRENGTH_FOR_CONFIDENCE: dict[PolicyConfidence, PolicyMatchStrength] = (
    engine.STRENGTH_FOR_CONFIDENCE
)


__all__ = ["SIGNAL_SOURCES", "Citation", "PolicyIdentificationService"]
