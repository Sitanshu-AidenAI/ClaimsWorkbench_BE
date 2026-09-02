"""Keeping a Graph change-notification subscription alive.

Intake has two ways in and they are not equals. A **change notification** is the
mailbox telling us a message arrived, seconds after it did. A **poll** is us
asking, and it stays because Microsoft does not guarantee notification delivery —
they document that notifications can be missed — so the sweep is the thing that
makes the webhook safe to rely on rather than the thing the webhook replaces.

What this module owns is the subscription's life: create it, keep it alive, notice
when it has died, and tear it down. The *decisions* are in
`app.domain.mail_subscription` where they can be tested without a network; what is
here is the IO and the record.

**The renewal is not optional and cannot be configured away.** Graph caps a
subscription on an Outlook mail resource at 4230 minutes — 70½ hours. Other
resource types get thirty days; mail does not. So a deployment that creates a
subscription and walks away has intake for three days and silence afterwards, and
the silence looks exactly like a quiet mailbox.

Nothing here commits. The caller owns the transaction, the same rule the rest of
this codebase follows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import GraphSettings, settings
from app.core.logging import get_logger
from app.domain import mail_subscription as rules
from app.domain.mail_subscription import SubscriptionAction
from app.integrations.graph.client import GraphMailClient
from app.integrations.graph.errors import GraphError
from app.models.mail_intake import MailSubscription
from app.repositories.mail_intake import MailIntakeRepository

logger = get_logger(__name__)


class MailSubscriptionService:
    def __init__(
        self,
        messages: MailIntakeRepository,
        client: GraphMailClient,
        *,
        config: GraphSettings | None = None,
    ) -> None:
        self._messages = messages
        self._client = client
        self._config = config or settings.graph

    # -- Keeping it alive ----------------------------------------------------

    async def ensure(self, *, now: datetime | None = None) -> MailSubscription | None:
        """Make sure a live subscription exists, and return it.

        Idempotent and safe to run on every boot and every renewal tick. Returns
        `None` when the webhook is not configured — which is not a failure and is
        the ordinary state of a machine with no public URL.

        The three outcomes come from `rules.decide`:

        * **leave** — there is time left and the address is still ours;
        * **renew** — extend it, and fall through to a recreate if Graph has
          forgotten it;
        * **recreate** — nothing stored, it has lapsed, or the notification URL
          has changed under it. The last is the one that bites in development: a
          dev tunnel issues a new hostname on restart and the old subscription
          goes on being renewed while Graph posts into the void.
        """
        if not self._config.webhook_ready:
            logger.debug("mail_subscription_skipped_unconfigured")
            return None

        moment = now or datetime.now(UTC)
        url = str(self._config.notification_url)
        resource = self._client.messages_resource()
        existing = await self._messages.get_subscription(
            mailbox=self._client.mailbox, resource=resource
        )

        action = rules.decide(
            existing,
            notification_url=url,
            now=moment,
            renew_before=timedelta(minutes=self._config.renew_before_minutes),
        )

        if action is SubscriptionAction.LEAVE and existing is not None:
            return existing

        if action is SubscriptionAction.RENEW and existing is not None:
            renewed = await self._renew(existing, now=moment)
            if renewed is not None:
                return renewed
            #: Graph no longer has it. Fall through and make a new one rather than
            #: report a failed renewal — the mailbox still needs collecting.
            logger.info(
                "mail_subscription_renew_fell_through", subscription_id=existing.subscription_id
            )

        return await self._recreate(existing, resource=resource, url=url, now=moment)

    async def _renew(self, existing: MailSubscription, *, now: datetime) -> MailSubscription | None:
        expires_at = rules.expiry_for(
            now, lifetime_minutes=self._config.subscription_lifetime_minutes
        )
        payload = await self._client.renew_subscription(
            existing.subscription_id, expires_at=expires_at
        )
        if payload is None:
            return None

        existing.expires_at = expires_at
        existing.renewed_at = now
        existing.renewal_count += 1
        await self._messages.flush()
        logger.info(
            "mail_subscription_renewed",
            subscription_id=existing.subscription_id,
            expires_at=expires_at.isoformat(),
            renewal_count=existing.renewal_count,
        )
        return existing

    async def _recreate(
        self,
        existing: MailSubscription | None,
        *,
        resource: str,
        url: str,
        now: datetime,
    ) -> MailSubscription | None:
        """Delete what we have, if anything, and subscribe again.

        The delete is best-effort and deliberately so: the common reasons to be
        here are that Graph has already dropped the subscription or that it points
        at a tunnel that no longer exists, and failing the whole operation because
        a dead subscription could not be deleted twice would leave the mailbox
        uncollected over a tidiness problem.
        """
        if existing is not None:
            try:
                await self._client.delete_subscription(existing.subscription_id)
            except GraphError as exc:
                logger.warning(
                    "mail_subscription_delete_failed",
                    subscription_id=existing.subscription_id,
                    error=str(exc),
                )
            await self._messages.delete_subscription(existing)
            await self._messages.flush()

        expires_at = rules.expiry_for(
            now, lifetime_minutes=self._config.subscription_lifetime_minutes
        )
        payload = await self._client.create_subscription(
            notification_url=url,
            lifecycle_url=self._config.resolved_lifecycle_url,
            client_state=str(self._config.webhook_client_state),
            expires_at=expires_at,
        )

        subscription = self._messages.add_subscription(
            MailSubscription(
                subscription_id=str(payload.get("id", "")),
                mailbox=self._client.mailbox,
                resource=resource,
                notification_url=url,
                #: Ours, not Graph's echo. Graph returns the expiry it granted and
                #: it matches what we asked for; storing our own value keeps the
                #: renewal arithmetic in one place and independent of the wire
                #: format it comes back in.
                expires_at=expires_at,
                renewed_at=None,
                renewal_count=0,
            )
        )
        await self._messages.flush()
        logger.info(
            "mail_subscription_created",
            subscription_id=subscription.subscription_id,
            resource=resource,
            expires_at=expires_at.isoformat(),
        )
        return subscription

    async def teardown(self) -> int:
        """Remove the subscription for this mailbox. Returns how many went.

        For a deployment being replaced and for a developer whose tunnel has
        moved. Leaving one behind is not harmless: Graph keeps trying to deliver
        to an address nobody is listening on, and the app registration's
        subscription quota fills with corpses.
        """
        resource = self._client.messages_resource()
        existing = await self._messages.get_subscription(
            mailbox=self._client.mailbox, resource=resource
        )
        if existing is None:
            return 0

        try:
            await self._client.delete_subscription(existing.subscription_id)
        except GraphError as exc:
            logger.warning(
                "mail_subscription_delete_failed",
                subscription_id=existing.subscription_id,
                error=str(exc),
            )

        await self._messages.delete_subscription(existing)
        await self._messages.flush()
        logger.info("mail_subscription_torn_down", subscription_id=existing.subscription_id)
        return 1

    # -- Receiving one -------------------------------------------------------

    async def authentic(self, *, client_state: str | None, subscription_id: str | None) -> bool:
        """Whether a notification is one of ours. See `rules.authentic`.

        The database read is the second half of the check: the secret proves the
        caller was told it, and the id proves the subscription is one we made.
        """
        return rules.authentic(
            client_state=client_state,
            expected_client_state=self._config.webhook_client_state,
            subscription_id=subscription_id,
            known_subscription_ids=await self._messages.list_subscription_ids(),
        )


__all__ = ["MailSubscriptionService"]
