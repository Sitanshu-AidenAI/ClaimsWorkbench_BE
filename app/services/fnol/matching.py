"""Matching a notice against records that already exist.

Two services, one shape: fetch a candidate set from a repository, score it with a
pure function from `app.domain`, persist the ranked result, and never decide. Both
can be pointed at an external system later by swapping the repository, because
none of the ranking logic is in SQL.

They are reached differently, though. The catastrophe matcher is a pipeline stage
of its own; the duplicate scan is not, and is run by
`app.services.fnol.identification` as part of identifying the policy. A repeat is
scored against the policy the notice was matched to, so the two questions are one
stage — and running the scan anywhere else would either score against a policy
that had not been settled yet, or score it twice.

The rule they share is the one this module exists to enforce: an AI never
introduces a claim or a catastrophe event that is not already a record in the
database. Candidates come *from* the repository, so a hallucinated reference
cannot become a match — it can only fail to be one.

Policy identification used to live here too and now has a module of its own,
`app.services.fnol.identification`, over an engine of its own,
`app.domain.policy_identification`. It outgrew the shared shape: identifying a
policy is a review stage with its own evidence, its own decision states and its
own explanation structure, and squeezing that into the same twelve-line service as
the catastrophe matcher was costing the thing that matters most about it — that an
officer can see why each candidate was ranked where it was.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import FNOLSettings, settings
from app.core.logging import get_logger
from app.domain import catastrophe as cat_rules
from app.domain import duplicates as duplicate_rules
from app.domain.enums import DuplicateResolution
from app.models.fnol import FNOLDuplicateCandidate
from app.repositories.catastrophe import CatEventRepository
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DuplicateOutcome:
    candidates: list[duplicate_rules.DuplicateAssessment]
    #: The rows those assessments were written to, in the same order. Carried on
    #: the outcome so that everything downstream of the scan reads what the scan
    #: just wrote rather than querying for it again — and because the officer's
    #: `resolution` lives on the row and not on the assessment, which is the one
    #: thing a re-scored candidate does not tell you.
    records: list[FNOLDuplicateCandidate] = field(default_factory=list)
    #: How many recent notices and claims were actually scored to produce those
    #: candidates. Carried out of the scan rather than only logged, because "no
    #: repeat found" and "nothing was there to compare against" are different
    #: answers and an officer cannot tell them apart from an empty list.
    compared: int = 0

    @property
    def strongest(self) -> duplicate_rules.DuplicateAssessment | None:
        return self.candidates[0] if self.candidates else None

    @property
    def unresolved(self) -> list[FNOLDuplicateCandidate]:
        """The candidates still holding the case, which are the ones that act.

        A candidate an officer has decided about — linked, dismissed or accepted
        as a repeat — is part of the record and is never pruned, but it no longer
        raises an exception or drags the completeness score down.
        """
        return [
            record for record in self.records if record.resolution == DuplicateResolution.UNRESOLVED
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "compared": self.compared,
            "candidates": [
                {
                    "reference": candidate.reference,
                    "kind": candidate.kind,
                    "score": round(candidate.score, 4),
                    "reasons": [reason.as_dict() for reason in candidate.reasons],
                }
                for candidate in self.candidates
            ],
        }


class DuplicateDetectionService:
    def __init__(
        self,
        fnol: FNOLRepository,
        claims: ClaimRepository,
        *,
        config: FNOLSettings | None = None,
    ) -> None:
        self._fnol = fnol
        self._claims = claims
        self._config = config or settings.fnol

    async def detect(self, case: Any) -> DuplicateOutcome:
        """Compare against recent notices and claims, and record what scores."""
        assessments: list[tuple[duplicate_rules.DuplicateAssessment, Any, str]] = []

        for other in await self._fnol.duplicate_scan_pool(case):
            assessment = duplicate_rules.compare(
                case, other, kind="fnol", reference=other.reference
            )
            assessments.append((assessment, other, "fnol"))

        for claim in await self._claims.duplicate_scan_pool(
            policy_number=case.policy_number,
            insured_name=case.insured_name,
            policy_id=case.policy_id,
            date_of_loss=case.date_of_loss,
            external_reference=case.external_reference,
        ):
            assessment = duplicate_rules.compare(
                case, claim, kind="claim", reference=claim.reference
            )
            assessments.append((assessment, claim, "claim"))

        scoring = [
            entry
            for entry in assessments
            if duplicate_rules.is_duplicate_candidate(entry[0], config=self._config)
        ]
        scoring.sort(key=lambda entry: entry[0].score, reverse=True)

        records = [
            await self._fnol.upsert_duplicate(
                case.id,
                candidate_kind=kind,
                candidate_reference=assessment.reference,
                score=assessment.score,
                reasons=[reason.as_dict() for reason in assessment.reasons],
                candidate_fnol_id=record.id if kind == "fnol" else None,
                candidate_claim_id=record.id if kind == "claim" else None,
            )
            for assessment, record, kind in scoring
        ]

        await self._fnol.prune_duplicates(
            case.id, [assessment.reference for assessment, _, _ in scoring]
        )

        logger.info(
            "fnol_duplicate_scan",
            reference=case.reference,
            compared=len(assessments),
            candidates=len(scoring),
        )
        return DuplicateOutcome(
            candidates=[assessment for assessment, _, _ in scoring],
            records=records,
            compared=len(assessments),
        )


# ---------------------------------------------------------------------------
# Catastrophe events
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CatOutcome:
    matches: list[cat_rules.CatMatch]

    @property
    def best(self) -> cat_rules.CatMatch | None:
        return self.matches[0] if self.matches else None

    def as_dict(self) -> dict[str, Any]:
        return {"matches": [match.as_dict() for match in self.matches]}


class CatastropheMatchingService:
    def __init__(self, events: CatEventRepository, *, config: FNOLSettings | None = None) -> None:
        self._events = events
        self._config = config or settings.fnol

    async def match(self, case: Any) -> CatOutcome:
        """Rank catastrophe events against the loss.

        The strongest match is written to the case as a *suggestion* —
        `cat_confirmed` stays false until an officer accepts it, and nothing
        downstream treats an unconfirmed attribution as settled except triage,
        which is explicitly allowed to route on a suspicion.
        """
        if case.date_of_loss is None:
            return CatOutcome(matches=[])

        events = await self._events.find_in_window(
            case.date_of_loss.date(),
            tolerance_days=self._config.cat_date_tolerance_days,
            country=None,
        )
        matches = cat_rules.rank_matches(case, list(events), config=self._config)

        if matches and not case.cat_confirmed:
            case.cat_event_id = matches[0].event_id
            case.cat_confidence = matches[0].confidence
        elif not matches and not case.cat_confirmed:
            case.cat_event_id = None
            case.cat_confidence = None

        logger.info("fnol_cat_match", reference=case.reference, matches=len(matches))
        return CatOutcome(matches=matches)
