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

So the check here reads `mail_intake_runs` and nothing else. It touches neither
Graph nor Celery, which is what lets it run in the API process: a watchdog in the
same process as the thing it watches cannot report that thing being dead.

`evaluate` is a pure function of (latest run, settings, now). That is the whole
point of its shape — every state below is reachable in a unit test without a
mailbox, a broker or a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import GraphSettings, settings
from app.core.logging import get_logger
from app.domain.enums import MAIL_INTAKE_HEALTH_SEVERITY, MailIntakeHealth, MailIntakeTrigger
from app.models.mail_intake import MailIntakeRun
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


def stale_after_seconds(config: GraphSettings | None = None) -> int:
    """How long without a poll means the poller has stopped."""
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
    #: Whether beat should be scheduling polls at all, from configuration.
    poll_enabled: bool
    configured: bool
    poll_interval_seconds: int
    stale_after_seconds: int

    #: The most recent poll of any outcome, and how long ago it started.
    last_run_at: datetime | None = None
    last_run_age_seconds: float | None = None
    last_run_trigger: str | None = None
    last_run_ok: bool | None = None
    last_run_error: str | None = None
    #: The most recent poll that reached the mailbox. Behind `last_run_at` when
    #: polls are landing and failing.
    last_success_at: datetime | None = None
    last_success_age_seconds: float | None = None

    #: Carried from the last run so a caller sees the numbers behind a verdict.
    fetched: int | None = None
    ingested: int | None = None
    dropped: int | None = None
    folder_total: int | None = None
    folder_unread: int | None = None
    ledger_total: int | None = None
    sweep_blind: bool | None = None
    #: True when no run has ever been recorded by beat. A mailbox kept alive by
    #: someone pressing the trigger button is a broken mailbox with a busy human
    #: in front of it, and it must not read as healthy.
    scheduled_run_seen: bool = False

    @property
    def worst(self) -> MailIntakeHealth:
        return self.state


def evaluate(
    run: MailIntakeRun | None,
    *,
    last_success: MailIntakeRun | None = None,
    config: GraphSettings | None = None,
    now: datetime | None = None,
) -> MailIntakeHealthReport:
    """Grade intake from its last run. Pure: no IO, no clock unless given one."""
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
    )

    if not config.configured:
        return _verdict(
            base,
            MailIntakeHealth.NOT_CONFIGURED,
            "Mailbox intake has no Graph credentials, so no mail is collected on this "
            "deployment. Set CWB_GRAPH_TENANT_ID, CWB_GRAPH_CLIENT_ID, "
            "CWB_GRAPH_CLIENT_SECRET and CWB_GRAPH_SHARED_MAILBOX to switch it on.",
        )

    if not config.poll_enabled:
        return _verdict(
            base,
            MailIntakeHealth.DISABLED,
            "Mailbox intake is configured but scheduled polling is switched off, so mail "
            "arrives only when someone triggers a poll. Set CWB_GRAPH_POLL_ENABLED=true "
            "and restart the worker and beat.",
        )

    if run is not None:
        age = (now - _aware(run.started_at)).total_seconds()
        base.last_run_at = _aware(run.started_at)
        base.last_run_age_seconds = age
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
        base.scheduled_run_seen = run.trigger == MailIntakeTrigger.SCHEDULE

    if last_success is not None:
        base.last_success_at = _aware(last_success.started_at)
        base.last_success_age_seconds = (now - _aware(last_success.started_at)).total_seconds()

    if run is None:
        return _verdict(
            base,
            MailIntakeHealth.NEVER_RUN,
            "Mailbox intake is configured and enabled, and no poll has ever been "
            "recorded. Nothing is collecting mail. Start the Celery worker and beat "
            "(`make dev` runs the API, worker and beat together — an API alone runs no "
            "scheduled work), and confirm migration 0013 has been applied.",
        )

    age = base.last_run_age_seconds or 0.0

    # Staleness first: it outranks everything else because a poller that has
    # stopped makes every other number on the row a historical curiosity.
    if age > threshold:
        return _verdict(
            base,
            MailIntakeHealth.STALE,
            f"The last mailbox poll started {_human(age)} ago and polls are configured "
            f"every {config.poll_interval_seconds}s. Nothing is collecting mail now: the "
            "Celery beat scheduler, the worker, or both are not running. Start them with "
            "`make dev`, and note that an API process on its own runs no scheduled work.",
        )

    if not run.ok:
        return _verdict(
            base,
            MailIntakeHealth.FAILING,
            "Polls are running and the most recent one could not read the mailbox: "
            f"{run.error or 'no detail recorded'}. Mail is accumulating in the folder "
            "uncollected — check the Graph credentials, the client secret's expiry and "
            "the Mail.ReadWrite application permission.",
        )

    if run.dropped or run.sweep_blind:
        return _verdict(
            base,
            MailIntakeHealth.LOSING_MAIL,
            f"The last poll listed {run.fetched} messages and {run.dropped} of them left "
            "no ledger row"
            + (
                ", and the folder reports unread mail the sweep returned nothing for"
                if run.sweep_blind
                else ""
            )
            + ". Those messages are neither collected nor queued for retry — this is a "
            "silent loss and needs a human.",
        )

    if not base.scheduled_run_seen:
        # Deliberately not healthy. A mailbox that only moves when somebody
        # presses the button is the exact fault being hunted, wearing the
        # disguise that has hidden it every previous time.
        return _verdict(
            base,
            MailIntakeHealth.STALE,
            f"The only recent poll was triggered by hand ({run.trigger}), not by the "
            "scheduler. Mail is arriving because someone is pressing the button; "
            "nothing is collecting it automatically. Start the Celery worker and beat.",
        )

    return _verdict(
        base,
        MailIntakeHealth.OK,
        f"Collecting normally: last poll {_human(age)} ago fetched {run.fetched} "
        f"message(s) and created {run.ingested} notice(s).",
    )


def worst_of(*states: MailIntakeHealth) -> MailIntakeHealth:
    """The most severe of several states, by `MAIL_INTAKE_HEALTH_SEVERITY`."""
    return max(states, key=MAIL_INTAKE_HEALTH_SEVERITY.index)


class MailIntakeHealthService:
    """Reads the run ledger and grades it. No Graph client, deliberately.

    Building a `GraphMailClient` raises when credentials are absent, so a health
    check that needed one could not report the very state most worth reporting.
    This class takes a repository and nothing else.
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
        run = await self._messages.latest_run(mailbox)
        success = await self._messages.latest_successful_run(mailbox)
        return evaluate(run, last_success=success, config=self._config, now=now)


def _verdict(
    report: MailIntakeHealthReport, state: MailIntakeHealth, detail: str
) -> MailIntakeHealthReport:
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
