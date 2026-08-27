"""The watchdog that notices mailbox intake has stopped, and says so unprompted.

`app.services.mail.health` can answer "is intake alive" when asked. This asks.

The distinction matters because of how the fault actually presents. Nobody goes
looking for a broken mail poller — they go looking for one specific email a
broker insists they sent, days after the poller died, and by then the question
has already cost somebody an afternoon. Every previous occurrence in this
codebase was found that way. So the check cannot wait to be called: something has
to run it on a timer and put the answer where a human already is.

Two decisions define this module:

* **It runs in the API process, not the worker.** That is not a convenience. The
  thing being watched is the Celery beat/worker pair, and a watchdog inside the
  process it watches reports nothing when that process is gone — which is the
  only case anyone cares about. The API is the process that is always up, and
  the process whose log the developer is already reading.
* **It writes a notification, not just a log line.** A log line is only read by
  someone who already suspects a problem. The desk panel is read by handlers who
  do not, and "no mail has been collected for six hours" is exactly the sentence
  that stops the afternoon being wasted.

The loop is deliberately dull: it never raises into the lifespan, it never
retries a database that is down more aggressively than its own interval, and it
holds no state that survives a restart beyond the notification dedupe key.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.domain.enums import MailIntakeHealth
from app.repositories.mail_intake import MailIntakeRepository
from app.repositories.notification import NotificationRepository
from app.services.mail.health import MailIntakeHealthReport, MailIntakeHealthService
from app.services.notifications.service import NotificationService

logger = get_logger(__name__)

#: Floor on how often the watchdog looks. A poll interval of eight seconds does
#: not warrant eight-second database round trips from the API; the staleness
#: threshold is minutes wide, so checking every minute detects it just as fast.
MINIMUM_CHECK_INTERVAL_SECONDS = 60

#: How long the first check waits, so a cold start has time to boot a worker
#: before being accused of not having one. Without it, every `make dev` would
#: announce `never_run` for its first few seconds.
STARTUP_GRACE_SECONDS = 90


def check_interval_seconds(config: Settings | None = None) -> int:
    config = config or settings
    return max(MINIMUM_CHECK_INTERVAL_SECONDS, config.graph.poll_interval_seconds)


class MailIntakeWatchdog:
    """Periodically grades intake and announces it when the answer is bad."""

    def __init__(self, config: Settings | None = None) -> None:
        self._config = config or settings
        self._task: asyncio.Task[None] | None = None
        #: The last state announced, so a continuing fault does not re-log every
        #: tick while a *change* of state always does.
        self._last_state: MailIntakeHealth | None = None

    # -- Lifecycle -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether watching is worth doing on this deployment.

        Note that a *disabled* poller is still watched: `poll_enabled=false` with
        credentials present is a state the endpoint reports and an operator may
        want announced. What is not watched is a deployment with no Graph
        credentials at all, where there is no mailbox to have an opinion about.
        """
        return self._config.graph.configured

    async def start(self) -> None:
        if not self.enabled:
            logger.info("mail_intake_watchdog_not_started", reason="graph_not_configured")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="mail-intake-watchdog")
        logger.info(
            "mail_intake_watchdog_started",
            check_interval_seconds=check_interval_seconds(self._config),
            startup_grace_seconds=STARTUP_GRACE_SECONDS,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("mail_intake_watchdog_stopped")

    # -- The loop ------------------------------------------------------------

    async def _run(self) -> None:
        interval = check_interval_seconds(self._config)
        try:
            await asyncio.sleep(STARTUP_GRACE_SECONDS)
            while True:
                try:
                    await self.check_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never let a bad check kill the watchdog: a watchdog that
                    # dies silently is worse than no watchdog, because the
                    # absence of alarms then reads as good news.
                    logger.error("mail_intake_watchdog_check_failed", error=str(exc), exc_info=exc)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def check_once(self) -> MailIntakeHealthReport:
        """One grading, logged and — where it is bad — announced. Returns it too."""
        factory = get_session_factory()
        async with factory() as session:
            messages = MailIntakeRepository(session)
            report = await MailIntakeHealthService(messages, config=self._config.graph).report()

            changed = report.state != self._last_state
            if report.healthy:
                if changed and self._last_state is not None:
                    logger.info(
                        "mail_intake_recovered",
                        state=report.state.value,
                        mailbox=report.mailbox,
                        detail=report.detail,
                    )
            else:
                # Every tick, not only on change. The operator's question is
                # asked in the present tense — "is it broken *now*" — and a line
                # that scrolled past an hour ago does not answer it. This is the
                # same reasoning as `_warn_if_configuration_is_stale`.
                logger.error(
                    "mail_intake_unhealthy",
                    state=report.state.value,
                    mailbox=report.mailbox,
                    last_run_at=(report.last_run_at.isoformat() if report.last_run_at else None),
                    last_run_age_seconds=report.last_run_age_seconds,
                    last_run_trigger=report.last_run_trigger,
                    stale_after_seconds=report.stale_after_seconds,
                    detail=report.detail,
                )
                await self._announce(session, report)

            self._last_state = report.state
            return report

    async def _announce(self, session: AsyncSession, report: MailIntakeHealthReport) -> None:
        """Put the fault on the desk panel, at most once an hour per state.

        The hourly bucket is the whole dedupe strategy and it is a deliberate
        trade: a mailbox that has been down since Tuesday should be *visible*
        every time someone opens the panel, without having written 3,000 rows to
        get there. One row per hour per state does both.

        States that are configuration rather than failure — `disabled` — are
        logged above but not announced: a handler cannot act on a deployment
        decision, and a panel that cries about deliberate settings gets ignored,
        taking the real alarms with it.
        """
        if report.state == MailIntakeHealth.DISABLED:
            return

        hour = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        notifications = NotificationService(NotificationRepository(session))
        recorded = await notifications.mail_intake_unhealthy(
            state=report.state,
            detail=report.detail,
            mailbox=report.mailbox,
            last_run_at=report.last_run_at,
            last_run_age_seconds=report.last_run_age_seconds,
            dedupe_key=f"mail_intake.unhealthy:{report.state.value}:{hour}",
        )
        if recorded is not None:
            await session.commit()


#: The API's single watchdog, held at module scope for the same reason the mail
#: client is: one per process, started and stopped by the lifespan.
_watchdog: MailIntakeWatchdog | None = None


def get_watchdog(config: Settings | None = None) -> MailIntakeWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = MailIntakeWatchdog(config)
    return _watchdog


async def start_watchdog(config: Settings | None = None) -> None:
    await get_watchdog(config).start()


async def stop_watchdog() -> None:
    global _watchdog
    if _watchdog is not None:
        await _watchdog.stop()
        _watchdog = None


__all__ = [
    "MailIntakeWatchdog",
    "check_interval_seconds",
    "get_watchdog",
    "start_watchdog",
    "stop_watchdog",
]
