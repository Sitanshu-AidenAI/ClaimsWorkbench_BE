"""When a Graph mail subscription needs renewing, recreating, or leaving alone.

Pure functions over values — no session, no HTTP, no clock beyond what is handed
in — the same shape as `lifecycle.py`, `claim_lifecycle.py` and `inspection.py`.

The whole module exists because a subscription to an Outlook mail resource
**cannot be made to last**. Graph's ceiling for that resource type is 4230
minutes, a little under three days, where drive items get thirty days and
directory objects twenty-nine. There is no configuration that removes the
renewal, so the renewal is the feature and these are its rules:

* **renew** while the subscription still exists and is merely running short;
* **recreate** when it has expired, or when the URL it was created against is no
  longer the URL we would be told on — Graph keeps posting to the address it was
  given, so a changed tunnel is a dead subscription that renews perfectly well;
* **leave alone** when there is time left, because a renewal is a network call
  and four of them a day is enough.

The distinction between renew and recreate is the one worth getting right. A
renewal of an expired subscription is a 404 from Graph, and a service that
retried it would sit in a loop reporting failures while the mailbox went
uncollected.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class SubscriptionAction(StrEnum):
    """What to do with the subscription we hold, if we hold one."""

    #: Nothing. There is time left on it and its address is still ours.
    LEAVE = "leave"
    #: Extend the expiry. The subscription exists and Graph will accept a PATCH.
    RENEW = "renew"
    #: Create a new one. Either it has lapsed, or it points somewhere we no
    #: longer listen, and both are past renewing.
    RECREATE = "recreate"


class Subscription(Protocol):
    """The stored record, as this module reads it."""

    subscription_id: str
    notification_url: str
    expires_at: datetime


def decide(
    subscription: Subscription | None,
    *,
    notification_url: str,
    now: datetime,
    renew_before: timedelta,
) -> SubscriptionAction:
    """What to do about the subscription for this mailbox.

    `None` — nothing stored — is `RECREATE` rather than an error: it is the state
    of a deployment that has just been switched on, and of one whose subscription
    was deleted at the far end.

    **The URL comparison is not a nicety.** A dev tunnel hands out a new hostname
    on every restart unless it is pinned, and Graph goes on posting notifications
    to the old one. Renewing that subscription succeeds, reports success, and
    collects nothing — the exact shape of failure this codebase keeps having to
    design against, so the address is checked before the clock.
    """
    if subscription is None:
        return SubscriptionAction.RECREATE

    if subscription.notification_url != notification_url:
        return SubscriptionAction.RECREATE

    #: Expired, not merely short. Graph answers a PATCH on a lapsed subscription
    #: with a 404, so renewing here would fail forever rather than recover.
    if subscription.expires_at <= now:
        return SubscriptionAction.RECREATE

    if subscription.expires_at - now <= renew_before:
        return SubscriptionAction.RENEW

    return SubscriptionAction.LEAVE


def expiry_for(now: datetime, *, lifetime_minutes: int) -> datetime:
    """The `expirationDateTime` to ask Graph for.

    The caller passes an already-clamped lifetime — `GraphSettings.
    subscription_lifetime_minutes` owns the ceiling, because that is a fact about
    the API and belongs beside the other facts about it.
    """
    return now + timedelta(minutes=max(1, lifetime_minutes))


def authentic(
    *,
    client_state: str | None,
    expected_client_state: str | None,
    subscription_id: str | None,
    known_subscription_ids: frozenset[str],
) -> bool:
    """Whether a notification is one of ours.

    The endpoint that receives these **cannot be authenticated** in the ordinary
    way: Graph has no bearer token to present, so the route is public and this
    function is the whole of its door.

    Two checks, and both are needed. `clientState` proves the caller knows a
    secret only Graph was told, which is what stops anybody who finds the URL from
    posting to it. The subscription id proves the notification belongs to a
    subscription *we created*, which is what stops a notification legitimately
    signed with a leaked secret from steering us at another tenant's mailbox.

    A missing expected secret returns `False` rather than admitting everything.
    Failing open on an unconfigured credential is how a public endpoint becomes an
    open one, and the caller's own `webhook_ready` already refuses to create a
    subscription without it.
    """
    if not expected_client_state:
        return False
    if client_state != expected_client_state:
        return False
    if not subscription_id:
        return False
    return subscription_id in known_subscription_ids


__all__ = [
    "Subscription",
    "SubscriptionAction",
    "authentic",
    "decide",
    "expiry_for",
]
