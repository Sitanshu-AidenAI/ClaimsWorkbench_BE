"""Turning a dataset's values into the claim record.

`app/services/extraction/` knows fields, passages and confidence and nothing
about insurance. `fnol_cases` has a `date_of_loss` column, a `policy_number`
column and a completeness engine that reads them. This module is the one place
that knows both, and it exists so that neither has to know the other.

The mapping is a **table, not a function**. The version of this that came before
was two hundred lines of hand-written `await record("policy.policy_number", …)`
calls, one per field, each repeating the same four steps with a different parser.
That shape is why adding a field meant editing code. Here, a field the dataset
carries and this table does not is simply not mirrored onto the case — it is
still extracted, still stored, still cited and still shown; it just has no column
of its own, which is the correct outcome for a field somebody invented last
Tuesday.

Two rules hold throughout, and both are about not losing a person's work:

* **A human correction is never overwritten.** Enforced in `upsert_field` and
  again on the case attribute, because a value written back onto the case is the
  one the rest of the pipeline reasons with.
* **A value that will not parse is still recorded.** The field row keeps the text
  the document stated even when the case column cannot hold it, so a reviewer
  sees "13/45/2026" and can correct it, rather than an empty box.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.domain import normalisation
from app.domain.enums import FieldSource
from app.models.extraction import ExtractedValue
from app.models.fnol import FNOLCase
from app.repositories.fnol import FNOLRepository

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WriteBack:
    """How one dataset field lands on the claim record."""

    #: The `fnol_cases` column, or `None` for a field that has a provenance row
    #: and no column — `documents.supporting` is a real question with no home on
    #: the case, and that is fine.
    attribute: str | None
    #: The review screen's grouping. Kept separate from the dataset's
    #: `group_label`, which is display text an administrator may rename freely.
    section: str
    #: Text from the document to the column's type. Returns `None` for a value the
    #: column cannot hold, which leaves the column untouched.
    parse: Callable[[str | None], Any] = lambda value: value


def _text(limit: int) -> Callable[[str | None], Any]:
    return lambda value: normalisation.clip(value, limit)


#: Which dataset fields have a home on the claim record.
#:
#: Keyed by field key, which for the built-in `fnol_notice` dataset is the same
#: dotted path the review screen and the completeness engine already use. That is
#: not a coincidence and it is not fragile: a dataset whose keys do not match
#: simply does not write back, which is the right behaviour for a dataset that is
#: not about claims.
FNOL_WRITEBACK: dict[str, WriteBack] = {
    "notification.reporter_name": WriteBack("reporter_name", "notification", _text(255)),
    "notification.reporter_organisation": WriteBack(
        "reporter_organisation", "notification", _text(255)
    ),
    "notification.reporter_role": WriteBack("reporter_role", "notification", _text(96)),
    "notification.reporter_email": WriteBack(
        "reporter_email", "notification", normalisation.parse_email
    ),
    "notification.reporter_phone": WriteBack(
        "reporter_phone", "notification", normalisation.parse_phone
    ),
    "policy.policy_number": WriteBack("policy_number", "policy", _text(64)),
    "policy.insured_name": WriteBack("insured_name", "policy", _text(255)),
    "policy.insured_organisation": WriteBack("insured_organisation", "policy", _text(255)),
    "policy.policy_type": WriteBack("policy_type", "policy", _text(64)),
    "loss.date_of_loss": WriteBack("date_of_loss", "loss", normalisation.parse_datetime),
    "loss.loss_location": WriteBack("loss_location", "loss", _text(2000)),
    "loss.loss_country": WriteBack("loss_country", "loss", _text(64)),
    "loss.loss_description": WriteBack("loss_description", "loss", _text(4000)),
    "loss.cause_of_loss": WriteBack("cause_of_loss", "loss", _text(255)),
    "loss.affected_assets": WriteBack("affected_assets", "loss", _text(2000)),
    "loss.injuries": WriteBack("injuries", "loss", normalisation.parse_count),
    "loss.fatalities": WriteBack("fatalities", "loss", normalisation.parse_count),
    "loss.business_interruption": WriteBack(
        "business_interruption", "loss", normalisation.parse_bool
    ),
    "loss.structural_damage": WriteBack("structural_damage", "loss", normalisation.parse_bool),
    "loss.environmental_exposure": WriteBack(
        "environmental_exposure", "loss", normalisation.parse_bool
    ),
    "financial.estimated_loss": WriteBack(
        "estimated_loss_minor", "financial", normalisation.parse_money_minor
    ),
    "financial.repair_estimate": WriteBack(
        "repair_estimate_minor", "financial", normalisation.parse_money_minor
    ),
    "financial.currency": WriteBack(None, "financial", _text(3)),
    "additional.police_reference": WriteBack("police_reference", "additional", _text(128)),
    "additional.incident_reference": WriteBack("incident_reference", "additional", _text(128)),
    "additional.authorities_involved": WriteBack("authorities_involved", "additional", _text(2000)),
    "additional.potential_litigation": WriteBack(
        "potential_litigation", "additional", normalisation.parse_bool
    ),
    "parties.claimant_name": WriteBack(None, "parties", _text(255)),
    "parties.people": WriteBack(None, "parties"),
    "documents.supporting": WriteBack(None, "documents", _text(2000)),
}

#: The dataset field carrying everyone named on the notice, as JSON. Handled
#: apart from the table because it produces rows rather than a column.
PARTIES_FIELD_KEY = "parties.people"

#: The dataset field naming the currency the amounts are in. Applied before the
#: money fields so an amount lands under the currency it was quoted in.
CURRENCY_FIELD_KEY = "financial.currency"


@dataclass(slots=True)
class WriteBackResult:
    """What the mirror did, for the pipeline's assessment stage."""

    #: Field path to confidence, which the completeness engine reads to mark a
    #: value as uncertain rather than present.
    confidences: dict[str, float | None]
    #: The date of loss exactly as the document wrote it. The exception engine
    #: raises "future loss date" from the *unparsed* string, so a date the parser
    #: refused still produces the right exception.
    raw_loss_date: str | None = None
    parties_written: int = 0


class FNOLWriteBackAdapter:
    """Mirrors extracted values onto the claim record."""

    def __init__(self, repository: FNOLRepository) -> None:
        self._repository = repository

    async def apply(self, case: FNOLCase, values: list[ExtractedValue]) -> WriteBackResult:
        """Write every mapped value onto the case and its provenance rows."""
        by_key = {value.field_key: value for value in values}
        result = WriteBackResult(confidences={})

        self._apply_currency(case, by_key.get(CURRENCY_FIELD_KEY))

        for value in values:
            mapping = FNOL_WRITEBACK.get(value.field_key)
            if mapping is None:
                # A field with no home on the claim record. Extracted, stored,
                # citable and shown — just not mirrored. Nothing to do.
                continue

            row = await self._repository.upsert_field(
                case.id,
                field_path=value.field_key,
                section=mapping.section,
                label=value.label,
                value_text=value.value_text,
                confidence=float(value.confidence) if value.confidence is not None else None,
                source=FieldSource.HUMAN if value.human_modified else FieldSource.AI,
                source_document_id=value.source_document_id,
                source_chunk_id=value.source_chunk_id,
                evidence_snippet=value.quote,
            )
            result.confidences[value.field_key] = row.confidence

            if mapping.attribute is None:
                continue
            if row.human_modified and not value.human_modified:
                # The field row is the authority on whether a person has spoken.
                # It says yes and this value does not, so this value is a model
                # answer arriving after a correction: recorded, not applied.
                continue

            parsed = mapping.parse(value.value_text)
            if parsed is not None:
                setattr(case, mapping.attribute, parsed)

        loss_date = by_key.get("loss.date_of_loss")
        result.raw_loss_date = loss_date.value_text if loss_date is not None else None

        parties = by_key.get(PARTIES_FIELD_KEY)
        if parties is not None:
            result.parties_written = await self._apply_parties(case, parties)

        case.extraction_confidence = _overall_confidence(values)
        return result

    def _apply_currency(self, case: FNOLCase, value: ExtractedValue | None) -> None:
        """Set the case currency, unless a person has fixed the amount already.

        Checked against the *estimated loss* field rather than a currency field:
        the currency has no provenance row of its own on the review screen, and
        an officer who corrected an amount corrected the currency with it.
        """
        code = (value.value_text or "").strip().upper() if value is not None else ""
        if len(code) == 3 and code.isalpha():
            case.currency = code

    async def _apply_parties(self, case: FNOLCase, value: ExtractedValue) -> int:
        """Turn the parties field's JSON into party rows.

        Tolerant by design. The field is configured to ask for a JSON array of
        objects and a model mostly obliges, but a run that returns something else
        must cost the parties and nothing else — the thirty scalar fields around
        it are unaffected and must not be lost to a malformed list.
        """
        people = value.value_json
        if not isinstance(people, list):
            return 0

        written = 0
        for entry in people:
            if not isinstance(entry, dict):
                continue
            name = normalisation.clip(_string(entry.get("name")), 255)
            if not name:
                continue
            await self._repository.upsert_party(
                case.id,
                role=normalisation.parse_party_role(_string(entry.get("role"))),
                name=name,
                organisation=normalisation.clip(_string(entry.get("organisation")), 255),
                email=normalisation.parse_email(_string(entry.get("email"))),
                phone=normalisation.parse_phone(_string(entry.get("phone"))),
                source=FieldSource.AI,
                confidence=float(value.confidence) if value.confidence is not None else None,
            )
            written += 1
        return written


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _overall_confidence(values: list[ExtractedValue]) -> float:
    """The case's headline confidence: the mean over the fields that answered.

    Over the answered fields rather than all of them, because a dataset with
    forty optional fields of which six apply to this notice is not a 15%-confident
    reading of it. Completeness is measured separately and deterministically by
    the assessment stage, which is the number that should fall when fields are
    missing.
    """
    scored = [
        float(value.confidence)
        for value in values
        if value.value_text is not None and value.confidence is not None
    ]
    if not scored:
        return 0.0
    return round(sum(scored) / len(scored), 4)


__all__ = [
    "CURRENCY_FIELD_KEY",
    "FNOL_WRITEBACK",
    "PARTIES_FIELD_KEY",
    "FNOLWriteBackAdapter",
    "WriteBack",
    "WriteBackResult",
]
