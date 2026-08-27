"""The vocabulary of the FNOL module.

Every one of these is a string enum stored as text with a database check
constraint, rather than a Postgres enum type. Adding a line of business or a
triage category is a fact about the business that changes yearly; a native enum
would make it a migration that rewrites a type other tables depend on.

Nothing outside this module should spell one of these values as a literal.
"""

from __future__ import annotations

from enum import StrEnum


class FNOLStatus(StrEnum):
    """Where a notification is in its life before it becomes a claim.

    `RECEIVED` through `CLAIM_CREATED` is the happy path. The rest are places a
    notification can rest while a human deals with something: they are statuses
    rather than flags because each one answers "why is this not moving" with a
    single word, which is what an intake queue is filtered by.
    """

    RECEIVED = "received"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    INCOMPLETE = "incomplete"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    POLICY_MATCH_REQUIRED = "policy_match_required"
    AI_PROCESSING_FAILED = "ai_processing_failed"
    REFERRED = "referred"
    READY_FOR_CLAIM = "ready_for_claim"
    CLAIM_CREATED = "claim_created"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


#: Statuses from which no further intake work happens.
TERMINAL_FNOL_STATUSES = frozenset(
    {FNOLStatus.CLAIM_CREATED, FNOLStatus.REJECTED, FNOLStatus.CANCELLED}
)

#: Statuses that mean "a human has to look at this before it can become a claim".
REVIEW_FNOL_STATUSES = frozenset(
    {
        FNOLStatus.NEEDS_REVIEW,
        FNOLStatus.INCOMPLETE,
        FNOLStatus.POSSIBLE_DUPLICATE,
        FNOLStatus.POLICY_MATCH_REQUIRED,
        FNOLStatus.AI_PROCESSING_FAILED,
        FNOLStatus.REFERRED,
    }
)


class FNOLChannel(StrEnum):
    """How the notification arrived. Every case stores exactly one."""

    BROKER_EMAIL = "broker_email"
    INSURED_EMAIL = "insured_email"
    PORTAL = "portal"
    API = "api"
    PHONE = "phone"
    TPA = "tpa"
    MANUAL = "manual"


#: Channels whose payload is an email envelope rather than a structured body.
EMAIL_CHANNELS = frozenset({FNOLChannel.BROKER_EMAIL, FNOLChannel.INSURED_EMAIL})


class ProcessingState(StrEnum):
    """The state of the AI/document pipeline, independent of the case status.

    Separate from `FNOLStatus` on purpose: a case can be `NEEDS_REVIEW` while a
    newly uploaded document is still being processed, and collapsing the two
    would make the officer's queue flicker every time a file lands.
    """

    IDLE = "idle"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class LineOfBusiness(StrEnum):
    PROPERTY = "property"
    MOTOR = "motor"
    MARINE = "marine"
    LIABILITY = "liability"
    CASUALTY = "casualty"
    WORKERS_COMPENSATION = "workers_compensation"
    CYBER = "cyber"
    CONSTRUCTION = "construction"
    ENGINEERING = "engineering"
    SPECIALTY = "specialty"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class RiskLevel(StrEnum):
    """Fraud signal strength. Never a determination — see `FraudIndicatorService`."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CoverageIndicator(StrEnum):
    """A preliminary read of the policy against the loss. Not a coverage decision."""

    LIKELY_COVERED = "likely_covered"
    REVIEW_REQUIRED = "review_required"
    POSSIBLE_EXCLUSION = "possible_exclusion"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    POLICY_NOT_LOCATED = "policy_not_located"


class PolicyMatchStrength(StrEnum):
    """The candidate list as a whole, for the queue and the exception engine.

    Coarser than `PolicyConfidence` on purpose. The board filters on it and the
    status machine reads it, and both want the answer to "does a human have to do
    something", not the five-band judgement the identification screen shows.
    """

    EXACT = "exact"
    HIGH = "high"
    POSSIBLE = "possible"
    NONE = "none"


class PolicyConfidence(StrEnum):
    """How well one policy answers one notice.

    Five bands rather than four, and the two additions are the useful ones.
    `WEAK` is a candidate worth showing below the line — an officer recognises
    the right policy at 0.3 far more often than the arithmetic does. `REJECTED`
    is a candidate that was compared and materially failed, which is a different
    statement from one that was never a candidate at all, and the officer who is
    wondering why their policy is not listed needs to be able to see it.
    """

    EXACT = "exact"
    STRONG = "strong"
    POSSIBLE = "possible"
    WEAK = "weak"
    REJECTED = "rejected"


class PolicyIdentificationStatus(StrEnum):
    """Where the identification of one notice's policy has got to.

    `NO_MATCH` and `NEEDS_REVIEW` are both "a person must act" but they are not
    the same act: the first sends the officer to the policy book, the second sends
    them to the candidate list. `REFERRED` is the recorded answer that no policy
    could be identified, which is a decision and not an absence of one.
    """

    NOT_RUN = "not_run"
    NO_MATCH = "no_match"
    NEEDS_REVIEW = "needs_review"
    CONFIDENT_MATCH = "confident_match"
    CONFIRMED = "confirmed"
    REFERRED = "referred"


class SignalOutcome(StrEnum):
    """What comparing one signal against one policy said.

    `MISSING` and `NOT_COMPARED` are deliberately separate, and the distinction is
    the whole reason this enum exists rather than a float. "The broker was not
    stated on the notice" is a gap in the notice the officer can go and fill.
    "Neither the notice nor the policy names a project" is a signal that does not
    apply to this risk. Collapsing them into one silence tells the officer to
    chase something that was never there.
    """

    MATCH = "match"
    PARTIAL = "partial"
    MISMATCH = "mismatch"
    MISSING = "missing"
    NOT_COMPARED = "not_compared"


class PolicyPeriodOutcome(StrEnum):
    """Where the date of loss falls relative to a policy's term.

    Three outcomes rather than a boolean, because property and construction both
    need the resolution: a defect discovered inside a defects liability period is
    in cover, and a loss falling in last year's term is not a failed match but a
    pointer at the prior policy.
    """

    IN_FORCE = "in_force"
    IN_MAINTENANCE_PERIOD = "in_maintenance_period"
    PRIOR_TERM = "prior_term"
    OUTSIDE_PERIOD = "outside_period"
    UNKNOWN = "unknown"


class DuplicateResolution(StrEnum):
    UNRESOLVED = "unresolved"
    NEW_CLAIM = "new_claim"
    LINKED = "linked"
    DUPLICATE = "duplicate"


class ExceptionCode(StrEnum):
    """The things that make an officer stop and look.

    The officer works the exception list rather than every field, so this list is
    the module's actual user interface. Each code has one cause and one remedy.
    """

    NO_POLICY_MATCH = "no_policy_match"
    MULTIPLE_POLICY_MATCHES = "multiple_policy_matches"
    UNCONFIRMED_POLICY_MATCH = "unconfirmed_policy_match"
    #: The reference agrees and the insured named does not. Its own code because
    #: the remedy is different: not "choose a policy" but "check this notice
    #: against the schedule before binding anything".
    POLICY_IDENTITY_CONFLICT = "policy_identity_conflict"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    MISSING_CRITICAL_INFORMATION = "missing_critical_information"
    HIGH_SEVERITY = "high_severity"
    FRAUD_INDICATOR = "fraud_indicator"
    COVERAGE_ISSUE = "coverage_issue"
    CAT_MATCH = "cat_match"
    LOW_EXTRACTION_CONFIDENCE = "low_extraction_confidence"
    CONFLICTING_DATES = "conflicting_dates"
    FUTURE_LOSS_DATE = "future_loss_date"
    OUTSIDE_POLICY_PERIOD = "outside_policy_period"
    EXCEEDS_POLICY_LIMIT = "exceeds_policy_limit"
    #: An amount on the notice is in a currency no configured rate reaches, so the
    #: severity band, the major-loss flag and the limit check were all withheld
    #: rather than decided on the raw integer. Its own code because the remedy is
    #: neither "correct the figure" nor "choose a policy": it is "price this by
    #: hand, or configure the rate".
    CURRENCY_NOT_COMPARABLE = "currency_not_comparable"
    AI_PROCESSING_FAILED = "ai_processing_failed"
    DOCUMENT_UNREADABLE = "document_unreadable"


class ExceptionSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ExceptionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class TriageCategory(StrEnum):
    """A claim may carry several of these at once."""

    SIMPLE = "simple"
    COMPLEX = "complex"
    MAJOR_LOSS = "major_loss"
    LITIGATION_RISK = "litigation_risk"
    FRAUD_REVIEW = "fraud_review"
    CAT_CLAIM = "cat_claim"
    SPECIALIST_REQUIRED = "specialist_required"


class Priority(StrEnum):
    ROUTINE = "routine"
    STANDARD = "standard"
    HIGH = "high"
    URGENT = "urgent"


class ClaimStatus(StrEnum):
    """Mirrors the lifecycle the claims queue already renders."""

    FNOL = "fnol"
    CLASSIFIED = "classified"
    IN_REVIEW = "in_review"
    ESCALATED = "escalated"
    APPROVED = "approved"
    REJECTED = "rejected"


#: Statuses from which no further casework happens. A decided claim is decided:
#: the trail stays readable and the figures stay reportable, and reopening one is
#: a deliberate act somebody has to build a reason for rather than a transition
#: that falls out of the state machine.
TERMINAL_CLAIM_STATUSES = frozenset({ClaimStatus.APPROVED, ClaimStatus.REJECTED})


class MovementType(StrEnum):
    """What a figure on the claim is *for*.

    Named for CLAWS's movement type, which is the vocabulary this has to be
    reported in, and which splits the money three ways: indemnity is what the
    claim costs, expense is what handling it costs, recoveries are what comes
    back. `LEGAL` is a fourth here and a sub-class of expense there, because a
    commercial desk reads legal spend separately and reporting it as undifferentiated
    expense loses the only number anyone asks about it.

    The sub-movement type CLAWS pairs with this is a free column rather than a
    second enum: the code list is in Attachment 1, which we do not hold, and
    guessing at it would produce values that have to be migrated once it arrives.
    """

    INDEMNITY = "indemnity"
    EXPENSE = "expense"
    LEGAL = "legal"
    RECOVERY = "recovery"


#: Movement types that increase what the claim is expected to cost. `RECOVERY`
#: is absent deliberately: a recovery reduces the net, and summing it with the
#: others is the mistake the split exists to stop.
COST_MOVEMENT_TYPES = frozenset({MovementType.INDEMNITY, MovementType.EXPENSE, MovementType.LEGAL})


class NoteSection(StrEnum):
    """Which part of the claim a note was written against.

    A discriminator rather than five tables: a note is a note, and the desk reads
    them as one chronology on the activity log and as five conversations on the
    tabs. The tab it was written on is a property of the note, not a different
    kind of thing.
    """

    GENERAL = "general"
    INSPECTION = "inspection"
    ASSESSMENT = "assessment"
    FRAUD = "fraud"
    RECOVERY = "recovery"


class ClaimDecisionAction(StrEnum):
    """What a handler can do with a claim from the workbench or the queue.

    Five verbs rather than "set the status to X", because the verb is what gets
    audited and the status is a consequence. Two of them — `SEND_TO_APPROVAL` and
    `REFER_TO_MANAGER` — land on the same status and are still different acts, and
    a trail that recorded only the status could not tell them apart.
    """

    APPROVE_SETTLEMENT = "approve_settlement"
    SEND_TO_APPROVAL = "send_to_approval"
    REFER_TO_MANAGER = "refer_to_manager"
    REQUEST_INFORMATION = "request_information"
    #: A manager sending an escalation back to the handler who raised it.
    #:
    #: Lands on `in_review` like `REQUEST_INFORMATION` and is a different act: the
    #: first asks the broker for something, this one tells a colleague to do more
    #: work. The trail could not tell them apart if they shared a verb.
    RETURN_TO_HANDLER = "return_to_handler"
    DECLINE = "decline"


class CoverageStandpoint(StrEnum):
    """Where one section of the policy stands against this loss.

    Four values, and the ordering is deliberate: it runs from "we will pay under
    this" to "we will not", with the two middle states being the ones a handler
    actually spends their time on. `APPLIES_WITH_LIMIT` is separate from
    `CONFIRMED` because a section that responds only up to a sublimit is a
    different conversation with the insured from one that responds in full, and
    collapsing them is how a claim gets reserved at the wrong figure.

    Distinct from `CoverageIndicator`, which is the *pipeline's* preliminary read
    of whether the policy responds at all. That one is a verdict about the notice;
    this is a position on one section of one contract, taken by a person.
    """

    CONFIRMED = "confirmed"
    APPLIES_WITH_LIMIT = "applies_with_limit"
    IN_QUESTION = "in_question"
    EXCLUDED = "excluded"


#: Standpoints under which the section is expected to pay something. What the
#: claim's exposure is summed over — an excluded section contributes nothing, and
#: one still in question contributes nothing *yet*, which is not the same as zero
#: and is why it is named here rather than inferred from a comparison.
RESPONDING_STANDPOINTS = frozenset(
    {CoverageStandpoint.CONFIRMED, CoverageStandpoint.APPLIES_WITH_LIMIT}
)


class DeductibleType(StrEnum):
    """How the excess bites.

    CLAWS names "deductible type" as a required field on entry category 7 and does
    not publish its code list outside Attachment 1, so this is the commercial set
    the wordings in `policy/` actually use. The distinction that matters most for
    the arithmetic is `AGGREGATE` versus the rest: an aggregate deductible erodes
    across the policy period, so what is left of it depends on every other claim
    on the contract, while a per-claim excess starts whole every time.
    """

    PER_CLAIM = "per_claim"
    PER_OCCURRENCE = "per_occurrence"
    PER_LOCATION = "per_location"
    AGGREGATE = "aggregate"
    #: A threshold rather than a deduction: below it nothing is paid, above it
    #: everything is. Named because treating it as a deduction understates the
    #: settlement by the franchise on every claim that clears it.
    FRANCHISE = "franchise"
    #: The insured carries the first slice through their own captive insurer.
    #: Appears on the RFP's own coverage-note field list as "deductible or captive".
    CAPTIVE = "captive"


#: Deductible types whose remaining amount depends on other claims on the policy.
#: The only ones for which erosion has to be computed across the book rather than
#: read off this claim.
ERODING_DEDUCTIBLE_TYPES = frozenset({DeductibleType.AGGREGATE})


class InspectionStatus(StrEnum):
    """Where a commissioned visit has got to.

    Six states, and the two that earn their place are `MORE_NEEDED` and
    `NOT_COMMISSIONED`. The first is not a failure — an adjuster who attends and
    finds they need a structural engineer has done their job, and a status that
    could only say "in progress" or "completed" would force the desk to record
    that as neither. The second is the resting state of most claims: a visit is a
    cost, and the majority of losses are settled on documents.
    """

    NOT_COMMISSIONED = "not_commissioned"
    TO_SCHEDULE = "to_schedule"
    VISIT_BOOKED = "visit_booked"
    IN_PROGRESS = "in_progress"
    MORE_NEEDED = "more_needed"
    COMPLETED = "completed"


class DamageSeverity(StrEnum):
    """How badly one element of the risk came off.

    `UNAFFECTED` is on the list deliberately: an adjuster recording that the
    sprinkler room was untouched is recording a finding, and a vocabulary with no
    word for it pushes that into free text where nothing can count it.
    """

    TOTAL_LOSS = "total_loss"
    SEVERE = "severe"
    MODERATE = "moderate"
    LIGHT = "light"
    UNAFFECTED = "unaffected"


class ClaimPartyRole(StrEnum):
    """Who is on the claim, in the vocabulary CLAWS screens and links.

    A superset of `app.domain.normalisation.PARTY_ROLES`, which is the *notice's*
    vocabulary, and deliberately a separate enum rather than a widening of it.

    They are two vocabularies because they answer to two different documents. The
    notice's set is what a broker's email can be read to contain. This one is what
    CLAWS entry category 5 screens and entry category 6 links — and it names three
    roles no email ever mentions: the underwriter who priced the risk, the internal
    handler who owns the file, and the loss adjuster instructed to inspect. That
    last one matters most: `parse_party_role` maps "loss_adjuster" to `other`, which
    loses the single role Attachment 2's entire loss-assessment lane is about.

    Widening the notice's set instead was considered and rejected. It would change
    what `parse_party_role` returns for an input it already handles, so existing
    rows would read `other` where new ones read `loss_adjuster` — a silent split in
    the data with no backfill to close it, in exchange for saving one enum.

    Claim creation maps the seven notice roles across one-for-one; the three added
    here can only be set by a person working the claim.
    """

    CLAIMANT = "claimant"
    INSURED = "insured"
    BROKER = "broker"
    THIRD_PARTY = "third_party"
    WITNESS = "witness"
    AUTHORITY = "authority"
    OTHER = "other"
    # --- Claim-side only, from CLAWS entry category 5 --------------------------
    UNDERWRITER = "underwriter"
    HANDLER = "handler"
    LOSS_ADJUSTER = "loss_adjuster"


class ActivityCategory(StrEnum):
    """Which part of the claim an audit event belongs to.

    Grouped by the part of the claim rather than by who acted, because that is how
    a handler reads the log back — "what happened to the money", "what happened to
    the documents" — and it is what the filter chips above the timeline offer.
    """

    INTAKE = "intake"
    DOCUMENTS = "documents"
    REVIEW = "review"
    INSPECTION = "inspection"
    ASSESSMENT = "assessment"
    FINANCIAL = "financial"
    FRAUD = "fraud"
    RECOVERY = "recovery"
    NOTE = "note"


class RecoveryKind(StrEnum):
    """The routes money comes back by.

    Five rather than the two a demo needs, because they behave differently and a
    handler chases them differently: subrogation is litigation against somebody at
    fault, salvage is selling what is left, reinsurance is an internal recovery
    under the treaty, contribution is another insurer paying its share, and
    reimbursement is the insured paying back an overpayment.
    """

    SUBROGATION = "subrogation"
    SALVAGE = "salvage"
    REINSURANCE = "reinsurance"
    CONTRIBUTION = "contribution"
    REIMBURSEMENT = "reimbursement"


class RecoveryStatus(StrEnum):
    """Where one recovery has got to."""

    IDENTIFIED = "identified"
    PURSUING = "pursuing"
    IN_NEGOTIATION = "in_negotiation"
    RECOVERED = "recovered"
    WRITTEN_OFF = "written_off"


#: Statuses in which the pursuit is over, one way or the other. A recovered
#: recovery is closed even if less came back than was expected; the shortfall is a
#: fact about the figures, not an open action.
CLOSED_RECOVERY_STATUSES = frozenset({RecoveryStatus.RECOVERED, RecoveryStatus.WRITTEN_OFF})


class SiuStatus(StrEnum):
    """How far an SIU referral has got.

    Six, and the last two are distinct on purpose: a case closed with no action
    found and a case closed having confirmed fraud are opposite outcomes, and
    collapsing them into "closed" would make the one statistic anybody asks about
    unanswerable.
    """

    NOT_REFERRED = "not_referred"
    SCREENING = "screening"
    REFERRED = "referred"
    UNDER_INVESTIGATION = "under_investigation"
    CLOSED_NO_ACTION = "closed_no_action"
    CLOSED_CONFIRMED = "closed_confirmed"


#: SIU states in which the investigation is finished.
CLOSED_SIU_STATUSES = frozenset({SiuStatus.CLOSED_NO_ACTION, SiuStatus.CLOSED_CONFIRMED})


class FraudDisposition(StrEnum):
    """What a reviewer concluded about one indicator.

    Two values and deliberately no "unsure": an indicator nobody has decided about
    has **no** disposition row, which is a different state from one somebody looked
    at and could not call. The absence is the third state.
    """

    ACCEPTED = "accepted"
    DISCOUNTED = "discounted"


class AssignmentStatus(StrEnum):
    RECOMMENDED = "recommended"
    ASSIGNED = "assigned"
    UNASSIGNED = "unassigned"


class AssignmentStrategy(StrEnum):
    SKILL_BASED = "skill_based"
    WORKLOAD = "workload"
    QUEUE = "queue"
    MANUAL = "manual"


class FieldSource(StrEnum):
    """Where a value on the case came from — the provenance a reviewer reads."""

    AI = "ai"
    HUMAN = "human"
    CHANNEL = "channel"
    DERIVED = "derived"


class DocumentExtractionStatus(StrEnum):
    PENDING = "pending"
    EXTRACTED = "extracted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class DocumentSource(StrEnum):
    """How a document came to be on a notice.

    `NOTIFICATION_BODY` is the interesting one. The body of the email that raised
    a notice is written out as a document so that a value read from it can be
    cited exactly like a value read from an attachment — stored, chunked,
    embedded, retrieved and highlighted by the same code. Everything downstream
    treats it as an ordinary document; this label is how the few things that
    should care — the case file's wording, and the extractor that always includes
    the notice itself — tell it apart.
    """

    UPLOAD = "upload"
    EMAIL_ATTACHMENT = "email_attachment"
    NOTIFICATION_BODY = "email_body"
    PORTAL = "portal"


class DocumentIndexStatus(StrEnum):
    """Where one document is in the read → chunk → embed sequence.

    Deliberately separate from `DocumentExtractionStatus`, which answers a
    different question: "could the text be read". A document can be `extracted`
    and `failed` at the same time — the text came out and the embedding call did
    not — and collapsing the two would mean an unreachable vector store looked
    like an unreadable survey report to whoever is asked to fix it.

    `SKIPPED` is not a failure. It means the passages exist and are searchable by
    keyword, but no embedding provider was configured to vectorise them, which is
    a normal state for a deployment that has not turned that on.
    """

    PENDING = "pending"
    INDEXING = "indexing"
    INDEXED = "indexed"
    SKIPPED = "skipped"
    FAILED = "failed"


#: Statuses that mean the document needs no further indexing work.
SETTLED_INDEX_STATUSES = frozenset({DocumentIndexStatus.INDEXED, DocumentIndexStatus.SKIPPED})


class PolicyIngestStatus(StrEnum):
    """Where one uploaded policy document is in the read → chunk → embed sequence.

    A separate enum from `DocumentIndexStatus`, which describes the same sequence
    over a *claim* attachment, and the reason is that these two have different
    terminal states and different consumers. A claim attachment that could not be
    embedded is still useful — the passages are keyword-searchable and the
    extraction reads the whole corpus when it has to. A policy document that never
    reached the vector index is a policy the matcher cannot find at all, so
    `EMBEDDED` and `CHUNKED` are held apart rather than collapsed into one
    "indexed": the second is a library entry that will not be matched on until an
    embedding provider exists, and an administrator has to be able to see that.

    `QUEUED` exists because ingestion is asynchronous. The upload request returns
    as soon as the bytes are stored and the row is written, so there is a real
    state between "accepted" and "a worker has it" that the Policies screen shows
    rather than guessing at.
    """

    PENDING = "pending"
    QUEUED = "queued"
    EXTRACTING = "extracting"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    FAILED = "failed"


#: Statuses that mean the document needs no further ingestion work. `CHUNKED` is
#: in here on purpose: with no embedding provider configured it is the *finished*
#: state, and re-queueing it on every sweep would be an infinite loop against a
#: deployment that is working exactly as configured.
SETTLED_POLICY_INGEST_STATUSES = frozenset(
    {PolicyIngestStatus.CHUNKED, PolicyIngestStatus.EMBEDDED}
)

#: The states an administrator is waiting on. What the Policies screen polls for.
PENDING_POLICY_INGEST_STATUSES = frozenset(
    {
        PolicyIngestStatus.PENDING,
        PolicyIngestStatus.QUEUED,
        PolicyIngestStatus.EXTRACTING,
    }
)


class ExtractionSchemaStatus(StrEnum):
    """Whether a configurable dataset is in use.

    `DRAFT` exists so a dataset can be built up field by field without every
    save re-reading every open notice against a half-written question set.
    Only an `ACTIVE` schema is run by the pipeline.
    """

    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ExtractionRunStatus(StrEnum):
    """How one execution of one dataset against one notice ended.

    `PARTIAL` is the one worth explaining: some fields came back and some did
    not, because a batch of them failed on its own. That is a normal outcome
    worth surfacing — it means the values on screen are real but incomplete, and
    a retry has something to do. Collapsing it into `FAILED` would throw away
    work that succeeded; collapsing it into `COMPLETED` would hide the gap.
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


#: Statuses that mean the run produced values worth showing.
PRODUCTIVE_RUN_STATUSES = frozenset({ExtractionRunStatus.COMPLETED, ExtractionRunStatus.PARTIAL})


class MailIntakeStatus(StrEnum):
    """Where one mailbox message is in its journey into the system.

    Deliberately separate from `FNOLStatus` and `ProcessingState`: this describes
    the *collection* of a message, which can fail — a Graph outage, an
    unreadable envelope — before there is any notice for a status to belong to.
    `FAILED` is retried on the next poll until `max_attempts` is reached, which
    is why the ledger carries an attempt count.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


#: Statuses that mean the message has been dealt with and must not be re-worked.
SETTLED_MAIL_STATUSES = frozenset({MailIntakeStatus.PROCESSED})


class MailAttachmentStatus(StrEnum):
    """What became of one attachment on one message.

    `SKIPPED` is not a failure: an inline signature image, a calendar item or a
    40MB video is refused by policy, and the row exists so that the refusal is
    visible to whoever asks why a document is not on the notice.
    """

    STORED = "stored"
    SKIPPED = "skipped"
    FAILED = "failed"


class NotificationKind(StrEnum):
    """What a handler is being told about.

    Deliberately the same dotted vocabulary as `AuditEventType`, and deliberately
    a shorter list. An audit event records everything that happened to a notice
    for a reader who came looking; a notification interrupts someone who did not.
    So this enum only grows when there is a moment a handler needs to know about
    *without having asked* — an email arriving on their desk, and how it went.

    The audit trail remains the record. These are the doorbell, not the ledger.
    """

    #: A broker's email landed in the shared mailbox and became a notice.
    FNOL_EMAIL_RECEIVED = "fnol.email_received"
    FNOL_PROCESSING_STARTED = "fnol.processing_started"
    FNOL_PROCESSING_SUCCEEDED = "fnol.processing_succeeded"
    FNOL_PROCESSING_FAILED = "fnol.processing_failed"


class NotificationTone(StrEnum):
    """How the panel draws one notification.

    Carried on the row rather than derived on the client from `kind`, because the
    same kind can warrant two tones: processing that completed with six open
    exceptions is a success the officer should still look at.
    """

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"


class AnalysisKind(StrEnum):
    """One row per kind per case; the newest is the current one."""

    EXTRACTION = "extraction"
    CLASSIFICATION = "classification"
    SUMMARY = "summary"
    SEVERITY = "severity"
    FRAUD = "fraud"
    COVERAGE = "coverage"
    COMPLETENESS = "completeness"
    DUPLICATES = "duplicates"
    POLICY_MATCH = "policy_match"
    #: The identification engine's whole answer for a case: the signals it read off
    #: the notice, the ranked candidates, and the near misses below the threshold.
    #: Stored as an analysis rather than in a table of its own because it *is* a
    #: computed reading fingerprinted by its inputs, which is exactly what this
    #: table holds — and because the candidates that can be *acted* on already have
    #: a table, `fnol_policy_matches`.
    POLICY_IDENTIFICATION = "policy_identification"
    CATASTROPHE = "catastrophe"
    TRIAGE = "triage"


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ActorType(StrEnum):
    HUMAN = "human"
    AI = "ai"
    SYSTEM = "system"


class AuditEventType(StrEnum):
    FNOL_CREATED = "fnol.created"
    FNOL_INGESTED = "fnol.ingested"
    FNOL_UPDATED = "fnol.updated"
    FNOL_STATUS_CHANGED = "fnol.status_changed"
    #: The notice and everything it held were purged. The event outlives the row
    #: it describes — audit is the one thing a deletion does not remove.
    FNOL_DELETED = "fnol.deleted"
    DOCUMENT_UPLOADED = "fnol.document_uploaded"
    DOCUMENT_DELETED = "fnol.document_deleted"
    PIPELINE_STARTED = "fnol.pipeline_started"
    PIPELINE_COMPLETED = "fnol.pipeline_completed"
    PIPELINE_FAILED = "fnol.pipeline_failed"
    EXTRACTION_COMPLETED = "fnol.extraction_completed"
    FIELD_CHANGED = "fnol.field_changed"
    POLICY_SELECTED = "fnol.policy_selected"
    POLICY_IDENTIFIED = "fnol.policy_identified"
    POLICY_REFERRED = "fnol.policy_referred"
    DUPLICATE_RESOLVED = "fnol.duplicate_resolved"
    SEVERITY_OVERRIDDEN = "fnol.severity_overridden"
    CLASSIFICATION_OVERRIDDEN = "fnol.classification_overridden"
    FRAUD_REVIEWED = "fnol.fraud_reviewed"
    CAT_MATCH_CONFIRMED = "fnol.cat_match_confirmed"
    CAT_MATCH_REMOVED = "fnol.cat_match_removed"
    EXCEPTION_RESOLVED = "fnol.exception_resolved"
    NOTE_ADDED = "fnol.note_added"
    CLAIM_CREATED = "claim.created"
    TRIAGE_COMPLETED = "claim.triage_completed"
    TRIAGE_OVERRIDDEN = "claim.triage_overridden"
    HANDLER_ASSIGNED = "claim.handler_assigned"
    CLAIM_NOTE_ADDED = "claim.note_added"
    CLAIM_STATUS_CHANGED = "claim.status_changed"
    #: The five decision verbs are one event type with the verb in `context`,
    #: not five event types. A reader filtering the trail for "decisions" wants
    #: them together, and the verb is a property of the decision rather than a
    #: different kind of event.
    CLAIM_DECIDED = "claim.decided"
    CLAIM_DECISION_BLOCKED = "claim.decision_blocked"
    COVERAGE_SELECTED = "claim.coverage_selected"
    COVERAGE_OVERRIDDEN = "claim.coverage_overridden"
    PARTY_ADDED = "claim.party_added"
    PARTY_LINKED_TO_COVERAGE = "claim.party_linked_to_coverage"
    DEDUCTIBLE_SET = "claim.deductible_set"
    INSPECTION_COMMISSIONED = "claim.inspection_commissioned"
    INSPECTION_SCHEDULED = "claim.inspection_scheduled"
    INSPECTION_ATTENDED = "claim.inspection_attended"
    INSPECTION_STATUS_CHANGED = "claim.inspection_status_changed"
    INSPECTION_OBSERVED = "claim.inspection_observed"
    INSPECTION_ACTION_SET = "claim.inspection_action_set"
    #: The adjuster sent their report to the handler. Distinct from the handler
    #: accepting it (`INSPECTION_STATUS_CHANGED` to `completed`): two people, two
    #: acts, and the trail has to be able to say which of them happened.
    INSPECTION_FILED = "claim.inspection_filed"
    RESERVE_MOVED = "claim.reserve_moved"
    PAYMENT_RECORDED = "claim.payment_recorded"
    RECOVERY_RECORDED = "claim.recovery_recorded"
    RECOVERY_OPENED = "claim.recovery_opened"
    RECOVERY_PROGRESSED = "claim.recovery_progressed"
    RECOVERY_TASK_SET = "claim.recovery_task_set"
    SIU_REFERRED = "claim.siu_referred"
    SIU_STATUS_CHANGED = "claim.siu_status_changed"
    FRAUD_INDICATOR_DISPOSED = "claim.fraud_indicator_disposed"
    DEDUCTIBLE_APPLIED = "claim.deductible_applied"


class Role(StrEnum):
    """Realm roles this module authorises against.

    Hyphenated because that is the form the identity provider issues them in and
    the form the frontend's `hasRole` is asked for them in.
    """

    FNOL_OFFICER = "fnol-officer"
    CLAIMS_HANDLER = "claims-handler"
    LOSS_ADJUSTER = "loss-adjuster"
    CLAIMS_MANAGER = "claims-manager"
    CLAIMS_ADMIN = "claims-admin"
    BUSINESS_ADMIN = "business-admin"


#: Who may work an FNOL: intake analysts, and the roles that supervise them.
FNOL_WRITE_ROLES: tuple[str, ...] = (
    Role.FNOL_OFFICER,
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_ADMIN,
)

#: Who may read intake. Handlers read the FNOL behind a claim they are working.
FNOL_READ_ROLES: tuple[str, ...] = (
    Role.FNOL_OFFICER,
    Role.CLAIMS_HANDLER,
    Role.LOSS_ADJUSTER,
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_ADMIN,
)

#: Reassigning a created claim is a manager's call, not an intake analyst's.
CLAIM_ASSIGN_ROLES: tuple[str, ...] = (Role.CLAIMS_MANAGER, Role.CLAIMS_ADMIN)

#: Who may work a claim: write a note, move a reserve, take a decision. Handlers
#: are in here and intake officers are not — an officer's job ends when the notice
#: becomes a claim, and a loss adjuster reads the file rather than writing to it.
#:
#: Note that a *settlement* is not gated by role. It is gated by the assigned
#: handler's authority limit, in `app.domain.claim_lifecycle.approval_blocks`,
#: which is how a claims desk actually works: seniority is a number on a person,
#: not a realm role, and a manager with no authority on the file has no more
#: business approving it than the handler does.
CLAIM_WORK_ROLES: tuple[str, ...] = (
    Role.CLAIMS_HANDLER,
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_ADMIN,
)

#: Who may work an *inspection*: record findings and file the report.
#:
#: **The one write a loss adjuster has**, and the reason this set exists rather
#: than reusing `CLAIM_WORK_ROLES`, which deliberately excludes them. That
#: exclusion was right while there was no inspection record — an adjuster read the
#: file — and it is wrong for this one surface: the report is the adjuster's own
#: work product, and a system where the handler types it up on their behalf is one
#: whose audit trail attributes the adjuster's findings to somebody else.
#:
#: Handlers and managers keep the write too. They commission the visit, they chase
#: it, and on a small loss they record what the adjuster told them on the telephone.
INSPECTION_WORK_ROLES: tuple[str, ...] = (
    Role.CLAIMS_HANDLER,
    Role.LOSS_ADJUSTER,
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_ADMIN,
)

#: Who may destroy a notification. Narrower than `FNOL_WRITE_ROLES` on purpose:
#: every other write on a notice is reversible or leaves the record standing, and
#: this one removes the notice, its documents, its passages and its dataset values
#: from every store that held them. An officer who thinks a notice should not be
#: worked rejects or cancels it — which keeps it, and keeps why.
FNOL_DELETE_ROLES: tuple[str, ...] = (Role.CLAIMS_MANAGER, Role.CLAIMS_ADMIN)

#: Who may change what the system reads out of a notice. Editing a dataset
#: changes every future extraction on the desk, so it sits with the roles that
#: already own configuration rather than with the officers who work the queue —
#: the same line the frontend's Admin Studio gate draws.
EXTRACTION_ADMIN_ROLES: tuple[str, ...] = (Role.CLAIMS_ADMIN, Role.BUSINESS_ADMIN)

#: Who may read a dataset definition. Everyone who reads intake, because the
#: review screen draws its panels from it on every page load — plus the
#: administrators, who own it and would otherwise be unable to see what they are
#: about to change.
EXTRACTION_READ_ROLES: tuple[str, ...] = tuple(
    dict.fromkeys((*FNOL_READ_ROLES, *EXTRACTION_ADMIN_ROLES))
)


#: Who may add to or remove from the policy library. Narrower than
#: `FNOL_WRITE_ROLES`: uploading a policy document changes what *every* future
#: notice is matched against, which is a configuration act rather than a
#: case-work one — the same line `EXTRACTION_ADMIN_ROLES` draws, plus the
#: managers who own the book on a commercial desk.
POLICY_LIBRARY_WRITE_ROLES: tuple[str, ...] = (
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_ADMIN,
    Role.BUSINESS_ADMIN,
)

#: Who may read the library and the matches drawn from it. Everyone who reads
#: intake, because the review screen shows an officer which policy wordings a
#: notice retrieved — plus the administrators who maintain it.
POLICY_LIBRARY_READ_ROLES: tuple[str, ...] = tuple(
    dict.fromkeys((*FNOL_READ_ROLES, *POLICY_LIBRARY_WRITE_ROLES))
)
