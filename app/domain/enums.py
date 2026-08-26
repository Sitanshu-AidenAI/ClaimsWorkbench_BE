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
