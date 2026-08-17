"""The validated shape of an FNOL extraction.

Every value the model returns arrives as a string with a confidence and the
snippet it was read from — never as a typed value. That is deliberate: a model
asked for a `date` will happily return `2026-13-45`, and a schema that declared
the field a date would either reject the whole response or, worse, coerce it.
Strings in, `app.domain.normalisation` out, and a field that will not parse is
recorded as missing rather than as wrong.

Nothing here has a default. A strict structured-output schema requires every
property, and a field the model genuinely could not find comes back as
`{"value": null, "confidence": 0, "evidence": null}`, which is a real answer.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Free text the model may echo back. Bounded so a runaway generation cannot
#: write a megabyte into a column.
MAX_VALUE_LENGTH = 2000
MAX_EVIDENCE_LENGTH = 600


class AIModel(BaseModel):
    """Base for everything the provider is asked to produce."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


#: A passage label as the extraction prompt prints it: `C1`, `C12`. Anything else is
#: a label the model invented, and is discarded rather than stored — see the validator.
_CHUNK_LABEL_RE = re.compile(r"^C\d{1,3}$")


class ExtractedField(AIModel):
    """One value, with how sure the model was and where it read it."""

    value: str | None = Field(description="The value exactly as stated in the source, or null.")
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="0 when absent, 1 when stated unambiguously."
    )
    evidence: str | None = Field(
        description="The phrase the value was read from. Null when inferred."
    )
    #: Which retrieved passage this value came from, so the review screen can show the
    #: officer the page it is written on. Null when it came from the notification body,
    #: which has no passage label — and null is the honest answer there rather than a
    #: guess at the nearest one.
    #:
    #: The one field here with a default, and the reason is worth recording: the
    #: strict-mode requirement is enforced by `pydantic_json_schema`'s `_tighten`,
    #: which marks *every* property required in the schema sent to the provider
    #: regardless of what the Python model defaults to. So the default is invisible to
    #: the model and visible only to the deterministic reader in
    #: `app.domain.heuristics`, which reads whole text rather than passages and has no
    #: label to give.
    source_chunk_ref: str | None = Field(
        default=None,
        description=(
            "The label of the passage this value was read from, exactly as printed "
            "(for example 'C3'). Null when the value came from the notification body "
            "rather than from a passage. Never invent a label."
        ),
    )

    @field_validator("value")
    @classmethod
    def _bound_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed or trimmed.lower() in {"null", "none", "n/a", "unknown", "not stated"}:
            return None
        return trimmed[:MAX_VALUE_LENGTH]

    @field_validator("evidence")
    @classmethod
    def _bound_evidence(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed[:MAX_EVIDENCE_LENGTH] if trimmed else None

    @field_validator("source_chunk_ref")
    @classmethod
    def _bound_chunk_ref(cls, value: str | None) -> str | None:
        """Reject anything that is not label-shaped.

        A model that answers "the survey report" or "page 4" here has not cited a
        passage, and storing it would put text in a column the review screen resolves
        as an identifier. Whether the label names a passage the model was actually
        shown is checked later, by the service that knows what it showed it.
        """
        if value is None:
            return None
        trimmed = value.strip().upper()
        return trimmed if _CHUNK_LABEL_RE.match(trimmed) else None

    @property
    def present(self) -> bool:
        return self.value is not None


class NotificationExtraction(AIModel):
    reporter_name: ExtractedField
    reporter_organisation: ExtractedField
    reporter_role: ExtractedField
    reporter_email: ExtractedField
    reporter_phone: ExtractedField


class PolicyExtraction(AIModel):
    policy_number: ExtractedField
    insured_name: ExtractedField
    insured_organisation: ExtractedField
    policy_type: ExtractedField
    effective_date: ExtractedField
    expiry_date: ExtractedField


class LossExtraction(AIModel):
    date_of_loss: ExtractedField
    time_of_loss: ExtractedField
    loss_location: ExtractedField
    loss_country: ExtractedField
    loss_description: ExtractedField
    cause_of_loss: ExtractedField
    affected_assets: ExtractedField
    injuries: ExtractedField
    fatalities: ExtractedField
    estimated_loss_amount: ExtractedField
    currency: ExtractedField
    business_interruption: ExtractedField
    structural_damage: ExtractedField
    environmental_exposure: ExtractedField


class PartyExtraction(AIModel):
    """Someone named in the notification.

    `role` is free text from the model and is mapped onto the known party roles by
    `app.domain.normalisation`; an unrecognised role becomes `other` rather than
    inventing a category.
    """

    role: str
    name: str
    organisation: str | None
    email: str | None
    phone: str | None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]


class AdditionalExtraction(AIModel):
    police_reference: ExtractedField
    incident_reference: ExtractedField
    authorities_involved: ExtractedField
    repair_estimate_amount: ExtractedField
    potential_litigation: ExtractedField
    supporting_documents: ExtractedField


class FNOLExtraction(AIModel):
    """Everything one extraction call returns.

    One call for the whole notice rather than one per field: a language model
    reading a broker's email answers thirty questions from the same context, and
    thirty calls would cost thirty times as much to produce a *less* coherent
    answer — the date of loss and the description have to agree with each other.
    """

    notification: NotificationExtraction
    policy: PolicyExtraction
    loss: LossExtraction
    additional: AdditionalExtraction
    parties: list[PartyExtraction]
    #: The model's own view of how much of the notice it could read. Used only as
    #: a signal on screen; completeness is computed deterministically.
    overall_confidence: Annotated[float, Field(ge=0.0, le=1.0)]


class ClassificationResult(AIModel):
    """The line and loss type a notice belongs to.

    `line_of_business` and `loss_type` are checked against the configured
    vocabulary after validation — the schema cannot express "one of the values
    this carrier writes", and a model that answers "Aviation Hull" for a carrier
    that does not write aviation must not create the category by saying it.
    """

    line_of_business: str
    claim_type: str | None
    loss_type: str | None
    complexity: str
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    reasoning: str


class SummaryResult(AIModel):
    """The executive paragraph at the head of the review screen."""

    summary: str
    key_points: list[str]


__all__ = [
    "AIModel",
    "AdditionalExtraction",
    "ClassificationResult",
    "ExtractedField",
    "FNOLExtraction",
    "LossExtraction",
    "NotificationExtraction",
    "PartyExtraction",
    "PolicyExtraction",
    "SummaryResult",
]
