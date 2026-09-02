"""Is mailbox intake actually collecting? Answered from outside the poller.

Every other check on mail intake lives inside a poll. `_reconcile` counts listed
messages against ledger rows; `sweep_blind` catches a folder holding unread mail
a sweep returned nothing for; `graph_message_list_truncated` warns when a page
was left behind. All of them are good checks and all of them share one blind
spot: they need a poll to be running in order to say anything. When the scheduler
is gone every one of them is silent, and the evidence a dead poller leaves is
identical to the evidence an empty mailbox leaves — no new rows, no errors, a
`/health` that says ok.

That is the failure this module exists for, and it is not theoretical. Messages
that arrived on 21 and 24 August were collected on the 27th by a poll someone ran
by hand: up to five days and twenty-two hours, with nothing anywhere reporting a
problem. Twice before that, the same shape of fault — a stale `unread_only` flag
in a running worker — produced 5,272 consecutive clean polls over mail it could
not see.

**Intake now has two ways in, and this module grades them separately.** A change
notification is the mailbox telling us; a scheduled sweep is us asking. The
temptation is to ask one question — "has mail arrived recently" — and the reason
that is wrong is that the two paths *hide each other*: a live subscription keeps
mail flowing while beat is dead, and a running sweep keeps mail flowing while the
subscription has lapsed. Both look healthy from the desk. So each path is graded
against its own evidence — the sweep against its own ledger rows, the subscription
against its expiry — and the verdict names which one went. That is what
`MailIntakeHealth.DEGRADED` is: the redundancy reporting it has been spent.

Two consequences worth stating plainly:

* A webhook run must never count as evidence that the *sweep* is alive. Both
  write to `mail_intake_runs`, so "the last run" is not a per-path answer;
  `latest_run_by_trigger` is.
* A stale `.env` makes this whole verdict untrustworthy, because the staleness
  threshold is derived from configuration. A process running superseded settings
  will report a fault that exists only in its own arithmetic — which is exactly
  what happened on 2 September, when an API started four minutes before the poll
  interval was changed from 8s to 600s and then announced a dead scheduler once
  an hour against a scheduler that was working perfectly. So drift is reported
  *first*, ahead of the verdict it undermines.

The check reads `mail_intake_runs` and `mail_subscriptions` and nothing else. It
touches neither Graph nor Celery, which is what lets it run in the API process: a
watchdog in the same process as the thing it watches cannot report that thing
being dead.

`evaluate` is a pure function of (rows, settings, now). That is the whole point of
its shape — every state below is reachable in a unit test without a mailbox, a
broker or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import GraphSettings, env_file_changed_since_load, settings
from app.core.logging import get_logger
from app.domain.enums import MAIL_INTAKE_HEALTH_SEVERITY, MailIntakeHealth, MailIntakeTrigger
from app.models.mail_intake import MailIntakeRun, MailSubscription
from app.repositories.mail_intake import MailIntakeRepository

logger = get_logger(__name__)

#: Floor under the staleness threshold, whatever the poll interval is. An eight
#: second interval must not raise the alarm because a worker took twenty seconds
#: to restart, and a threshold derived purely as a multiple of the interval would.
MINIMUM_STALE_AFTER_SECONDS = 180

#: How many intervals of silence count as stopped. Four rather than two so an
#: ordinary redeploy, a long batch or one retried poll never trips it: this alarm
#: has to be believed, and an alarm that cries wolf gets muted and then the
#: original bug comes back wearing the mute as camouflage.
STALE_INTERVAL_MULTIPLE = 4

#: Triggers that mean a person did this. Called out in the verdict because a
#: mailbox kept alive by somebody pressing a button is a broken mailbox with a
#: busy human in front of it, and the sentence has to say so or the human keeps
#: pressing the button.
_BY_HAND = frozenset({MailIntakeTrigger.MANUAL, MailIntakeTrigger.CLI})


def stale_after_seconds(config: GraphSettings | None = None) -> int:
    """How long without a scheduled sweep means the sweep has stopped."""
    config = config or settings.graph
    return max(
        MINIMUM_STALE_AFTER_SECONDS,
        config.poll_interval_seconds * STALE_INTERVAL_MULTIPLE,
    )


@dataclass(slots=True)
class MailIntakeHealthReport:
    """The verdict, and everything needed to explain it to a person.

    `detail` is written for an operator reading it at the moment something is
    wrong, so it says what is true, what it means and what to do — not a status
    code they then have to look up.
    """

    state: MailIntakeHealth
    healthy: bool
    detail: str
    mailbox: str | None
    #: Whether beat should be scheduling sweeps at all, from configuration.
    poll_enabled: bool
    configured: bool
    poll_interval_seconds: int
    stale_after_seconds: int
    #: Whether a change-notification subscription is configured on this
    #: deployment. False is the ordinary state of a machine with no public URL,
    #: and is not a fault.
    webhook_ready: bool = False

    #: The most recent run of any kind, and how long ago it started. Diagnostic
    #: only: it is deliberately *not* what either path is graded on, because both
    #: paths write here and each would mask the other's silence.
    last_run_at: datetime | None = None
    last_run_age_seconds: float | None = None
    last_run_trigger: str | None = None
    last_run_ok: bool | None = None
    last_run_error: str | None = None
    #: The most recent run that reached the mailbox. Behind `last_run_at` when
    #: runs are landing and failing.
    last_success_at: datetime | None = None
    last_success_age_seconds: float | None = None

    # -- The sweep limb ------------------------------------------------------
    #: Whether the scheduled sweep is running on time. This is the one graded
    #: against `stale_after_seconds`.
    sweep_collecting: bool = False
    last_sweep_at: datetime | None = None
    last_sweep_age_seconds: float | None = None

    # -- The notification limb -----------------------------------------------
    #: Whether a live Graph subscription exists, so the mailbox can push to us.
    webhook_collecting: bool = False
    subscription_id: str | None = None
    subscription_expires_at: datetime | None = None
    #: Negative once it has lapsed, which is why it is a signed number rather
    #: than a "seconds remaining" that would bottom out at zero and lose the
    #: single most useful fact about a dead subscription: how long it has been
    #: dead.
    subscription_expires_in_seconds: float | None = None
    subscription_renewal_count: int | None = None
    #: Last notification actually collected. Not what the limb is graded on — a
    #: quiet mailbox produces no notifications and that is not a fault — but it
    #: is the figure that shows the push path has ever worked.
    last_webhook_at: datetime | None = None
    last_webhook_age_seconds: float | None = None

    #: Set when `.env` was edited after this process loaded its settings, which
    #: makes every threshold above the old one. See the module docstring.
    config_stale_seconds: float | None = None

    #: Carried from the last run so a caller sees the numbers behind a verdict.
    fetched: int | None = None
    ingested: int | None = None
    dropped: int | None = None
    folder_total: int | None = None
    folder_unread: int | None = None
    ledger_total: int | None = None
    sweep_blind: bool | None = None
    #: Whether beat has ever recorded a sweep. A mailbox kept alive by someone
    #: pressing the trigger button is a broken mailbox with a busy human in front
    #: of it, and it must not read as healthy.
    scheduled_run_seen: bool = False

    @property
    def worst(self) -> MailIntakeHealth:
        return self.state


def evaluate(
    run: MailIntakeRun | None,
    *,
    last_success: MailIntakeRun | None = None,
    sweep: MailIntakeRun | None = None,
    webhook: MailIntakeRun | None = None,
    subscription: MailSubscription | None = None,
    config_stale_seconds: float | None = None,
    config: GraphSettings | None = None,
    now: datetime | None = None,
) -> MailIntakeHealthReport:
    """Grade intake from its ledger and its subscription. Pure: no IO, no clock.

    `run` is the newest row of any kind and supplies the outcome figures.
    `sweep` and `webhook` are the newest row *of each trigger*, and are what the
    two limbs are graded on — see the module docstring for why that distinction
    is the whole point.
    """
    config = config or settings.graph
    now = now or datetime.now(UTC)
    threshold = stale_after_seconds(config)

    base = MailIntakeHealthReport(
        state=MailIntakeHealth.OK,
        healthy=True,
        detail="",
        mailbox=config.shared_mailbox,
        poll_enabled=config.poll_enabled,
        configured=config.configured,
        poll_interval_seconds=config.poll_interval_seconds,
        stale_after_seconds=threshold,
        webhook_ready=config.webhook_ready,
        config_stale_seconds=config_stale_seconds,
    )

    if not config.configured:
        return _verdict(
            base,
            MailIntakeHealth.NOT_CONFIGURED,
            "Mailbox intake has no Graph credentials, so no mail is collected on this "
            "deployment. Set CWB_GRAPH_TENANT_ID, CWB_GRAPH_CLIENT_ID, "
            "CWB_GRAPH_CLIENT_SECRET and CWB_GRAPH_SHARED_MAILBOX to switch it on.",
        )

    if run is not None:
        base.last_run_at = _aware(run.started_at)
        base.last_run_age_seconds = (now - base.last_run_at).total_seconds()
        base.last_run_trigger = run.trigger
        base.last_run_ok = run.ok
        base.last_run_error = run.error
        base.fetched = run.fetched
        base.ingested = run.ingested
        base.dropped = run.dropped
        base.folder_total = run.folder_total
        base.folder_unread = run.folder_unread
        base.ledger_total = run.ledger_total
        base.sweep_blind = run.sweep_blind

    if last_success is not None:
        base.last_success_at = _aware(last_success.started_at)
        base.last_success_age_seconds = (now - base.last_success_at).total_seconds()

    if sweep is not None:
        base.last_sweep_at = _aware(sweep.started_at)
        base.last_sweep_age_seconds = (now - base.last_sweep_at).total_seconds()
        base.scheduled_run_seen = True

    if webhook is not None:
        base.last_webhook_at = _aware(webhook.started_at)
        base.last_webhook_age_seconds = (now - base.last_webhook_at).total_seconds()

    if subscription is not None:
        base.subscription_id = subscription.subscription_id
        base.subscription_expires_at = _aware(subscription.expires_at)
        base.subscription_expires_in_seconds = (base.subscription_expires_at - now).total_seconds()
        base.subscription_renewal_count = subscription.renewal_count

    # The two limbs. Each is graded on its own evidence, and each is only
    # *expected* where it is configured — a deployment running on notifications
    # alone has no dead scheduler, it has no scheduler.
    base.sweep_collecting = (
        config.poll_enabled
        and base.last_sweep_age_seconds is not None
        and base.last_sweep_age_seconds <= threshold
    )
    base.webhook_collecting = (
        config.webhook_ready
        and base.subscription_expires_in_seconds is not None
        and base.subscription_expires_in_seconds > 0
    )
    sweep_down = config.poll_enabled and not base.sweep_collecting
    webhook_down = config.webhook_ready and not base.webhook_collecting

    if not config.poll_enabled and not config.webhook_ready:
        return _verdict(
            base,
            MailIntakeHealth.DISABLED,
            "Mailbox intake is configured and every way in is switched off, so mail "
            "arrives only when someone triggers a poll by hand. Set "
            "CWB_GRAPH_POLL_ENABLED=true for the scheduled sweep, or configure "
            "CWB_GRAPH_WEBHOOK_NOTIFICATION_URL and CWB_GRAPH_WEBHOOK_CLIENT_STATE for "
            "change notifications, then restart the API, worker and beat.",
        )

    if run is None:
        return _verdict(
            base,
            MailIntakeHealth.NEVER_RUN,
            "Mailbox intake is configured and enabled, and nothing has ever collected "
            "from it. Start the Celery worker and beat (`make dev` runs the API, worker "
            "and beat together — an API alone runs no scheduled work), and confirm "
            "migration 0013 has been applied.",
        )

    # Nothing collecting outranks everything else: when neither way in works,
    # every other number on the row is a historical curiosity.
    if not base.sweep_collecting and not base.webhook_collecting:
        return _verdict(
            base,
            MailIntakeHealth.STALE,
            "Nothing is collecting mail. "
            + _sweep_clause(base, config)
            + " "
            + _webhook_clause(base, config)
            + " Mail is accumulating in the folder unread.",
        )

    if not run.ok:
        return _verdict(
            base,
            MailIntakeHealth.FAILING,
            "Intake is running and the most recent attempt could not read the mailbox: "
            f"{run.error or 'no detail recorded'}. Mail is accumulating in the folder "
            "uncollected — check the Graph credentials, the client secret's expiry and "
            "the Mail.ReadWrite application permission.",
        )

    if run.dropped or run.sweep_blind:
        return _verdict(
            base,
            MailIntakeHealth.LOSING_MAIL,
            f"The last run listed {run.fetched} messages and {run.dropped} of them left "
            "no ledger row"
            + (
                ", and the folder reports unread mail the sweep returned nothing for"
                if run.sweep_blind
                else ""
            )
            + ". Those messages are neither collected nor queued for retry — this is a "
            "silent loss and needs a human.",
        )

    if webhook_down:
        # Mail is still arriving, by sweep. Say what has actually been lost:
        # latency, and the fallback.
        return _verdict(
            base,
            MailIntakeHealth.DEGRADED,
            "Mail is still being collected by the scheduled sweep, but change "
            f"notifications have stopped. {_webhook_clause(base, config)} Notices now "
            f"wait up to {config.poll_interval_seconds}s instead of arriving within "
            "seconds, and the sweep is the only way in left. Renewal is attempted every "
            f"{config.renewal_interval_seconds}s — check the worker log for "
            "`mail_subscription_renewal_failed`.",
        )

    if sweep_down:
        # Mail is still arriving, by notification. The loss here is not latency,
        # it is the reconciliation that makes notifications safe to rely on.
        return _verdict(
            base,
            MailIntakeHealth.DEGRADED,
            "Mail is still arriving by change notification, but the scheduled sweep has "
            f"stopped. {_sweep_clause(base, config)} The sweep is what recovers messages "
            "Graph never notifies us about — Microsoft does not guarantee notification "
            "delivery — so until it is running again a missed notification is a lost "
            "notice, and nothing will report it.",
        )

    return _verdict(base, MailIntakeHealth.OK, _healthy_detail(base, config))


def worst_of(*states: MailIntakeHealth) -> MailIntakeHealth:
    """The most severe of several states, by `MAIL_INTAKE_HEALTH_SEVERITY`."""
    return max(states, key=MAIL_INTAKE_HEALTH_SEVERITY.index)


class MailIntakeHealthService:
    """Reads the run ledger and the subscription, and grades them.

    No Graph client, deliberately. Building a `GraphMailClient` raises when
    credentials are absent, so a health check that needed one could not report the
    very state most worth reporting. This class takes a repository and nothing
    else.
    """

    def __init__(
        self,
        messages: MailIntakeRepository,
        *,
        config: GraphSettings | None = None,
    ) -> None:
        self._messages = messages
        self._config = config or settings.graph

    async def report(self, *, now: datetime | None = None) -> MailIntakeHealthReport:
        # Narrowed to the configured mailbox, always. A deployment repointed at a
        # second address would otherwise be graded on the first one's runs and
        # call a scheduler healthy that has never polled the inbox anybody is
        # waiting on — the same class of false reassurance this module exists to
        # remove. `never_run` is the honest verdict for an unpolled address.
        mailbox = self._config.shared_mailbox
        return evaluate(
            await self._messages.latest_run(mailbox),
            last_success=await self._messages.latest_successful_run(mailbox),
            sweep=await self._messages.latest_run_by_trigger(MailIntakeTrigger.SCHEDULE, mailbox),
            webhook=await self._messages.latest_run_by_trigger(MailIntakeTrigger.WEBHOOK, mailbox),
            subscription=await self._messages.newest_subscription(mailbox),
            #: Read here rather than inside `evaluate` so that stays pure. It is
            #: a `stat` on one file, on a check that runs once a minute at most.
            config_stale_seconds=env_file_changed_since_load(),
            config=self._config,
            now=now,
        )


# ---------------------------------------------------------------------------
# Sentences
# ---------------------------------------------------------------------------


def _sweep_clause(report: MailIntakeHealthReport, config: GraphSettings) -> str:
    """Why the scheduled sweep is not collecting, and what to do about it."""
    if not config.poll_enabled:
        return "The scheduled sweep is switched off (CWB_GRAPH_POLL_ENABLED=false)."
    if report.last_sweep_at is None:
        if report.last_run_trigger in _BY_HAND:
            #: Worth naming explicitly, because this is the disguise the fault
            #: has worn every previous time it happened: somebody notices no mail
            #: arrived, presses the trigger button, mail appears, and nobody ever
            #: diagnoses the dead scheduler underneath.
            return (
                "No scheduled sweep has ever run — mail is moving only because it was "
                f"triggered by hand ({report.last_run_trigger}), which is what keeps this "
                "fault hidden. Start the Celery worker and beat with `make dev`; an API "
                "process on its own runs no scheduled work."
            )
        return (
            "No scheduled sweep has ever run: start the Celery worker and beat with "
            "`make dev` — an API process on its own runs no scheduled work."
        )
    return (
        f"The last scheduled sweep started {_human(report.last_sweep_age_seconds or 0)} ago "
        f"and sweeps are configured every {config.poll_interval_seconds}s, so the Celery "
        "beat scheduler, the worker, or both have stopped — start them with `make dev`."
    )


def _webhook_clause(report: MailIntakeHealthReport, config: GraphSettings) -> str:
    """Why the mailbox is not notifying us, and what to do about it."""
    if not config.webhook_ready:
        return (
            "No change-notification subscription is configured "
            "(CWB_GRAPH_WEBHOOK_NOTIFICATION_URL and CWB_GRAPH_WEBHOOK_CLIENT_STATE), so "
            "there is no push path either."
        )
    if report.subscription_expires_at is None:
        return (
            "No Graph subscription has been created, so the mailbox has nothing to notify. "
            "It is created by the `renew_mail_subscription` task, which needs the worker "
            "and beat running."
        )
    lapsed = -(report.subscription_expires_in_seconds or 0)
    return (
        f"The Graph subscription {report.subscription_id} lapsed {_human(lapsed)} ago and "
        "was not renewed — Graph caps a mail subscription at 4230 minutes, so one that is "
        "not renewed dies inside three days."
    )


def _healthy_detail(report: MailIntakeHealthReport, config: GraphSettings) -> str:
    """What "collecting normally" means on *this* deployment.

    Names both limbs rather than quoting the last run, because "last poll 3
    minutes ago" is the sentence that made a dead subscription look well.
    """
    parts: list[str] = []
    if report.webhook_collecting:
        renewals = report.subscription_renewal_count or 0
        parts.append(
            "change notifications are live (subscription expires in "
            f"{_human(report.subscription_expires_in_seconds or 0)}, renewed {renewals} "
            f"time{'s' if renewals != 1 else ''})"
        )
    if report.sweep_collecting:
        parts.append(f"the scheduled sweep ran {_human(report.last_sweep_age_seconds or 0)} ago")
    elif not config.poll_enabled:
        parts.append("the scheduled sweep is switched off")

    collected = (
        f" Last run fetched {report.fetched} message(s) and created {report.ingested} notice(s)."
        if report.fetched is not None
        else ""
    )
    return "Collecting normally: " + " and ".join(parts) + "." + collected


def _verdict(
    report: MailIntakeHealthReport, state: MailIntakeHealth, detail: str
) -> MailIntakeHealthReport:
    """Record the verdict, prefixing the caveat that can invalidate it.

    Configuration drift comes first and only on a fault, because it is the one
    thing that makes the sentence after it untrustworthy: thresholds are derived
    from settings, so a process holding superseded settings can manufacture a
    fault out of nothing but its own arithmetic. Saying it *after* the fault
    would be read as a footnote to a real problem rather than as the reason to
    doubt it. On a healthy verdict it is left out — the drift is still on the
    report for the endpoint and the log, and leading a good-news line with a
    warning is how alarms get tuned out.
    """
    if state != MailIntakeHealth.OK and report.config_stale_seconds is not None:
        detail = (
            "`.env` has been edited since this API process loaded its settings, so the "
            "thresholds behind this verdict are the superseded ones and the fault below "
            "may be an artefact of them. Restart the API, then re-read this. — " + detail
        )
    report.state = state
    report.healthy = state == MailIntakeHealth.OK
    report.detail = detail
    return report


def _aware(value: datetime) -> datetime:
    """Postgres hands these back aware; a SQLite-backed test may not."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _human(seconds: float) -> str:
    """ "6 days", "22 hours", "3 minutes" — the unit an operator would use."""
    delta = timedelta(seconds=max(seconds, 0))
    days = delta.days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = int(delta.total_seconds() // 3600)
    if hours >= 1:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = int(delta.total_seconds() // 60)
    if minutes >= 1:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{int(delta.total_seconds())} second{'s' if int(delta.total_seconds()) != 1 else ''}"


__all__ = [
    "MailIntakeHealthReport",
    "MailIntakeHealthService",
    "evaluate",
    "stale_after_seconds",
    "worst_of",
]
