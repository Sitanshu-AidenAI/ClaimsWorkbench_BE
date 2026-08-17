"""The one prompt, and the shape the model answers in.

There is a single prompt for every dataset. That is the point of the module: a
new field is a row in a table, not a new paragraph of prompt engineering. What
varies between calls is the *field list* and the *passages*, both of which are
rendered into the user message from configuration.

The answer shape is fixed, so it is a static Pydantic model rather than one
generated per dataset. A generated model would give the provider a schema whose
keys are the field keys, which sounds tighter and is worse in two ways: a
provider caches on schema identity, so every dataset edit would cold-start it,
and a strict-mode schema requiring thirty specific keys makes a model invent
values for the ones it could not find rather than omit them. A list of answers
lets a model say nothing about a field, which is the honest answer and the one
this codebase already prefers everywhere else.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field, field_validator

from app.domain.extraction import AIModel
from app.services.extraction.schema import FieldSpec

#: Free text bounds, matching `app.domain.extraction` so a runaway generation
#: cannot write a megabyte into a column.
MAX_VALUE_LENGTH = 4000
MAX_QUOTE_LENGTH = 600

#: A passage label as the prompt prints it. Anything else is a label the model
#: invented, and is dropped rather than stored — provenance pointing at a passage
#: the model was never shown is worse than no provenance.
_LABEL_RE = re.compile(r"^C\d{1,3}$")

#: Values a model returns to mean "not stated". Treated as absent, because a
#: field whose value is the string "unknown" reads on the review screen as a
#: value somebody wrote down.
_ABSENT = frozenset(
    {"null", "none", "n/a", "na", "not stated", "not specified", "unknown", "not provided", "-"}
)


SYSTEM_PROMPT = (
    "You are an insurance document-extraction engine. You are given passages from "
    "a claim notification and the documents attached to it, and a list of fields to "
    "find. You return only what the passages actually state.\n"
    "Rules you must follow:\n"
    "1. Never infer a reference number, a name, a date or an amount that is not "
    "written in the passages. A field you cannot find is one you leave out.\n"
    "2. Return one answer per field you found, and no answer at all for a field you "
    "did not. Do not return a placeholder.\n"
    "3. Copy each value as the source writes it. Do not reformat dates, convert "
    "currencies, expand abbreviations or tidy up spelling.\n"
    "4. `quote` must be text copied verbatim from the passage you read the value "
    "from, short and containing the value. It is used to highlight the source, so a "
    "paraphrase points a reviewer at nothing.\n"
    "5. `passage` must be the label of the passage you read the value from, exactly "
    "as printed above it — for example 'C3'. Never invent a label.\n"
    "6. `confidence` is 1.0 only when the passage states the value explicitly and "
    "unambiguously; 0.5 to 0.8 when you inferred it from context. Do not return a "
    "field you are guessing at.\n"
    "7. Where two passages disagree, prefer the one from a formal document over the "
    "one from an email body, and say so is uncertain by lowering your confidence."
)


class FieldAnswer(AIModel):
    """One field the model found, and where it read it."""

    field_key: str = Field(description="The exact key of the field, as listed in the request.")
    value: str | None = Field(description="The value exactly as the passage states it.")
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        description="1.0 when stated explicitly; 0.5-0.8 when inferred from context."
    )
    quote: str | None = Field(
        description="Text copied verbatim from the passage, short, containing the value."
    )
    passage: str | None = Field(
        description="The label of the passage the value was read from, e.g. 'C3'."
    )

    @field_validator("value")
    @classmethod
    def _bound_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed or trimmed.lower() in _ABSENT:
            return None
        return trimmed[:MAX_VALUE_LENGTH]

    @field_validator("quote")
    @classmethod
    def _bound_quote(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed[:MAX_QUOTE_LENGTH] if trimmed else None

    @field_validator("passage")
    @classmethod
    def _bound_passage(cls, value: str | None) -> str | None:
        """Reject anything that is not label-shaped.

        A model answering "the survey report" or "page 4" here has not cited a
        passage. Whether the label names a passage it was actually shown is
        checked later, by the engine that knows what it showed it.
        """
        if value is None:
            return None
        trimmed = value.strip().upper()
        return trimmed if _LABEL_RE.match(trimmed) else None

    @property
    def present(self) -> bool:
        return self.value is not None


class FieldAnswerSet(AIModel):
    """Everything one call returns."""

    answers: list[FieldAnswer] = Field(
        description="One entry per field found. Omit any field the passages do not state."
    )


def render_fields(specs: list[FieldSpec]) -> str:
    """The field list, as the model is asked to answer it.

    The label leads and the key follows in brackets, because the model answers
    with the key and reads with the label — and putting the human-readable name
    first measurably improves which passage it attends to.
    """
    lines: list[str] = []
    for spec in specs:
        line = f"- {spec.label} [{spec.key}] ({spec.data_type}): {spec.description}"
        if spec.aliases:
            line += f"\n  Also written as: {', '.join(spec.aliases)}."
        if spec.extraction_hint:
            line += f"\n  {spec.extraction_hint}"
        lines.append(line)
    return "\n".join(lines)


def render_user_prompt(*, channel: str, fields: str, passages: str) -> str:
    return (
        f"Notification channel: {channel}.\n\n"
        "=== FIELDS TO FIND ===\n"
        f"{fields}\n\n"
        "=== PASSAGES ===\n"
        f"{passages}\n"
        "=== END OF PASSAGES ===\n\n"
        "Return one answer for each field the passages state, and omit the rest."
    )


__all__ = [
    "MAX_QUOTE_LENGTH",
    "MAX_VALUE_LENGTH",
    "SYSTEM_PROMPT",
    "FieldAnswer",
    "FieldAnswerSet",
    "render_fields",
    "render_user_prompt",
]
