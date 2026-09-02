"""The check that answers "is mail actually being collected" from outside the poller.

Every one of these tests is about a failure that used to be invisible. The bug
they guard against is not "intake is broken" — intake works, and the rest of
`test_mail_intake.py` proves it. The bug is that when intake *stops*, nothing
anywhere says so: a dead scheduler and an empty mailbox produce identical
evidence, which is how messages sat uncollected for five days on a deployment
whose API reported healthy the whole time.

So the assertions here are mostly about states, not mechanics. `evaluate` is pure
— a function of (rows, settings, now) — precisely so that six days of silence is
a one-line test instead of a six-day one.

**`evaluate` grades two limbs, and the tests must feed both.** Intake has two
independent ways in — a scheduled sweep and a Graph change notification — and
they write to the same ledger, so "the newest run" cannot tell you which one is
alive. Tests therefore pass `sweep=` and `subscription=` rather than relying on
the newest run to stand for everything; `grade` below does that routing the way
the repository does, so a test body still reads as one sentence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import GraphSettings
from app.domain.enums import MailIntakeHealth, MailIntakeTrigger
from app.models.mail_intake import MailIntakeRun, MailSubscription
from app.services.mail.health import (
    MINIMUM_STALE_AFTER_SECONDS,
    STALE_INTERVAL_MULTIPLE,
    MailIntakeHealthReport,
    MailIntakeHealthService,
    evaluate,
    stale_after_seconds,
    worst_of,
)

MAILBOX = "claims@carrier.test"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def configured(**overrides: object) -> GraphSettings:
    """Settings for a mailbox that is set up and sweeping, with **no webhook**.

    Every field that matters is pinned, including the ones being turned *off*,
    for the reason `unconfigured` below spells out: the suite runs against the
    developer's own `.env`, and `GraphSettings` fills anything unset from it. The
    webhook fields are the live example — a machine with a dev tunnel configured
    would otherwise hand every test in this file `webhook_ready=True` and a
    subscription it never created, turning each sweep assertion into an assertion
    about notifications as well. Tests that want the webhook ask for it by name,
    via `with_webhook`.
    """
    values: dict[str, object] = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "shared_mailbox": MAILBOX,
        "poll_enabled": True,
        "poll_interval_seconds": 300,
        "webhook_enabled": False,
        "notification_url": None,
        "lifecycle_url": None,
        "webhook_client_state": None,
    }
    values.update(overrides)
    return GraphSettings(**values)  # type: ignore[arg-type]


def with_webhook(**overrides: object) -> GraphSettings:
    """Settings for a mailbox that also has change notifications set up.

    Kept separate from `configured` deliberately. Most of the tests below are
    about the sweep, and giving them a subscription too would mean every sweep
    assertion was also asserting something about the webhook — so `configured`
    stays webhook-free and the notification tests opt in.
    """
    return configured(
        webhook_enabled=True,
        notification_url="https://tunnel.test/api/v1/mail-intake/notifications",
        webhook_client_state="a-secret-only-graph-was-told",
        **overrides,
    )


def a_subscription(*, expires_in: timedelta, renewals: int = 2) -> MailSubscription:
    return MailSubscription(
        id=uuid.uuid4(),
        subscription_id="sub-0001",
        mailbox=MAILBOX,
        resource=f"users/{MAILBOX}/mailFolders('inbox')/messages",
        notification_url="https://tunnel.test/api/v1/mail-intake/notifications",
        expires_at=NOW + expires_in,
        renewed_at=NOW - timedelta(hours=1),
        renewal_count=renewals,
    )


def grade(
    run: MailIntakeRun | None,
    *,
    sweep: MailIntakeRun | None = None,
    config: GraphSettings | None = None,
    now: datetime = NOW,
    **kwargs: object,
) -> MailIntakeHealthReport:
    """`evaluate`, with the sweep limb derived from the run the way the repository is.

    The repository answers "the newest run" and "the newest run *of this
    trigger*" from the same table. This mirrors the second for the common case —
    a test whose single run is a scheduled one — so that the tests read the way
    they did before the limbs were separated, while still going through the
    explicit parameter rather than around it.

    A run that is *not* a scheduled sweep is deliberately not routed here. That
    is the whole point: a manual poll or a change notification must never be
    evidence that beat is alive.
    """
    if sweep is None and run is not None and run.trigger == MailIntakeTrigger.SCHEDULE:
        sweep = run
    return evaluate(
        run,
        sweep=sweep,
        config=config or configured(),
        now=now,
        **kwargs,  # type: ignore[arg-type]
    )


def unconfigured() -> GraphSettings:
    """Settings with no credentials at all.

    Written out field by field rather than as `GraphSettings()`: the test suite
    runs against the developer's own `.env`, and a bare constructor picks up the
    real tenant — which made this test assert the opposite of what it says.
    """
    return GraphSettings(
        tenant_id=None,
        client_id=None,
        client_secret=None,
        shared_mailbox=None,
    )


def a_run(
    *,
    started_at: datetime | None = None,
    trigger: str = MailIntakeTrigger.SCHEDULE,
    ok: bool = True,
    error: str | None = None,
    fetched: int = 3,
    ingested: int = 1,
    dropped: int = 0,
    sweep_blind: bool = False,
) -> MailIntakeRun:
    return MailIntakeRun(
        id=uuid.uuid4(),
        mailbox=MAILBOX,
        trigger=trigger,
        started_at=started_at or NOW - timedelta(seconds=30),
        finished_at=started_at or NOW - timedelta(seconds=29),
        swept_since=NOW - timedelta(days=1),
        fetched=fetched,
        ingested=ingested,
        duplicates=0,
        failed=0,
        abandoned=0,
        dropped=dropped,
        folder_total=12,
        folder_unread=0,
        ledger_total=12,
        sweep_blind=sweep_blind,
        ok=ok,
        error=error,
    )


class TestTheStalenessThreshold:
    def test_it_is_a_multiple_of_the_poll_interval(self) -> None:
        config = configured(poll_interval_seconds=600)
        assert stale_after_seconds(config) == 600 * STALE_INTERVAL_MULTIPLE

    def test_a_very_short_interval_does_not_produce_a_hair_trigger(self) -> None:
        """The eight-second interval this deployment runs must not alarm on a restart.

        Four times eight seconds is 32 seconds, and a worker takes longer than
        that to come back after a code change. An alarm that fires on every
        restart gets muted, and a muted alarm is worse than none — the original
        bug returns wearing the mute as camouflage.
        """
        config = configured(poll_interval_seconds=8)
        assert stale_after_seconds(config) == MINIMUM_STALE_AFTER_SECONDS


class TestWhatIsNotAFault:
    def test_a_deployment_without_credentials_is_not_configured_rather_than_broken(
        self,
    ) -> None:
        report = grade(None, config=unconfigured(), now=NOW)
        assert report.state == MailIntakeHealth.NOT_CONFIGURED
        assert not report.healthy
        # The sentence has to send someone to the right place, not to a log.
        assert "CWB_GRAPH_TENANT_ID" in report.detail

    def test_polling_switched_off_says_so_rather_than_looking_dead(self) -> None:
        report = grade(None, config=configured(poll_enabled=False), now=NOW)
        assert report.state == MailIntakeHealth.DISABLED
        assert "CWB_GRAPH_POLL_ENABLED" in report.detail

    def test_a_recent_scheduled_poll_is_healthy(self) -> None:
        report = grade(a_run(), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK
        assert report.healthy
        assert report.scheduled_run_seen

    def test_a_quiet_mailbox_is_still_healthy(self) -> None:
        """Fetching nothing is the normal state of a working mailbox.

        This is the test that keeps the check honest: the whole failure being
        guarded against looks exactly like this from inside a poll, and the
        difference is *when the poll ran*, never what it found.
        """
        report = grade(a_run(fetched=0, ingested=0), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK


class TestTheFailureThatUsedToBeInvisible:
    def test_no_poll_ever_recorded_is_reported_as_never_run(self) -> None:
        report = grade(None, config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.NEVER_RUN
        assert not report.healthy
        # It must name the actual remedy. "Start the worker" is the whole fix and
        # it is not guessable from a status code.
        assert "worker" in report.detail.lower()
        assert "beat" in report.detail.lower()

    def test_silence_longer_than_the_threshold_is_stale(self) -> None:
        config = configured(poll_interval_seconds=300)
        report = grade(a_run(started_at=NOW - timedelta(seconds=1201)), config=config, now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy

    def test_six_days_of_silence_is_stale_and_says_so_in_days(self) -> None:
        """The exact outage on this deployment, as a one-line test."""
        report = grade(a_run(started_at=NOW - timedelta(days=6)), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert "6 days" in report.detail
        assert report.last_run_age_seconds == pytest.approx(6 * 86400)

    def test_silence_just_inside_the_threshold_is_not_stale(self) -> None:
        config = configured(poll_interval_seconds=300)
        report = grade(a_run(started_at=NOW - timedelta(seconds=1199)), config=config, now=NOW)
        assert report.state == MailIntakeHealth.OK

    def test_a_mailbox_kept_alive_by_a_human_is_not_healthy(self) -> None:
        """The disguise the fault has worn every previous time.

        Somebody notices no mail arrived, presses the trigger button, mail
        appears, and the underlying condition — nothing is polling on a schedule
        — is never diagnosed. A recent *manual* poll must therefore not read as
        healthy, however recent it is.
        """
        report = grade(a_run(trigger=MailIntakeTrigger.MANUAL), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy
        assert not report.scheduled_run_seen
        assert "by hand" in report.detail

    def test_a_cli_poll_does_not_count_as_a_working_scheduler_either(self) -> None:
        report = grade(a_run(trigger=MailIntakeTrigger.CLI), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE


class TestAPollerThatIsAliveAndWrong:
    def test_a_poll_that_could_not_read_the_mailbox_is_failing_not_stale(self) -> None:
        """Alive-and-failing and stopped are different faults with different fixes."""
        report = grade(
            a_run(ok=False, error="Microsoft Graph returned 401: invalid client secret"),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.FAILING
        assert "invalid client secret" in report.detail

    def test_staleness_outranks_a_failure_because_nothing_is_running(self) -> None:
        report = grade(
            a_run(started_at=NOW - timedelta(days=2), ok=False, error="401"),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.STALE

    def test_a_dropped_message_is_reported_as_losing_mail(self) -> None:
        report = grade(a_run(fetched=5, dropped=2), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.LOSING_MAIL
        assert "silent loss" in report.detail

    def test_a_blind_sweep_is_reported_as_losing_mail(self) -> None:
        report = grade(a_run(sweep_blind=True), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.LOSING_MAIL

    def test_the_last_success_is_carried_separately_from_the_last_run(self) -> None:
        """So an operator can see how long it has actually been collecting for."""
        report = grade(
            a_run(ok=False, error="401"),
            last_success=a_run(started_at=NOW - timedelta(hours=5)),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.FAILING
        assert report.last_success_age_seconds == pytest.approx(5 * 3600)


class TestTheTwoWaysIn:
    """The regression this class exists for shipped and fired once an hour.

    Notifications were added as the primary intake path and the health check was
    not told. Two faults followed, and they are the two shapes this class guards:

    1. A change notification is not a person pressing a button. The check graded
       the newest run by trigger, saw `webhook`, and announced "the only recent
       poll was triggered by hand — start the Celery worker and beat" at a worker
       and beat that were both running perfectly.
    2. The two paths hide each other. Either one alone keeps mail arriving, so no
       "has mail arrived recently" question can find a half-dead intake — and the
       surviving path is then a single point of failure nobody knows they have.
    """

    def test_a_change_notification_is_not_a_human_pressing_a_button(self) -> None:
        """Fault 1, exactly: a live webhook must never read as a hand-crank."""
        notification = a_run(trigger=MailIntakeTrigger.WEBHOOK)
        report = evaluate(
            notification,
            sweep=a_run(),
            webhook=notification,
            subscription=a_subscription(expires_in=timedelta(days=2)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.OK
        assert report.healthy
        assert "by hand" not in report.detail
        assert "start" not in report.detail.lower()

    def test_a_lapsed_subscription_is_found_even_though_the_sweep_is_collecting(
        self,
    ) -> None:
        """Fault 2: mail keeps arriving, so only the subscription itself can tell."""
        report = evaluate(
            a_run(),
            sweep=a_run(),
            subscription=a_subscription(expires_in=timedelta(hours=-2)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.DEGRADED
        assert not report.healthy
        assert report.sweep_collecting
        assert not report.webhook_collecting
        # It has to name the path that died, or the operator restarts the wrong thing.
        assert "sub-0001" in report.detail
        assert "4230" in report.detail

    def test_a_dead_scheduler_is_found_even_though_notifications_are_collecting(
        self,
    ) -> None:
        """The mirror image, and the one a working webhook would otherwise mask."""
        notification = a_run(trigger=MailIntakeTrigger.WEBHOOK)
        report = evaluate(
            notification,
            sweep=a_run(started_at=NOW - timedelta(hours=6)),
            webhook=notification,
            subscription=a_subscription(expires_in=timedelta(days=2)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.DEGRADED
        assert report.webhook_collecting
        assert not report.sweep_collecting
        # The sweep's value is reconciliation, not speed. The sentence must say so,
        # because "mail is arriving" is exactly why this gets deprioritised.
        assert "does not guarantee" in report.detail
        assert "beat" in report.detail.lower()

    def test_a_webhook_run_is_never_evidence_that_the_sweep_is_alive(self) -> None:
        """Both paths write to one ledger, so `last_run_at` cannot answer this.

        The newest run here is 30 seconds old and intake is *not* healthy. That
        gap is the entire reason the limbs are graded separately.
        """
        notification = a_run(trigger=MailIntakeTrigger.WEBHOOK)
        report = evaluate(
            notification,
            sweep=a_run(started_at=NOW - timedelta(days=3)),
            webhook=notification,
            subscription=a_subscription(expires_in=timedelta(days=2)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.last_run_age_seconds == pytest.approx(30)
        assert not report.sweep_collecting
        assert report.state == MailIntakeHealth.DEGRADED

    def test_both_paths_down_is_stale_rather_than_degraded(self) -> None:
        """Degraded means mail is still arriving. When it is not, say so."""
        report = evaluate(
            a_run(started_at=NOW - timedelta(days=3)),
            sweep=a_run(started_at=NOW - timedelta(days=3)),
            subscription=a_subscription(expires_in=timedelta(hours=-30)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.STALE
        assert "Nothing is collecting mail" in report.detail

    def test_a_deployment_with_no_webhook_is_not_degraded_for_lacking_one(self) -> None:
        """An unconfigured webhook is not a broken one.

        Without this, every developer machine and every pre-tunnel environment
        would report a permanent fault — and a permanently-failing check is a
        check people learn to ignore.
        """
        report = grade(a_run(), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK
        assert not report.webhook_ready
        assert not report.webhook_collecting

    def test_notifications_alone_are_healthy_when_the_sweep_is_switched_off(self) -> None:
        """A deliberate choice is not a fault, even an unwise one."""
        notification = a_run(trigger=MailIntakeTrigger.WEBHOOK)
        report = evaluate(
            notification,
            webhook=notification,
            subscription=a_subscription(expires_in=timedelta(days=2)),
            config=with_webhook(poll_enabled=False),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.OK
        assert "switched off" in report.detail

    def test_a_lapsed_subscription_reports_how_long_it_has_been_dead(self) -> None:
        """Negative rather than clamped at zero: the duration is the diagnosis."""
        report = evaluate(
            a_run(),
            sweep=a_run(),
            subscription=a_subscription(expires_in=timedelta(hours=-9)),
            config=with_webhook(),
            now=NOW,
        )
        assert report.subscription_expires_in_seconds == pytest.approx(-9 * 3600)
        assert "9 hours ago" in report.detail

    def test_a_missing_subscription_is_distinguished_from_a_lapsed_one(self) -> None:
        """Different causes, different fixes: never created vs. not renewed."""
        report = evaluate(a_run(), sweep=a_run(), config=with_webhook(), now=NOW)
        assert report.state == MailIntakeHealth.DEGRADED
        assert "No Graph subscription has been created" in report.detail
        assert report.subscription_id is None


class TestConfigurationDrift:
    """The false alarm that prompted all of this, and why it leads the sentence.

    An API started at 15:00:04 and `.env` was saved at 15:04:53, changing the
    poll interval from 8s to 600s. The process kept the old interval, so it kept
    a 180-second staleness threshold, and graded a ten-minute sweep against it —
    guaranteeing a critical "nothing is collecting mail" every hour about a
    deployment that was working. Nothing in the alarm hinted at the cause.
    """

    def test_a_fault_found_on_superseded_settings_says_so_first(self) -> None:
        report = evaluate(
            a_run(started_at=NOW - timedelta(hours=2)),
            sweep=a_run(started_at=NOW - timedelta(hours=2)),
            config_stale_seconds=289.0,
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.STALE
        # First, not as a footnote: it is the reason to doubt everything after it.
        assert report.detail.startswith("`.env` has been edited")
        assert "Restart the API" in report.detail
        assert report.config_stale_seconds == pytest.approx(289.0)

    def test_a_healthy_verdict_is_not_prefixed_with_a_warning(self) -> None:
        """Drift is still on the report — but good news does not open with an alarm.

        A check that hedges every healthy answer is a check whose warnings stop
        being read, which is how the fault this module exists for stayed hidden.
        """
        report = grade(a_run(), config_stale_seconds=289.0, config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK
        assert not report.detail.startswith("`.env`")
        assert report.config_stale_seconds == pytest.approx(289.0)


class TestSeverityOrdering:
    def test_stale_outranks_everything(self) -> None:
        assert (
            worst_of(
                MailIntakeHealth.OK,
                MailIntakeHealth.LOSING_MAIL,
                MailIntakeHealth.STALE,
            )
            == MailIntakeHealth.STALE
        )

    def test_ok_loses_to_any_fault(self) -> None:
        assert worst_of(MailIntakeHealth.OK, MailIntakeHealth.FAILING) == (MailIntakeHealth.FAILING)

    def test_every_state_is_ranked(self) -> None:
        """A state added without a severity would crash `worst_of` at runtime."""
        from app.domain.enums import MAIL_INTAKE_HEALTH_SEVERITY

        assert set(MAIL_INTAKE_HEALTH_SEVERITY) == set(MailIntakeHealth)


class TestNaiveTimestamps:
    def test_a_timestamp_without_a_zone_is_read_as_utc(self) -> None:
        """Postgres returns aware datetimes; a fake or a SQLite test may not.

        Without the coercion this raises `TypeError: can't subtract offset-naive
        and offset-aware datetimes` — inside the health check, which would make
        the watchdog the thing that breaks.
        """
        run = a_run()
        run.started_at = (NOW - timedelta(hours=1)).replace(tzinfo=None)
        report = grade(run, config=configured(), now=NOW)
        assert report.last_run_age_seconds == pytest.approx(3600)


class _RunRepository:
    """Just enough repository for the health service.

    `latest_run_by_trigger` is not a convenience here — it is the behaviour under
    test. The service must grade the sweep on scheduled rows alone, and a stub
    that answered it with the newest row of any kind would let a change
    notification stand in for a heartbeat and quietly pass a test that should
    fail.
    """

    def __init__(
        self,
        runs: list[MailIntakeRun],
        subscription: MailSubscription | None = None,
    ) -> None:
        self._runs = runs
        self._subscription = subscription

    async def latest_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        return max(self._runs, key=lambda row: row.started_at) if self._runs else None

    async def latest_successful_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        ok = [row for row in self._runs if row.ok]
        return max(ok, key=lambda row: row.started_at) if ok else None

    async def latest_run_by_trigger(
        self, trigger: MailIntakeTrigger, mailbox: str | None = None
    ) -> MailIntakeRun | None:
        matching = [row for row in self._runs if row.trigger == trigger]
        return max(matching, key=lambda row: row.started_at) if matching else None

    async def newest_subscription(self, mailbox: str | None = None) -> MailSubscription | None:
        return self._subscription


class TestTheService:
    @pytest.mark.asyncio
    async def test_it_grades_the_newest_run(self) -> None:
        runs = [
            a_run(started_at=NOW - timedelta(days=9)),
            a_run(started_at=NOW - timedelta(seconds=20)),
        ]
        service = MailIntakeHealthService(_RunRepository(runs), config=configured())  # type: ignore[arg-type]
        report = await service.report(now=NOW)
        assert report.state == MailIntakeHealth.OK

    @pytest.mark.asyncio
    async def test_an_empty_ledger_reports_never_run(self) -> None:
        service = MailIntakeHealthService(_RunRepository([]), config=configured())  # type: ignore[arg-type]
        report = await service.report(now=NOW)
        assert report.state == MailIntakeHealth.NEVER_RUN

    @pytest.mark.asyncio
    async def test_it_needs_no_graph_client_to_report_a_missing_mailbox(self) -> None:
        """The endpoint that names a credentials problem must not need credentials.

        `GraphMailClient.__init__` raises `GraphNotConfiguredError` when they are
        absent, so a health path that built one could never report the state it
        most needs to.
        """
        service = MailIntakeHealthService(_RunRepository([]), config=unconfigured())  # type: ignore[arg-type]
        report = await service.report(now=NOW)
        assert report.state == MailIntakeHealth.NOT_CONFIGURED
