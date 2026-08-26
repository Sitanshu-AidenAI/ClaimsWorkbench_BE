"""Coverage sections, the parties on them, and the excess. CLAWS 4, 5, 6 and 7.

Two halves with different characters, and they are in one service because they
share a subject rather than because they share a shape.

**Materialising**, which runs once, at claim creation. The policy's named perils
become coverage rows, the notice's parties become claim parties, and the policy's
excess becomes a claim deductible. Nothing here decides anything: every section
lands at the standpoint `app.domain.coverage.propose_sections` proposed, which is
`in_question` unless the policy is bound and its checks passed. The point of
proposing at all is that a handler who has to create eight rows by hand creates
none — and a claim with no coverage rows cannot answer CLAWS category 4 at all.

**Working**, which is everything afterwards: taking a position on a section,
adding a party the email never mentioned, linking parties to sections, setting an
excess. Each of those is a person's decision, each is audited with an actor, and
each keeps what the rules had proposed so the override stays visible as one.

Nothing here commits — the route owns the transaction, for the reason
`FNOLContext.commit` gives.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domain import claim_lifecycle
from app.domain import coverage as coverage_rules
from app.domain.enums import (
    ActorType,
    AnalysisKind,
    AuditEventType,
    ClaimPartyRole,
    CoverageStandpoint,
    DeductibleType,
    FieldSource,
)
from app.domain.normalisation import PARTY_ROLES
from app.models.claim import (
    Claim,
    ClaimCoverage,
    ClaimCoverageParty,
    ClaimDeductible,
    ClaimParty,
)
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.policy import PolicyRepository
from app.services.fnol.audit import AuditService

#: The notice's party roles that carry across unchanged. Every value in
#: `PARTY_ROLES` is also a `ClaimPartyRole`, which is asserted here rather than
#: assumed — the two enums are allowed to diverge upward, and a notice role this
#: mapping could not place would otherwise land silently as `other`.
_ROLE_ACROSS: dict[str, ClaimPartyRole] = {
    role: ClaimPartyRole(role) for role in sorted(PARTY_ROLES)
}


class ClaimCoverageService:
    def __init__(
        self,
        claims: ClaimRepository,
        cases: FNOLRepository,
        policies: PolicyRepository,
        audit: AuditService,
    ) -> None:
        self._claims = claims
        self._cases = cases
        self._policies = policies
        self._audit = audit

    # -- Materialising, at claim creation -------------------------------------

    async def materialise(self, claim: Claim, case: Any, *, actor: str) -> None:
        """Give the claim its sections, its parties and its excess.

        **Idempotent.** Checks for existing rows and returns rather than adding a
        second set, because the caller is `ClaimCreationService`, which is itself
        idempotent on a retried create — and because the unique constraint on
        `(claim_id, section_key)` would otherwise turn a harmless retry into an
        integrity error the officer sees as a 500.

        Runs after the claim has been flushed, so `claim.id` exists. Called inside
        the creation transaction, so a claim either has its sections or does not
        exist.
        """
        if await self._claims.list_coverages(claim.id):
            return

        policy = await self._policies.get(claim.policy_id) if claim.policy_id else None
        analysis = await self._cases.get_analysis(case.id, AnalysisKind.COVERAGE) if case else None

        sections = coverage_rules.propose_sections(
            policy,
            coverage_result=getattr(analysis, "result", None),
            policy_confirmed=bool(claim.policy_id),
        )
        for section in sections:
            self._claims.add_coverage(
                ClaimCoverage(
                    claim_id=claim.id,
                    section_key=section.key,
                    label=section.label,
                    standpoint=str(section.standpoint),
                    #: The same value as `standpoint` on the way in. They diverge the
                    #: first time somebody takes a different position, and that
                    #: divergence is the whole record of the override.
                    proposed_standpoint=str(section.standpoint),
                    source_check=section.source_check,
                    limit_minor=section.limit_minor,
                    claimed_minor=None,
                    currency=claim.currency,
                    note=section.note,
                )
            )

        parties = await self._copy_parties(claim, case)
        deductible = self._copy_deductible(claim, policy, actor=actor)

        if sections or parties or deductible is not None:
            self._audit.claim(
                claim,
                event_type=AuditEventType.COVERAGE_SELECTED,
                summary=(
                    f"{len(sections)} coverage section(s) proposed and {parties} "
                    "party record(s) carried over from the notification."
                ),
                actor="Coverage rules",
                # `AI` rather than `SYSTEM`, and the distinction is the one
                # `AuditService.system` exists to make: a regulator asking who
                # decided this section was in question is entitled to "nobody
                # did — a rule proposed it and no handler has been yet".
                actor_type=ActorType.AI,
                after={
                    "sections": [section.key for section in sections],
                    "parties": parties,
                    "deductible_minor": (
                        deductible.amount_minor if deductible is not None else None
                    ),
                },
                context={"policy_number": claim.policy_number},
            )

    async def _copy_parties(self, claim: Claim, case: Any) -> int:
        """Copy the notice's parties onto the claim. Returns how many.

        `source` is carried across rather than defaulted: a party the extraction
        read and a party an officer typed are different records to an auditor, and
        the notice already knows which is which.
        """
        if case is None:
            return 0
        rows = await self._cases.list_parties(case.id)
        for row in rows:
            self._claims.add_party(
                ClaimParty(
                    claim_id=claim.id,
                    fnol_party_id=row.id,
                    role=str(_ROLE_ACROSS.get(str(row.role), ClaimPartyRole.OTHER)),
                    name=row.name,
                    organisation=row.organisation,
                    email=row.email,
                    phone=row.phone,
                    address=row.address,
                    notes=row.notes,
                    is_primary=bool(row.is_primary),
                    source=str(row.source or FieldSource.AI),
                    confidence=row.confidence,
                )
            )
        return len(rows)

    def _copy_deductible(
        self, claim: Claim, policy: Any | None, *, actor: str
    ) -> ClaimDeductible | None:
        """The policy's excess as the claim's, where the policy states one.

        Typed `PER_CLAIM`, which is the commercial default and is **a guess this
        says out loud** in its own comment field. The policy book carries an amount
        and no type, so the alternative is to leave the type null and have CLAWS
        reject the submission — a stated default a handler can correct is better
        than an absent field nobody notices.
        """
        amount = getattr(policy, "deductible_amount_minor", None) if policy else None
        if amount is None:
            return None
        return self._claims.add_deductible(
            ClaimDeductible(
                claim_id=claim.id,
                coverage_id=None,
                deductible_type=str(DeductibleType.PER_CLAIM),
                amount_minor=int(amount),
                currency=getattr(policy, "currency", None) or claim.currency,
                applied_minor=0,
                comment=(
                    "Carried from the policy schedule at claim creation. The book "
                    "records an amount and not a type, so per-claim is assumed — "
                    "correct it if the schedule says otherwise."
                ),
                set_by=actor,
            )
        )

    # -- Working the sections -------------------------------------------------

    async def set_standpoint(
        self,
        claim: Claim,
        *,
        section_key: str,
        standpoint: CoverageStandpoint,
        actor: str,
        note: str | None = None,
        reason: str | None = None,
        claimed_minor: int | None = None,
        sublimit_minor: int | None = None,
    ) -> ClaimCoverage:
        """Take, or change, a position on one section.

        `reason` is required only when the new standpoint differs from what the
        rules proposed. Confirming a proposal is agreement and needs no
        justification; departing from it is a decision, and the one an auditor
        reading a declined section will ask about.

        A decided claim refuses, for the reason a reserve movement on one does: the
        coverage position on an approved claim is what the settlement was paid
        against, and restating it afterwards is a deliberate act with its own
        approval rather than an edit.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is {claim.status.replace('_', ' ')}, so its "
                "coverage position is final."
            )

        row = await self._claims.get_coverage(claim.id, section_key)
        if row is None:
            raise NotFoundError(
                f"{claim.reference} has no coverage section '{section_key}'. "
                "Sections come from the policy's named perils."
            )

        target = str(standpoint)
        departing = row.proposed_standpoint is not None and target != row.proposed_standpoint
        if departing and not (reason or "").strip():
            raise ValidationError(
                f"Give a reason. The rules proposed "
                f"'{row.proposed_standpoint.replace('_', ' ')}' for this section and "
                f"you are recording '{target.replace('_', ' ')}'."
            )

        before = {
            "standpoint": row.standpoint,
            "claimed_minor": row.claimed_minor,
            "sublimit_minor": row.sublimit_minor,
        }

        row.standpoint = target
        if note is not None:
            row.note = note
        if claimed_minor is not None:
            row.claimed_minor = max(0, claimed_minor)
        if sublimit_minor is not None:
            row.sublimit_minor = max(0, sublimit_minor)
        if departing:
            row.overridden = True
            row.overridden_by = actor
            row.override_reason = reason
        row.confirmed_by = actor
        row.confirmed_at = datetime.now(UTC)

        self._audit.claim(
            claim,
            event_type=(
                AuditEventType.COVERAGE_OVERRIDDEN
                if departing
                else AuditEventType.COVERAGE_SELECTED
            ),
            summary=(
                f"{actor} recorded {row.label} as "
                f"{target.replace('_', ' ')}" + (" against the proposal." if departing else ".")
            ),
            actor=actor,
            before=before,
            after={
                "standpoint": row.standpoint,
                "claimed_minor": row.claimed_minor,
                "sublimit_minor": row.sublimit_minor,
            },
            context={"section": row.section_key, "reason": reason}
            if reason
            else {"section": row.section_key},
        )
        return row

    # -- Working the parties --------------------------------------------------

    async def add_party(
        self,
        claim: Claim,
        *,
        role: ClaimPartyRole,
        name: str,
        actor: str,
        organisation: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        notes: str | None = None,
    ) -> ClaimParty:
        """Add somebody the notice never named.

        Stamped `source="human"` unconditionally. Everything the extraction found
        arrived through `_copy_parties` with its own provenance, so anything
        reaching this method was typed by the person named in `actor` — and a party
        added by hand that claimed to be AI-read would be the one lie the audit
        trail cannot tolerate.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is {claim.status.replace('_', ' ')}. "
                "Parties are not added to a decided claim."
            )

        party = self._claims.add_party(
            ClaimParty(
                claim_id=claim.id,
                fnol_party_id=None,
                role=str(role),
                name=name,
                organisation=organisation,
                email=email,
                phone=phone,
                address=address,
                notes=notes,
                is_primary=False,
                source=str(FieldSource.HUMAN),
                confidence=None,
            )
        )
        self._audit.claim(
            claim,
            event_type=AuditEventType.PARTY_ADDED,
            summary=f"{actor} added {name} as {str(role).replace('_', ' ')}.",
            actor=actor,
            after={"name": name, "role": str(role)},
        )
        return party

    async def link_party(
        self,
        claim: Claim,
        *,
        section_key: str,
        party_id: Any,
        actor: str,
        basis: str | None = None,
    ) -> ClaimCoverageParty:
        """Link one party to one section. CLAWS entry category 6.

        Both sides are looked up **scoped to the claim** rather than trusted from
        the path, so a section key or a party id belonging to another claim misses
        rather than links across two files.

        A repeated link is a conflict rather than a silent no-op: the unique
        constraint would refuse it anyway, and a caller who thinks they linked
        something twice has a bug worth being told about.
        """
        row = await self._claims.get_coverage(claim.id, section_key)
        if row is None:
            raise NotFoundError(f"{claim.reference} has no coverage section '{section_key}'.")

        party = await self._claims.get_party(claim.id, party_id)
        if party is None:
            raise NotFoundError(f"{claim.reference} has no party {party_id}.")

        if await self._claims.get_coverage_link(row.id, party.id) is not None:
            raise ConflictError(f"{party.name} is already linked to {row.label}.")

        link = self._claims.add_coverage_link(
            ClaimCoverageParty(coverage_id=row.id, party_id=party.id, basis=basis, linked_by=actor)
        )
        self._audit.claim(
            claim,
            event_type=AuditEventType.PARTY_LINKED_TO_COVERAGE,
            summary=f"{actor} linked {party.name} to {row.label}.",
            actor=actor,
            after={"section": row.section_key, "party": party.name, "basis": basis},
        )
        return link

    async def unlink_party(
        self, claim: Claim, *, section_key: str, party_id: Any, actor: str
    ) -> None:
        row = await self._claims.get_coverage(claim.id, section_key)
        if row is None:
            raise NotFoundError(f"{claim.reference} has no coverage section '{section_key}'.")

        link = await self._claims.get_coverage_link(row.id, party_id)
        if link is None:
            raise NotFoundError("That party is not linked to that section.")

        party = await self._claims.get_party(claim.id, party_id)
        await self._claims.delete_coverage_link(link)
        self._audit.claim(
            claim,
            event_type=AuditEventType.PARTY_LINKED_TO_COVERAGE,
            summary=(f"{actor} removed {party.name if party else 'a party'} from {row.label}."),
            actor=actor,
            before={"section": row.section_key, "party": party.name if party else None},
        )

    # -- Working the excess ---------------------------------------------------

    async def set_deductible(
        self,
        claim: Claim,
        *,
        deductible_type: DeductibleType,
        amount_minor: int,
        actor: str,
        section_key: str | None = None,
        currency: str | None = None,
        maximum_applied_minor: int | None = None,
        comment: str | None = None,
    ) -> ClaimDeductible:
        """Record an excess, at claim level or against one section.

        Replaces rather than accumulates: a claim has one excess per scope, and a
        second row for the same scope would leave the arithmetic to pick one. So the
        existing row for that scope is updated in place and the change is audited
        with what it was before.
        """
        if claim_lifecycle.is_terminal(claim.status):
            raise ConflictError(
                f"Claim {claim.reference} is {claim.status.replace('_', ' ')}, so its "
                "excess is final."
            )

        coverage_id = None
        if section_key is not None:
            row = await self._claims.get_coverage(claim.id, section_key)
            if row is None:
                raise NotFoundError(f"{claim.reference} has no coverage section '{section_key}'.")
            coverage_id = row.id

        existing = next(
            (
                row
                for row in await self._claims.list_deductibles(claim.id)
                if row.coverage_id == coverage_id
            ),
            None,
        )

        before = (
            {"type": existing.deductible_type, "amount_minor": existing.amount_minor}
            if existing is not None
            else None
        )

        if existing is None:
            existing = self._claims.add_deductible(
                ClaimDeductible(
                    claim_id=claim.id,
                    coverage_id=coverage_id,
                    deductible_type=str(deductible_type),
                    amount_minor=max(0, amount_minor),
                    currency=(currency or claim.currency).upper(),
                    maximum_applied_minor=maximum_applied_minor,
                    applied_minor=0,
                    comment=comment,
                    set_by=actor,
                )
            )
        else:
            existing.deductible_type = str(deductible_type)
            existing.amount_minor = max(0, amount_minor)
            existing.currency = (currency or existing.currency).upper()
            existing.maximum_applied_minor = maximum_applied_minor
            existing.comment = comment
            existing.set_by = actor

        self._audit.claim(
            claim,
            event_type=AuditEventType.DEDUCTIBLE_SET,
            summary=(
                f"{actor} set the {str(deductible_type).replace('_', ' ')} excess to "
                f"{existing.amount_minor} {existing.currency}"
                + (f" on {section_key}." if section_key else " on the claim.")
            ),
            actor=actor,
            before=before,
            after={
                "type": existing.deductible_type,
                "amount_minor": existing.amount_minor,
                "maximum_applied_minor": existing.maximum_applied_minor,
            },
            context={"section": section_key} if section_key else {},
        )
        return existing

    # -- Reads the sections endpoint needs ------------------------------------

    async def erosion_for(self, claim: Claim, deductible: ClaimDeductible) -> Any:
        """How much of this excess is left, and what ate the rest.

        Reads other claims on the same policy only when the type actually erodes —
        `app.domain.coverage.erosion` ignores them otherwise, but the query is the
        expensive part and there is no reason to run it for a per-claim excess.
        """
        others: list[int] = []
        if str(deductible.deductible_type) in {DeductibleType.AGGREGATE}:
            others = await self._claims.applied_deductibles_on_other_claims(
                policy_id=claim.policy_id, exclude_claim_id=claim.id
            )
        return coverage_rules.erosion(
            deductible_type=str(deductible.deductible_type),
            total_minor=deductible.amount_minor,
            applied_here_minor=deductible.applied_minor,
            applied_on_other_claims=others,
        )


__all__ = ["ClaimCoverageService"]
