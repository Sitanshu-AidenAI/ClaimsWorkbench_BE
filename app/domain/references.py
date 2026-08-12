"""Human-facing references.

`FNOL-2026-000412`, `CLM-2026-001284`. The sequence per prefix and year lives in
a database sequence table rather than in a counter here, because two API workers
allocating the same number is exactly the failure a claims desk cannot absorb —
`app.repositories.reference` owns the allocation, this module owns the shape.
"""

from __future__ import annotations

import re

FNOL_PREFIX = "FNOL"
CLAIM_PREFIX = "CLM"

#: Six digits: a large carrier books six figures of notifications a year, and a
#: reference that grew a digit mid-year would break every column it is aligned in.
_SEQUENCE_WIDTH = 6

_REFERENCE_RE = re.compile(r"^(?P<prefix>[A-Z]{2,6})-(?P<year>\d{4})-(?P<sequence>\d{4,8})$")


def format_reference(prefix: str, year: int, sequence: int) -> str:
    return f"{prefix}-{year:04d}-{sequence:0{_SEQUENCE_WIDTH}d}"


def parse_reference(reference: str) -> tuple[str, int, int] | None:
    """Split a reference back into its parts, or `None` when it is not one."""
    match = _REFERENCE_RE.match(reference.strip().upper())
    if not match:
        return None
    return (
        match.group("prefix"),
        int(match.group("year")),
        int(match.group("sequence")),
    )


def is_fnol_reference(value: str) -> bool:
    parsed = parse_reference(value)
    return parsed is not None and parsed[0] == FNOL_PREFIX


def is_claim_reference(value: str) -> bool:
    parsed = parse_reference(value)
    return parsed is not None and parsed[0] == CLAIM_PREFIX
