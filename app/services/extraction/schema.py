"""What a dataset is, in memory, and how a value of it is typed.

The database rows are the record; these are the shapes the engine works with, so
the engine never touches a SQLAlchemy object and stays testable with a literal.

**Coercion never destroys.** A model asked for a date returns `12/13/2026` often
enough that a parser which raises would lose the value and the citation with it.
So every coercion returns `(coerced, error)`: the coerced form goes in
`value_json`, the text the document actually said stays in `value_text`, and a
failure becomes a sentence on the row and a flag for review. A value a person can
see and correct is worth more than a null that is technically well-typed.

The type list is deliberately short. Nine types cover an insurance dataset, and
each one exists because something downstream reads it differently — a `money`
renders with a currency, a `boolean` drives a checkbox, a `json` becomes rows.
Adding a tenth should require justifying what changes about how it is displayed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.domain import normalisation

#: Every type a field may declare. The default is `string`: a dataset author who
#: gives no type gets the one that cannot lose information.
DATA_TYPES: frozenset[str] = frozenset(
    {"string", "text", "integer", "number", "money", "date", "datetime", "boolean", "json"}
)

#: Types whose coerced form is worth storing separately from the document's own
#: wording. A `string` is its own coerced form, so storing it twice is noise.
_TYPED = DATA_TYPES - {"string", "text"}

_MONEY_RE = re.compile(r"-?\d[\d,\s]*(?:\.\d+)?")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field to extract, as the engine sees it."""

    key: str
    label: str
    #: Prose in the vocabulary a *document* uses. Used verbatim as the retrieval
    #: query, which is why it is the longest column in the table and why a
    #: dataset author's time is best spent here.
    description: str
    data_type: str = "string"
    group_label: str = "Fields"
    required: bool = False
    position: int = 0
    aliases: tuple[str, ...] = ()
    #: A rule for the model about this field only. Kept out of `description`
    #: because a rule makes a poor search query.
    extraction_hint: str | None = None

    @property
    def query(self) -> str:
        """What retrieval searches for.

        The label leads, then the description, then the aliases. All three,
        because a document may name the thing the way the schema does, describe
        it the way the description does, or use a synonym — and a single
        embedding of all three is one search rather than three.
        """
        parts = [self.label, self.description, *self.aliases]
        return " ".join(part.strip() for part in parts if part and part.strip())

    def coerce(self, value: str | None) -> tuple[Any, str | None]:
        """`(coerced, error)`. Never raises, never discards a value."""
        return coerce_value(value, self.data_type)


@dataclass(frozen=True, slots=True)
class DatasetSchema:
    """A named set of fields, and the confidence below which a human is asked."""

    key: str
    name: str
    description: str | None = None
    version: int = 1
    review_threshold: float = 0.6
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.fields:
            return
        seen: set[str] = set()
        for spec in self.fields:
            if spec.key in seen:
                raise ValueError(f"Duplicate field key in schema {self.key!r}: {spec.key!r}")
            seen.add(spec.key)

    @property
    def groups(self) -> tuple[str, ...]:
        """The panels this dataset draws, in the order its fields declare them."""
        ordered: list[str] = []
        for spec in self.fields:
            if spec.group_label not in ordered:
                ordered.append(spec.group_label)
        return tuple(ordered)

    def field(self, key: str) -> FieldSpec | None:
        return next((spec for spec in self.fields if spec.key == key), None)


def normalise_data_type(value: str | None) -> str:
    """Map a declared type onto one this module knows, defaulting to `string`."""
    cleaned = (value or "").strip().lower()
    aliases = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "decimal": "number",
        "bool": "boolean",
        "yes/no": "boolean",
        "currency": "money",
        "amount": "money",
        "object": "json",
        "array": "json",
        "list": "json",
    }
    resolved = aliases.get(cleaned, cleaned)
    return resolved if resolved in DATA_TYPES else "string"


def stores_typed_value(data_type: str) -> bool:
    """Whether a coerced form is worth keeping alongside the document's wording."""
    return data_type in _TYPED


def coerce_value(value: str | None, data_type: str) -> tuple[Any, str | None]:
    """Turn the text a document states into the type a field declares.

    Returns `(coerced, error)`. `coerced` is `None` when the text could not be
    read as the declared type — and `error` then says so in a sentence a reviewer
    can act on, because "12/13/2026 is not a date this reads as day/month/year"
    tells them what to do and a silent null does not.
    """
    if value is None:
        return None, None
    text = value.strip()
    if not text:
        return None, None

    match data_type:
        case "string" | "text":
            return text, None
        case "integer":
            return _coerce_integer(text)
        case "number":
            return _coerce_number(text)
        case "money":
            return _coerce_money(text)
        case "date":
            return _coerce_temporal(text, want_time=False)
        case "datetime":
            return _coerce_temporal(text, want_time=True)
        case "boolean":
            parsed = normalisation.parse_bool(text)
            if parsed is None:
                return None, f"{text!r} does not read as yes or no."
            return parsed, None
        case "json":
            return _coerce_json(text)
        case _:  # pragma: no cover — normalise_data_type prevents this
            return text, None


#: Ways a document says "there were none". Read as zero rather than as missing,
#: because "no injuries" is a fact a claims officer needs stated — an empty
#: injuries field means nobody looked, and zero means somebody did.
_EXPLICIT_ZERO = frozenset(
    {"none", "nil", "zero", "no", "n/a", "none reported", "none stated", "not applicable"}
)


def _coerce_integer(text: str) -> tuple[Any, str | None]:
    count = normalisation.parse_count(text)
    if count is not None:
        return count, None

    cleaned = text.strip().lower().rstrip(".")
    if cleaned in _EXPLICIT_ZERO or cleaned.startswith(("none ", "no ", "nil ")):
        return 0, None

    number, error = _coerce_number(text)
    if number is None:
        return None, error or f"{text!r} does not read as a whole number."
    return int(number), None


def _coerce_number(text: str) -> tuple[Any, str | None]:
    match = _MONEY_RE.search(text)
    if match is None:
        return None, f"{text!r} contains no number."
    try:
        return float(re.sub(r"[,\s]", "", match.group(0))), None
    except ValueError:  # pragma: no cover — the pattern guarantees a parseable body
        return None, f"{text!r} does not read as a number."


def _coerce_money(text: str) -> tuple[Any, str | None]:
    """Minor units, so a stored amount is an integer and never a float of pence."""
    minor = normalisation.parse_money_minor(text)
    if minor is None:
        return None, f"{text!r} does not read as an amount of money."
    return minor, None


def _coerce_temporal(text: str, *, want_time: bool) -> tuple[Any, str | None]:
    parsed = normalisation.parse_datetime(text)
    if parsed is None:
        return None, f"{text!r} does not read as a date."
    if want_time:
        return parsed.isoformat(), None
    return parsed.date().isoformat(), None


def _coerce_json(text: str) -> tuple[Any, str | None]:
    """Parse a JSON value, tolerating the fences a model wraps them in."""
    candidate = text.strip()
    if candidate.startswith("```"):
        # ```json … ``` — strip the fence and its language tag.
        candidate = candidate.split("```", 2)[1] if candidate.count("```") >= 2 else candidate
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.rsplit("```", 1)[0]
    try:
        return json.loads(candidate.strip()), None
    except (ValueError, TypeError):
        return None, "The value is not valid JSON."


def render_value(coerced: Any, data_type: str) -> str | None:
    """The coerced value as text, for a store that only has a text column.

    Used by the FNOL adapter, which mirrors into a table whose value column is
    `Text`. Kept here so the two representations of one value cannot drift.
    """
    if coerced is None:
        return None
    if isinstance(coerced, bool):
        return "true" if coerced else "false"
    if data_type == "money" and isinstance(coerced, int):
        return f"{coerced / 100:.2f}"
    if isinstance(coerced, date | datetime):  # pragma: no cover — coercion returns ISO strings
        return coerced.isoformat()
    if isinstance(coerced, dict | list):
        return json.dumps(coerced, ensure_ascii=False)
    return str(coerced)


__all__ = [
    "DATA_TYPES",
    "DatasetSchema",
    "FieldSpec",
    "coerce_value",
    "normalise_data_type",
    "render_value",
    "stores_typed_value",
]
