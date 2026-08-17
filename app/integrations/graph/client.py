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
    ) -> list[GraphMessage]:
        """A page of messages from the configured folder, oldest first.

        Oldest first on purpose: notifications are worked in the order a claims
        team would have opened them, and a poll that always read the newest page
        would starve the bottom of a backlog indefinitely.

        One page only. `@odata.nextLink` is not followed — the batch size bounds
        what one poll may do, and the next poll picks up where this one stopped.
        """
        unread = self._config.unread_only if unread_only is None else unread_only
        params: dict[str, Any] = {
            "$select": ",".join(MESSAGE_SELECT_FIELDS),
            "$top": limit or self._config.batch_size,
            "$orderby": "receivedDateTime asc",
        }
        if unread:
            params["$filter"] = "isRead eq false"

        path = f"{self._folder_path(folder)}/messages"
        response = await self._request("GET", path, params=params)
        payload = _json(response)
        rows = payload.get("value")
        if not isinstance(rows, list):
            raise GraphError("The message list response carried no `value` array.")

        messages = [parse_message(row) for row in rows if isinstance(row, dict)]
        logger.info(
            "graph_messages_listed",
            mailbox=self.mailbox,
            folder=folder or self._config.mail_folder,
            unread_only=unread,
            count=len(messages),
        )
        return messages

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
        logger.info("graph_mail_client_created", mailbox=_client.mailbox)
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
