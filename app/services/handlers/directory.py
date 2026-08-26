"""Keeping the handler directory made of real people.

The `handlers` table decides who appears in *Assign a handler*, and it was seeded
with eight fictional colleagues at `@carrier.com`. That was fine while assignment
was a demonstration and became a fault the moment it had a consequence: nobody can
sign in as Amara Bello, so a claim assigned to her is a claim that has left the
queue and arrived nowhere.

**Identity comes from the caller's own token.** When somebody who may own a claim
signs in, their `subject`, name and email are written to the directory. That is the
most trustworthy source of those three facts available — it is what the identity
provider just asserted about them — and, unlike reading the realm's user list, it
needs no administrative privilege on Keycloak. The trade is stated in
`register`: a handler who has never signed in is not yet in the directory.

**What the token supplies and what it must never overwrite.** Keycloak owns who a
person *is*: subject, display name, address. The desk owns what they may *do*:
their team, their skills, the lines and territories they cover, the severity
ceiling, the settlement authority, the capacity. A sign-in updates the first three
and touches none of the rest — otherwise every login would quietly reset a
manager's decision about somebody's authority.

**A new account arrives with no settlement authority at all**, which is deliberate
and is the one default here worth arguing about. A number would be a figure nobody
approved, on the field that decides whether this person can release money; null
means *a manager has not said yet*, and `approval_blocks` already refuses a
settlement above an absent limit. So a new handler can be assigned work
immediately and cannot approve a settlement until somebody decides what they are
worth trusting with — which is the right way round.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.core.security import Principal
from app.domain.enums import CLAIM_WORK_ROLES
from app.models.reference_data import Handler
from app.repositories.handler import HandlerRepository

logger = get_logger(__name__)

#: The team a self-registered handler lands in until somebody moves them.
#:
#: Not an empty string: `team` is displayed on the assignment dialog and indexed
#: for the queue, and a blank there reads as a rendering fault rather than as an
#: unanswered question.
UNPLACED_TEAM = "Unplaced"


class HandlerDirectoryService:
    def __init__(self, handlers: HandlerRepository) -> None:
        self._handlers = handlers

    async def register(self, principal: Principal) -> Handler | None:
        """Make sure the signed-in person is in the directory, if they may own a claim.

        Called on every sign-in and every token renewal, so it has to be cheap and
        idempotent: one lookup by subject, and a write only when something has
        actually changed.

        Returns `None` for a caller who cannot own a claim — an intake officer, a
        business admin. They are not handlers, and putting them in the directory
        would offer a manager somebody who has no business holding a claim.

        Nothing here commits. The caller owns the transaction, the same rule the
        rest of this codebase follows.
        """
        if not any(role in CLAIM_WORK_ROLES for role in principal.roles):
            return None

        #: Keycloak asserts these. A caller with neither a name nor an address is
        #: not somebody a manager could pick out of a list, so there is nothing
        #: useful to record.
        name = (principal.full_name or principal.username or "").strip()
        email = (principal.email or "").strip().lower()
        if not name or not email:
            logger.info(
                "handler_registration_skipped",
                subject=principal.subject,
                reason="the token carries no display name or no email address",
            )
            return None

        existing = await self._handlers.get_by_subject(principal.subject)
        if existing is not None:
            return self._refresh(existing, name=name, email=email)

        #: **Adoption, not insertion.** A seeded row may already carry this person's
        #: address — that is exactly the case of a real colleague whose demo record
        #: predates their account — and `handlers.email` is unique, so inserting
        #: would fail anyway. Claiming the row keeps their team, skills and
        #: authority, which is the whole reason the seed had a row for them.
        adopted = await self._handlers.get_by_email(email)
        if adopted is not None:
            logger.info(
                "handler_row_adopted",
                subject=principal.subject,
                email=email,
                handler_id=str(adopted.id),
            )
            adopted.subject = principal.subject
            return self._refresh(adopted, name=name, email=email)

        handler = self._handlers.add(
            Handler(
                subject=principal.subject,
                full_name=name,
                email=email,
                team=UNPLACED_TEAM,
                job_title=None,
                skills=[],
                lines_of_business=[],
                countries=[],
                #: The lowest ceiling and no authority. See the module docstring on
                #: why this is null rather than a number.
                max_severity="low",
                authority_limit_minor=None,
                open_claims=0,
                active=True,
            )
        )
        logger.info("handler_registered", subject=principal.subject, email=email, name=name)
        return handler

    def _refresh(self, handler: Handler, *, name: str, email: str) -> Handler:
        """Bring the identity fields up to date and leave everything else alone.

        People are renamed and change address. What they are trusted with is not a
        fact the identity provider holds, and a login that reset it would undo a
        manager's decision every morning.
        """
        if handler.full_name != name:
            handler.full_name = name
        if handler.email != email:
            handler.email = email
        #: A handler who signs in is working. Reactivating them here means somebody
        #: who was deactivated and has since returned reappears in the directory
        #: without an administrator having to remember.
        if not handler.active:
            handler.active = True
        return handler


__all__ = ["UNPLACED_TEAM", "HandlerDirectoryService"]
