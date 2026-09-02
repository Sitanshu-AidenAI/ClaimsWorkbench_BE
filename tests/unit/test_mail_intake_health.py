"""The check that answers "is mail actually being collected" from outside the poller.

Every one of these tests is about a failure that used to be invisible. The bug
they guard against is not "intake is broken" — intake works, and the rest of
`test_mail_intake.py` proves it. The bug is that when intake *stops*, nothing
anywhere says so: a dead scheduler and an empty mailbox produce identical
evidence, which is how messages sat uncollected for five days on a deployment
whose API reported healthy the whole time.

So the assertions here are mostly about states, not mechanics. `evaluate` is pure
— a function of (last run, settings, now) — precisely so that six days of silence
is a one-line test instead of a six-day one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import GraphSettings
from app.domain.enums import MailIntakeHealth, MailIntakeTrigger
from app.models.mail_intake import MailIntakeRun
from app.services.mail.health import (
    MINIMUM_STALE_AFTER_SECONDS,
    STALE_INTERVAL_MULTIPLE,
    MailIntakeHealthService,
    evaluate,
    stale_after_seconds,
    worst_of,
)

MAILBOX = "claims@carrier.test"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def configured(**overrides: object) -> GraphSettings:
    """Settings for a mailbox that is fully set up and polling."""
    values: dict[str, object] = {
        "tenant_id": "tenant",
        "client_id": "client",
        "client_secret": "secret",
        "shared_mailbox": MAILBOX,
        "poll_enabled": True,
        "poll_interval_seconds": 300,
    }
    values.update(overrides)
    return GraphSettings(**values)  # type: ignore[arg-type]


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
        report = evaluate(None, config=unconfigured(), now=NOW)
        assert report.state == MailIntakeHealth.NOT_CONFIGURED
        assert not report.healthy
        # The sentence has to send someone to the right place, not to a log.
        assert "CWB_GRAPH_TENANT_ID" in report.detail

    def test_polling_switched_off_says_so_rather_than_looking_dead(self) -> None:
        report = evaluate(None, config=configured(poll_enabled=False), now=NOW)
        assert report.state == MailIntakeHealth.DISABLED
        assert "CWB_GRAPH_POLL_ENABLED" in report.detail

    def test_a_recent_scheduled_poll_is_healthy(self) -> None:
        report = evaluate(a_run(), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK
        assert report.healthy
        assert report.scheduled_run_seen

    def test_a_quiet_mailbox_is_still_healthy(self) -> None:
        """Fetching nothing is the normal state of a working mailbox.

        This is the test that keeps the check honest: the whole failure being
        guarded against looks exactly like this from inside a poll, and the
        difference is *when the poll ran*, never what it found.
        """
        report = evaluate(a_run(fetched=0, ingested=0), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.OK


class TestTheFailureThatUsedToBeInvisible:
    def test_no_poll_ever_recorded_is_reported_as_never_run(self) -> None:
        report = evaluate(None, config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.NEVER_RUN
        assert not report.healthy
        # It must name the actual remedy. "Start the worker" is the whole fix and
        # it is not guessable from a status code.
        assert "worker" in report.detail.lower()
        assert "beat" in report.detail.lower()

    def test_silence_longer_than_the_threshold_is_stale(self) -> None:
        config = configured(poll_interval_seconds=300)
        report = evaluate(a_run(started_at=NOW - timedelta(seconds=1201)), config=config, now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy

    def test_six_days_of_silence_is_stale_and_says_so_in_days(self) -> None:
        """The exact outage on this deployment, as a one-line test."""
        report = evaluate(a_run(started_at=NOW - timedelta(days=6)), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert "6 days" in report.detail
        assert report.last_run_age_seconds == pytest.approx(6 * 86400)

    def test_silence_just_inside_the_threshold_is_not_stale(self) -> None:
        config = configured(poll_interval_seconds=300)
        report = evaluate(a_run(started_at=NOW - timedelta(seconds=1199)), config=config, now=NOW)
        assert report.state == MailIntakeHealth.OK

    def test_a_mailbox_kept_alive_by_a_human_is_not_healthy(self) -> None:
        """The disguise the fault has worn every previous time.

        Somebody notices no mail arrived, presses the trigger button, mail
        appears, and the underlying condition — nothing is polling on a schedule
        — is never diagnosed. A recent *manual* poll must therefore not read as
        healthy, however recent it is.
        """
        report = evaluate(a_run(trigger=MailIntakeTrigger.MANUAL), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE
        assert not report.healthy
        assert not report.scheduled_run_seen
        assert "by hand" in report.detail

    def test_a_cli_poll_does_not_count_as_a_working_scheduler_either(self) -> None:
        report = evaluate(a_run(trigger=MailIntakeTrigger.CLI), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.STALE


class TestAPollerThatIsAliveAndWrong:
    def test_a_poll_that_could_not_read_the_mailbox_is_failing_not_stale(self) -> None:
        """Alive-and-failing and stopped are different faults with different fixes."""
        report = evaluate(
            a_run(ok=False, error="Microsoft Graph returned 401: invalid client secret"),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.FAILING
        assert "invalid client secret" in report.detail

    def test_staleness_outranks_a_failure_because_nothing_is_running(self) -> None:
        report = evaluate(
            a_run(started_at=NOW - timedelta(days=2), ok=False, error="401"),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.STALE

    def test_a_dropped_message_is_reported_as_losing_mail(self) -> None:
        report = evaluate(a_run(fetched=5, dropped=2), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.LOSING_MAIL
        assert "silent loss" in report.detail

    def test_a_blind_sweep_is_reported_as_losing_mail(self) -> None:
        report = evaluate(a_run(sweep_blind=True), config=configured(), now=NOW)
        assert report.state == MailIntakeHealth.LOSING_MAIL

    def test_the_last_success_is_carried_separately_from_the_last_run(self) -> None:
        """So an operator can see how long it has actually been collecting for."""
        report = evaluate(
            a_run(ok=False, error="401"),
            last_success=a_run(started_at=NOW - timedelta(hours=5)),
            config=configured(),
            now=NOW,
        )
        assert report.state == MailIntakeHealth.FAILING
        assert report.last_success_age_seconds == pytest.approx(5 * 3600)


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
        report = evaluate(run, config=configured(), now=NOW)
        assert report.last_run_age_seconds == pytest.approx(3600)


class _RunRepository:
    """Just enough repository for the health service."""

    def __init__(self, runs: list[MailIntakeRun]) -> None:
        self._runs = runs

    async def latest_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        return max(self._runs, key=lambda row: row.started_at) if self._runs else None

    async def latest_successful_run(self, mailbox: str | None = None) -> MailIntakeRun | None:
        ok = [row for row in self._runs if row.ok]
        return max(ok, key=lambda row: row.started_at) if ok else None


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
