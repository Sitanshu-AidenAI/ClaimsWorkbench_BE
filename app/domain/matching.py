"""Deterministic similarity primitives.

Policy matching, duplicate detection and catastrophe matching all need the same
three questions answered: how alike are two names, how alike are two free-text
descriptions, and how far apart are two places. None of that is a job for a
language model — it is arithmetic, it has to be reproducible, and an auditor has
to be able to recompute a 0.91 duplicate score two years later and get 0.91.

Everything here is pure and cheap enough to run over a few thousand candidates.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence

#: Words carrying no discriminating power in an insurance context. Dropping them
#: stops "the damage to the property" matching "the damage to the vehicle" on the
#: strength of "the", "to" and "damage".
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "for",
        "from",
        "had",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "our",
        "their",
        "there",
        "they",
        "we",
        "you",
        "your",
        "please",
        "kind",
        "regards",
        "dear",
        "hi",
        "hello",
        "thanks",
        "thank",
        "regarding",
        "ref",
        "re",
        "fwd",
        "fw",
    ]
)

#: Company-form suffixes stripped before comparing organisation names, so
#: "Northline Logistics Ltd" and "Northline Logistics Limited" are one name.
_COMPANY_SUFFIXES = (
    "limited",
    "ltd",
    "plc",
    "llp",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "gmbh",
    "ag",
    "sa",
    "bv",
    "nv",
    "pty",
    "holdings",
    "group",
    "international",
    "uk",
    "europe",
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalise(value: str | None) -> str:
    """Lowercase, strip accents, collapse whitespace — the gate everything else goes through."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", stripped.strip().lower())


def tokens(value: str | None, *, drop_stopwords: bool = True) -> set[str]:
    words = _WORD_RE.findall(normalise(value))
    if drop_stopwords:
        return {word for word in words if word not in _STOPWORDS and len(word) > 1}
    return set(words)


def normalise_reference(value: str | None) -> str:
    """A policy or claim reference reduced to comparable characters.

    Punctuation and spacing in a reference are formatting, not identity:
    `POL-2026/0041` and `pol 2026 0041` are the same policy, and a broker's email
    will render it either way.
    """
    return re.sub(r"[^a-z0-9]", "", normalise(value))


def normalise_organisation(value: str | None) -> str:
    """An organisation name with its company form removed."""
    words = _WORD_RE.findall(normalise(value))
    while words and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """Overlap of two token sets, 0..1. Empty on either side scores zero."""
    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    intersection = len(left_set & right_set)
    union = len(left_set | right_set)
    return intersection / union if union else 0.0


def token_containment(left: Iterable[str], right: Iterable[str]) -> float:
    """How much of the smaller token set the larger one contains.

    Kinder than Jaccard where one side is a short reference and the other a whole
    email body — "Northline warehouse fire" inside a 400-word notification should
    not be penalised for the 397 words it does not share.
    """
    left_set, right_set = set(left), set(right)
    if not left_set or not right_set:
        return 0.0
    smaller = min(len(left_set), len(right_set))
    return len(left_set & right_set) / smaller


def sequence_ratio(left: str | None, right: str | None) -> float:
    """Character-level similarity of two short strings, 0..1.

    A local implementation of the longest-common-subsequence ratio rather than
    `difflib`, so the score is stable across Python versions — a stored duplicate
    score has to stay reproducible.
    """
    a, b = normalise(left), normalise(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    # Classic LCS over the shorter axis, two rows at a time.
    if len(a) < len(b):
        a, b = b, a
    previous = [0] * (len(b) + 1)
    for char_a in a:
        current = [0]
        for index, char_b in enumerate(b):
            if char_a == char_b:
                current.append(previous[index] + 1)
            else:
                current.append(max(previous[index + 1], current[index]))
        previous = current
    return (2.0 * previous[-1]) / (len(a) + len(b))


def name_similarity(left: str | None, right: str | None) -> float:
    """How alike two party names are, taking the kinder of token and character views."""
    left_norm, right_norm = normalise_organisation(left), normalise_organisation(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return max(
        token_containment(
            tokens(left_norm, drop_stopwords=False), tokens(right_norm, drop_stopwords=False)
        ),
        sequence_ratio(left_norm, right_norm),
    )


def text_similarity(left: str | None, right: str | None) -> float:
    """How alike two free-text descriptions are."""
    return jaccard(tokens(left), tokens(right))


#: Words that name a distinguishable part of one site, when an identifier follows.
#:
#: The identifier is required, which is what keeps this from firing on prose: "the
#: warehouse roof" names no particular warehouse and "the 2200 block of West
#: Carbondale Avenue" names no particular block, while "Building K" and "Unit 7" do.
_SITE_DESIGNATORS: tuple[str, ...] = (
    "building",
    "bldg",
    "block",
    "unit",
    "suite",
    "ste",
    "floor",
    "level",
    "apartment",
    "apt",
    "flat",
    "wing",
    "annex",
    "annexe",
    "phase",
    "stage",
    "bay",
    "plot",
    "lot",
    "tower",
    "house",
    "warehouse",
    "dock",
    "berth",
    "pad",
    "stand",
    "pier",
)

_DESIGNATOR_RE = re.compile(
    r"\b(?P<kind>"
    + "|".join(_SITE_DESIGNATORS)
    + r")\s*(?:no\.?|number|#)?\s*(?P<ident>[a-z]?[-/]?[0-9]{1,4}[a-z]?|[a-z])\b"
)

#: What a designator on one side and none on the other is worth. The same *site*,
#: an unknown part of it — a real signal, and not the "same location" one.
_DESIGNATOR_UNSTATED = 0.6
#: What two different designators of the same kind are worth. Building K and
#: Building C are two buildings, whatever the street they share.
_DESIGNATOR_CONTRADICTED = 0.15


def site_designators(value: str | None) -> dict[str, set[str]]:
    """`{kind: {identifier}}` for every named part of a site in an address."""
    found: dict[str, set[str]] = {}
    for match in _DESIGNATOR_RE.finditer(normalise(value)):
        found.setdefault(match["kind"], set()).add(match["ident"])
    return found


def designator_agreement(left: str | None, right: str | None) -> float:
    """How much two addresses agree about *which part* of a site the loss is in.

    A multiplier on the token score rather than a signal of its own, because it is
    not a separate fact — it is a correction to how much the token overlap is worth.

    The case this exists for: `windrow-grove-hail` reports "6120 East 91st Street,
    Tulsa, OK 74137" and `windrow-grove-building-k-fire` reports the same address
    "— Building K". Every token of the shorter one is in the longer one, so plain
    containment scored 1.0 and the duplicate check read "Same location" — on a pair
    of claims seven months and one peril apart whose fixture README says in as many
    words that they should not be confused. It scored 0.6292 against a 0.62
    threshold and landed as a review candidate.

    Containment is still the right primitive: "Unit 7, Wakefield Road, Leeds LS9"
    and "Leeds LS9" *are* the same site, and the shorter form is what a phone
    notification captures. What containment cannot say is that the shorter form
    left the question of which building open — so an address that names a part and
    one that does not are the same site with an unknown part, not the same place.
    """
    left_parts, right_parts = site_designators(left), site_designators(right)
    if not left_parts and not right_parts:
        return 1.0

    shared = set(left_parts) & set(right_parts)
    if any(left_parts[kind] != right_parts[kind] for kind in shared):
        return _DESIGNATOR_CONTRADICTED
    if left_parts == right_parts:
        return 1.0
    return _DESIGNATOR_UNSTATED


def location_similarity(left: str | None, right: str | None) -> float:
    """How alike two written addresses are, and how sure that they are one place.

    Token containment, corrected by whether the two sides agree about which part of
    the site the loss was in. See `designator_agreement` for why the correction is a
    multiplier and not a signal.
    """
    overlap = token_containment(tokens(left), tokens(right))
    if overlap <= 0.0:
        return overlap
    return overlap * designator_agreement(left, right)


EARTH_RADIUS_KM = 6371.0088


def haversine_km(
    lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None
) -> float | None:
    """Great-circle distance in kilometres, or `None` if either point is unknown."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    assert lat1 is not None and lon1 is not None and lat2 is not None and lon2 is not None

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def weighted_score(parts: Sequence[tuple[float, float]]) -> float:
    """Combine `(score, weight)` pairs, ignoring signals that could not be compared.

    Signals score `-1` when neither side had the data. Dropping them rather than
    scoring them zero is what keeps a duplicate check over two sparse records
    from being dragged to nothing by the fields neither of them filled in.
    """
    usable = [(score, weight) for score, weight in parts if score >= 0.0 and weight > 0.0]
    if not usable:
        return 0.0
    total_weight = sum(weight for _, weight in usable)
    return sum(score * weight for score, weight in usable) / total_weight
