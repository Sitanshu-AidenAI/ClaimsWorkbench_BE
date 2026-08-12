"""Attributing a loss to a catastrophe event.

Three dimensions, all of which have to agree: the loss happened while the event
was running, near where it happened, and was caused by the kind of thing the
event is. A flood claim in Leeds during a Yorkshire flood event is a match; a
theft claim in Leeds during the same event is not, and a rule that matched on
place and date alone would attribute it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.core.config import FNOLSettings
from app.domain.matching import haversine_km, normalise, tokens

#: Peril words that identify an event type in a loss description.
EVENT_PERIL_TERMS: dict[str, tuple[str, ...]] = {
    "flood": ("flood", "flooding", "inundation", "water ingress", "river burst", "surface water"),
    "storm": ("storm", "gale", "high winds", "wind damage", "tempest"),
    "cyclone": ("cyclone", "typhoon", "tropical storm"),
    "hurricane": ("hurricane",),
    "earthquake": ("earthquake", "seismic", "tremor"),
    "wildfire": ("wildfire", "bushfire", "brush fire", "forest fire"),
    "hail": ("hail", "hailstone"),
    "freeze": ("freeze", "frozen pipe", "burst pipe", "cold snap"),
    "industrial": ("explosion", "industrial incident", "plant failure", "chemical release"),
}


#: The peril score when the notice has not said what caused the loss. Neutral:
#: it neither supports nor rules out an attribution.
_PERIL_UNSTATED = 0.35
#: The peril score when the notice states a cause that is not this event's.
_PERIL_MISMATCH = 0.15


@dataclass(slots=True)
class CatMatch:
    event_id: Any
    reference: str
    name: str
    confidence: float
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "reference": self.reference,
            "name": self.name,
            "confidence": round(self.confidence, 4),
            "reasons": self.reasons,
        }


def score_event(case: Any, event: Any, *, config: FNOLSettings) -> CatMatch | None:
    """Score one event against the loss, or `None` when the date rules it out."""
    loss_date = _as_date(getattr(case, "date_of_loss", None))
    if loss_date is None:
        return None

    margin = timedelta(days=config.cat_date_tolerance_days)
    if not (event.start_date - margin <= loss_date <= event.end_date + margin):
        return None

    reasons: list[str] = []
    inside_window = event.start_date <= loss_date <= event.end_date
    date_score = 1.0 if inside_window else 0.6
    reasons.append(
        f"Loss dated {loss_date:%d %b %Y}"
        + (
            f" falls inside the event window ({event.start_date:%d %b} – {event.end_date:%d %b})."
            if inside_window
            else f" is within {config.cat_date_tolerance_days} days of the event window."
        )
    )

    place_score, place_reason = _place_score(case, event, config=config)
    if place_reason:
        reasons.append(place_reason)

    peril_score, peril_reason = _peril_score(case, event)
    if peril_reason:
        reasons.append(peril_reason)

    # Place and peril both gate rather than merely contribute. A loss in the
    # wrong place is not this event whatever the date says; and a notice that
    # states its cause and states something other than this event's peril — a
    # theft during a flood week — is not this event either. Only a notice that
    # has not said what caused the loss stays open to attribution on date and
    # place alone.
    if place_score <= 0.0 or peril_score <= _PERIL_MISMATCH:
        return None

    confidence = date_score * 0.3 + place_score * 0.35 + peril_score * 0.35
    if confidence < config.cat_match_threshold:
        return None

    return CatMatch(
        event_id=event.id,
        reference=event.reference,
        name=event.name,
        confidence=round(confidence, 4),
        reasons=reasons,
    )


def best_match(case: Any, events: list[Any], *, config: FNOLSettings) -> CatMatch | None:
    matches = [
        match for match in (score_event(case, event, config=config) for event in events) if match
    ]
    if not matches:
        return None
    return max(matches, key=lambda match: match.confidence)


def rank_matches(case: Any, events: list[Any], *, config: FNOLSettings) -> list[CatMatch]:
    matches = [
        match for match in (score_event(case, event, config=config) for event in events) if match
    ]
    matches.sort(key=lambda match: match.confidence, reverse=True)
    return matches


def _place_score(case: Any, event: Any, *, config: FNOLSettings) -> tuple[float, str]:
    distance = haversine_km(
        _as_float(getattr(case, "loss_latitude", None)),
        _as_float(getattr(case, "loss_longitude", None)),
        _as_float(event.latitude),
        _as_float(event.longitude),
    )
    if distance is not None:
        radius = float(event.radius_km or config.cat_radius_km)
        if distance <= radius:
            return (
                1.0,
                f"The loss is {distance:.0f} km from the event centre (radius {radius:.0f} km).",
            )
        if distance <= radius * 1.5:
            return (
                0.5,
                f"The loss is {distance:.0f} km from the event centre, just outside its radius.",
            )
        return 0.0, ""

    location = normalise(getattr(case, "loss_location", None) or "")
    country = normalise(getattr(case, "loss_country", None) or "")
    if not location and not country:
        return 0.0, ""

    areas = [normalise(area) for area in (event.affected_areas or [])]
    location_tokens = tokens(f"{location} {country}")
    for area in areas:
        if area and (area in location or tokens(area) & location_tokens):
            return 1.0, f"The loss location falls in the affected area “{area}”."

    if event.region and tokens(event.region) & location_tokens:
        return 0.8, f"The loss location falls in the affected region “{event.region}”."
    if event.country and normalise(event.country) == country:
        return 0.45, f"The loss is in {event.country}, where the event occurred."
    return 0.0, ""


def _peril_score(case: Any, event: Any) -> tuple[float, str]:
    haystack = normalise(
        " ".join(
            part
            for part in (
                getattr(case, "cause_of_loss", None),
                getattr(case, "loss_type", None),
                getattr(case, "loss_description", None),
            )
            if part
        )
    )
    if not haystack:
        return _PERIL_UNSTATED, ""

    candidates = {normalise(peril) for peril in (event.perils or [])}
    candidates.add(normalise(event.event_type))
    for peril in candidates:
        terms = EVENT_PERIL_TERMS.get(peril, (peril,))
        hit = next((term for term in terms if term and term in haystack), None)
        if hit:
            return 1.0, f"The loss describes “{hit}”, which matches the event type."

    return _PERIL_MISMATCH, "The reported cause does not match the event type."


def _as_date(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
