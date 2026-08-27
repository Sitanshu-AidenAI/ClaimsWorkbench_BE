"""The Outlook half of Microsoft Graph: reading a shared mailbox.

This client knows about messages, attachments and folders. It knows nothing
about claims, notices or documents — that separation is the reason it can be
tested against captured Graph payloads, and the reason the intake service can be
tested without Graph at all.

Three decisions are worth naming:

* **Attachment metadata is read before any bytes are.** `$select` deliberately
  omits `contentBytes`, so a 60MB video on a broker's email costs one small JSON
  response and is refused on its size, rather than being pulled into memory and
  then rejected.
* **A 401 is retried exactly once, after dropping the cached token.** Tokens
  expire, and a clock that has drifted is not a reason to fail a poll. A second
  401 is a credentials problem and is raised as one.
* **A 404 on a single message is not an error worth raising.** Mailboxes move
  and people delete things mid-poll; the caller is told the message is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import GraphSettings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.graph.auth import GraphTokenProvider
from app.integrations.graph.errors import GraphAuthError, GraphError
from app.integrations.graph.messages import (
    ATTACHMENT_SELECT_FIELDS,
    MESSAGE_SELECT_FIELDS,
    GraphAttachmentMetadata,
    GraphMessage,
    parse_attachment,
    parse_message,
)
from app.integrations.http import HttpClient

logger = get_logger(__name__)


class GraphNotConfiguredError(GraphError):
    """Mailbox intake was asked for on a deployment that has no credentials."""

    status_code = 503
    code = "graph_not_configured"
    message = "Mailbox intake is not configured: set the Graph tenant, client, secret and mailbox."


class GraphMailClient:
    """Reads — and, where configured, marks — messages in one shared mailbox."""

    def __init__(
        self,
        config: GraphSettings | None = None,
        *,
        tokens: GraphTokenProvider | None = None,
    ) -> None:
        self._config = config or settings.graph
        if not self._config.configured:
            raise GraphNotConfiguredError()
        self._tokens = tokens or GraphTokenProvider(self._config)
        self._http = HttpClient(
            self._config.api_base_url,
            timeout=httpx.Timeout(self._config.timeout_seconds, connect=10.0),
            max_attempts=self._config.http_max_attempts,
        )

    @property
    def mailbox(self) -> str:
        # `configured` has already established this is set; the assertion is for
        # the type checker, not for the runtime.
        assert self._config.shared_mailbox is not None
        return self._config.shared_mailbox

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._tokens.aclose()

    # -- Reads ---------------------------------------------------------------

    async def list_messages(
        self,
        *,
        limit: int | None = None,
        unread_only: bool | None = None,
        folder: str | None = None,
        since: datetime | None = None,
    ) -> list[GraphMessage]:
        """Messages from the configured folder, oldest first.

        Oldest first on purpose: notifications are worked in the order a claims
        team would have opened them, and a poll that always read the newest page
        would starve the bottom of a backlog indefinitely.

        `@odata.nextLink` **is** followed, up to `max_pages`. It did not used to
        be, and the omission was a claim-losing bug rather than a tuning choice:
        with `$top=25` and an ascending sort, a folder holding twenty-six read
        messages returns the same oldest twenty-five to every poll forever, and
        the twenty-sixth — the one that just arrived — is never in the window.
        Nothing about that failure is visible from the outside; the poll reports
        success on a page of messages it has already collected.

        `since` is the other half. Following pages to the end of a folder that
        has years in it is not something a five-minute poll should do, so the
        caller passes a watermark and Graph does the narrowing server-side. The
        two together are what bound the sweep by *time* instead of by count:
        every message newer than the watermark is seen, however many that is,
        and nothing older is transferred.
        """
        unread = self._config.unread_only if unread_only is None else unread_only
        # `batch_size` is the size of a *page*, not the size of a poll. Conflating
        # the two is what made paging pointless: a ceiling of `batch_size` breaks
        # out of the loop the moment the first page is full, which is the exact
        # behaviour the paging is here to remove. A poll's real ceiling is every
        # page it is allowed to follow, unless the caller names a smaller one.
        page_size = self._config.batch_size
        ceiling = limit if limit is not None else page_size * self._config.max_pages
        page_size = min(page_size, ceiling)

        filters: list[str] = []
        if unread:
            filters.append("isRead eq false")
        if since is not None:
            # Graph wants UTC, to the second, with a literal Z.
            stamp = since.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            filters.append(f"receivedDateTime ge {stamp}")

        params: dict[str, Any] = {
            "$select": ",".join(MESSAGE_SELECT_FIELDS),
            "$top": page_size,
            "$orderby": "receivedDateTime asc",
        }
        if filters:
            params["$filter"] = " and ".join(filters)

        messages: list[GraphMessage] = []
        pages = 0
        truncated = False
        path: str | None = f"{self._folder_path(folder)}/messages"
        # Only the first request carries params; `@odata.nextLink` is an absolute
        # URL that already has them baked in, and re-appending would reject it.
        request_params: dict[str, Any] | None = params

        while path is not None and pages < self._config.max_pages:
            response = await self._request("GET", path, params=request_params)
            payload = _json(response)
            rows = payload.get("value")
            if not isinstance(rows, list):
                raise GraphError("The message list response carried no `value` array.")

            messages.extend(parse_message(row) for row in rows if isinstance(row, dict))
            pages += 1

            if len(messages) >= ceiling:
                truncated = len(messages) > ceiling or bool(payload.get("@odata.nextLink"))
                messages = messages[:ceiling]
                break

            next_link = payload.get("@odata.nextLink")
            path = next_link if isinstance(next_link, str) else None
            request_params = None
            if path is not None and pages >= self._config.max_pages:
                truncated = True

        logger.info(
            "graph_messages_listed",
            mailbox=self.mailbox,
            folder=folder or self._config.mail_folder,
            unread_only=unread,
            since=since.isoformat() if since else None,
            pages=pages,
            count=len(messages),
            truncated=truncated,
        )
        if truncated:
            # Not an error — the next poll continues from a watermark this batch
            # advances — but it must not be silent, because a permanently
            # truncated sweep and a healthy one log the same `count` otherwise.
            logger.warning(
                "graph_message_list_truncated",
                mailbox=self.mailbox,
                folder=folder or self._config.mail_folder,
                returned=len(messages),
                pages=pages,
                max_pages=self._config.max_pages,
            )
        return messages

    async def folder_stats(self, folder: str | None = None) -> tuple[int, int]:
        """`(total, unread)` item counts for the folder, for reconciliation.

        Intake uses this to answer a question its own ledger cannot: does the
        mailbox hold more than we have rows for? A poll that collects nothing
        because the folder is empty and a poll that collects nothing because the
        sweep cannot see the folder are indistinguishable without it.
        """
        response = await self._request(
            "GET",
            self._folder_path(folder),
            params={"$select": "totalItemCount,unreadItemCount"},
            allow_missing=True,
        )
        if response.status_code == 404:
            return 0, 0
        payload = _json(response)
        total = payload.get("totalItemCount")
        unread = payload.get("unreadItemCount")
        return (total if isinstance(total, int) else 0, unread if isinstance(unread, int) else 0)

    async def get_message(self, message_id: str) -> GraphMessage | None:
        """One message, or `None` if it is no longer in the mailbox."""
        response = await self._request(
            "GET",
            f"{self._message_path(message_id)}",
            params={"$select": ",".join(MESSAGE_SELECT_FIELDS)},
            allow_missing=True,
        )
        if response.status_code == 404:
            return None
        return parse_message(_json(response))

    async def list_attachments(self, message_id: str) -> list[GraphAttachmentMetadata]:
        """Attachment metadata for one message. No bytes are transferred."""
        response = await self._request(
            "GET",
            f"{self._message_path(message_id)}/attachments",
            params={"$select": ",".join(ATTACHMENT_SELECT_FIELDS)},
            allow_missing=True,
        )
        if response.status_code == 404:
            return []

        rows = _json(response).get("value")
        if not isinstance(rows, list):
            return []
        return [parse_attachment(row) for row in rows if isinstance(row, dict)]

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """The raw bytes of one file attachment.

        `/$value` rather than the base64 `contentBytes` field: it streams the file
        itself, so nothing pays for a 33% base64 inflation on the way through.
        """
        response = await self._request(
            "GET",
            f"{self._message_path(message_id)}/attachments/{quote(attachment_id, safe='')}/$value",
        )
        return response.content

    # -- Writes --------------------------------------------------------------

    async def mark_as_read(self, message_id: str) -> bool:
        """Flag the message read. Returns False if it has since disappeared."""
        response = await self._request(
            "PATCH",
            self._message_path(message_id),
            json={"isRead": True},
            allow_missing=True,
        )
        if response.status_code == 404:
            return False
        logger.info("graph_message_marked_read", mailbox=self.mailbox, message_id=message_id)
        return True

    async def move_message(self, message_id: str, destination: str) -> str | None:
        """Move the message to a folder, returning its new id.

        Graph mints a *new* id for a moved message, which is exactly why the
        ledger records the id it was processed under before anything is moved.
        """
        response = await self._request(
            "POST",
            f"{self._message_path(message_id)}/move",
            json={"destinationId": destination},
            allow_missing=True,
        )
        if response.status_code == 404:
            return None
        moved_id = _json(response).get("id")
        logger.info(
            "graph_message_moved",
            mailbox=self.mailbox,
            message_id=message_id,
            destination=destination,
        )
        return moved_id if isinstance(moved_id, str) else None

    # -- Plumbing ------------------------------------------------------------

    def _mailbox_path(self) -> str:
        return f"/users/{quote(self.mailbox, safe='')}"

    def _folder_path(self, folder: str | None = None) -> str:
        name = folder or self._config.mail_folder
        return f"{self._mailbox_path()}/mailFolders/{quote(name, safe='')}"

    def _message_path(self, message_id: str) -> str:
        return f"{self._mailbox_path()}/messages/{quote(message_id, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allow_missing: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue an authenticated request, refreshing the token on a 401."""
        response = await self._send(method, path, **kwargs)

        if response.status_code == 401:
            self._tokens.invalidate()
            logger.info("graph_token_refreshing", method=method, path=path)
            response = await self._send(method, path, **kwargs)

        if response.status_code == 404 and allow_missing:
            return response
        if response.status_code >= 400:
            raise _as_error(response, method=method, path=path)
        return response

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = await self._tokens.token()
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            return await self._http.request(method, path, headers=headers, **kwargs)
        except ExternalServiceError as exc:
            # `HttpClient` has already retried transport errors and 5xx/429.
            logger.error("graph_request_failed", method=method, path=path, error=str(exc))
            raise GraphError(f"Microsoft Graph could not be reached: {exc.message}") from exc


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise GraphError("Microsoft Graph returned a response that was not JSON.") from exc
    if not isinstance(payload, dict):
        raise GraphError("Microsoft Graph returned a response of an unexpected shape.")
    return payload


def _as_error(response: httpx.Response, *, method: str, path: str) -> GraphError:
    """Turn a failed Graph response into the error the caller should see."""
    graph_code, detail = _error_detail(response)
    logger.error(
        "graph_response_rejected",
        method=method,
        path=path,
        status_code=response.status_code,
        graph_code=graph_code,
        detail=detail,
    )
    error_class = GraphAuthError if response.status_code in (401, 403) else GraphError
    return error_class(
        f"Microsoft Graph returned {response.status_code}: {detail}",
        status_code=response.status_code,
        graph_code=graph_code,
    )


def _error_detail(response: httpx.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, f"HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, f"HTTP {response.status_code}"
    code = error.get("code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        (message if isinstance(message, str) else f"HTTP {response.status_code}")[:300],
    )


_client: GraphMailClient | None = None


def get_mail_client(config: GraphSettings | None = None) -> GraphMailClient:
    """The process-wide mail client, built on first use.

    One client rather than one per request, so the HTTP connection pool and the
    cached token are shared — the same reasoning as `get_document_store`.
    """
    global _client
    if _client is None:
        _client = GraphMailClient(config)
        settings_used = _client._config
        # The effective sweep settings, logged once where the client is built.
        # A process reads its configuration at import and keeps it: a worker
        # started before an `.env` edit runs the *old* values for as long as it
        # lives, and the only way anyone found that out last time was by reading
        # 5,000 lines of poll output. One line at startup answers "what is this
        # process actually doing" without a debugger or a restart.
        logger.info(
            "graph_mail_client_created",
            mailbox=_client.mailbox,
            folder=settings_used.mail_folder,
            unread_only=settings_used.unread_only,
            batch_size=settings_used.batch_size,
            max_pages=settings_used.max_pages,
            lookback_minutes=settings_used.lookback_minutes,
            mark_as_read=settings_used.mark_as_read,
            move_to_folder=settings_used.move_to_folder,
            poll_interval_seconds=settings_used.poll_interval_seconds,
        )
    return _client


def set_mail_client(client: GraphMailClient | None) -> None:
    """Override the process client. Used by tests."""
    global _client
    _client = client


async def close_mail_client() -> None:
    """Close the process client, if one was ever built. Wired to shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
