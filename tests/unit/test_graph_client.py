"""The Microsoft Graph client: token handling, mapping, and what a failure means.

Every test here runs against `respx`, so the suite needs no tenant, no secret
and no network. The payloads are the shapes Graph actually returns, including
the ones with fields missing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.core.config import GraphSettings
from app.integrations.graph.auth import GraphTokenProvider
from app.integrations.graph.client import GraphMailClient, GraphNotConfiguredError
from app.integrations.graph.errors import GraphAuthError, GraphError
from app.integrations.graph.messages import parse_attachment, parse_message

AUTHORITY = "http://login.test"
API = "http://graph.test/v1.0"
MAILBOX = "claims@carrier.test"
TOKEN_URL = f"{AUTHORITY}/tenant-1/oauth2/v2.0/token"
MESSAGES_URL = f"{API}/users/claims%40carrier.test/mailFolders/inbox/messages"
MESSAGE_URL = f"{API}/users/claims%40carrier.test/messages/AAMk-1"


def graph_settings(**overrides: object) -> GraphSettings:
    values: dict[str, object] = {
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "shared_mailbox": MAILBOX,
        "authority": AUTHORITY,
        "api_base_url": API,
    }
    values.update(overrides)
    return GraphSettings(**values)  # type: ignore[arg-type]


def token_route(expires_in: int = 3600) -> respx.Route:
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "token-value", "expires_in": expires_in}
        )
    )


MESSAGE_PAYLOAD = {
    "id": "AAMk-1",
    "internetMessageId": "<abc123@broker.test>",
    "conversationId": "conv-9",
    "subject": "FNOL — flood damage at Unit 4",
    "from": {"emailAddress": {"name": "Priya Raman", "address": "priya@broker.test"}},
    "toRecipients": [{"emailAddress": {"name": "Claims", "address": MAILBOX}}],
    "ccRecipients": [{"emailAddress": {"address": "supervisor@broker.test"}}],
    "receivedDateTime": "2026-08-11T09:14:00Z",
    "hasAttachments": True,
    "isRead": False,
    "bodyPreview": "Please find attached the survey report",
    "body": {"contentType": "html", "content": "<p>Please find attached.</p>"},
}


class TestMessageMapping:
    def test_a_full_message_maps_every_field_the_notice_needs(self) -> None:
        message = parse_message(MESSAGE_PAYLOAD)

        assert message.message_id == "AAMk-1"
        assert message.internet_message_id == "<abc123@broker.test>"
        assert message.conversation_id == "conv-9"
        assert message.subject == "FNOL — flood damage at Unit 4"
        assert message.sender.address == "priya@broker.test"
        assert message.sender.as_text() == "Priya Raman <priya@broker.test>"
        assert [item.address for item in message.to_recipients] == [MAILBOX]
        assert [item.address for item in message.cc_recipients] == ["supervisor@broker.test"]
        assert message.received_at == datetime(2026, 8, 11, 9, 14, tzinfo=UTC)
        assert message.body_is_html is True
        assert message.has_attachments is True
        assert message.is_read is False

    def test_the_message_id_is_what_deduplication_keys_on(self) -> None:
        assert parse_message(MESSAGE_PAYLOAD).dedupe_key == "<abc123@broker.test>"

    def test_a_message_without_an_internet_id_falls_back_to_the_graph_id(self) -> None:
        payload = {**MESSAGE_PAYLOAD}
        del payload["internetMessageId"]

        # Namespaced, so it can never collide with a real Message-ID from the
        # HTTP email endpoint.
        assert parse_message(payload).dedupe_key == "graph:AAMk-1"

    def test_a_sparse_message_is_read_rather_than_refused(self) -> None:
        message = parse_message({"id": "AAMk-2"})

        assert message.subject == "(no subject)"
        assert message.body == ""
        assert message.sender.as_text() == ""
        assert message.to_recipients == []
        assert message.received_at.tzinfo is UTC

    def test_an_unparseable_timestamp_becomes_now_rather_than_an_error(self) -> None:
        message = parse_message({"id": "AAMk-3", "receivedDateTime": "the day before yesterday"})

        assert message.received_at.tzinfo is UTC

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        message = parse_message({"id": "AAMk-4", "receivedDateTime": "2026-08-11T09:14:00"})

        assert message.received_at == datetime(2026, 8, 11, 9, 14, tzinfo=UTC)

    def test_an_attachment_reports_its_kind_and_declared_size(self) -> None:
        attachment = parse_attachment(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "id": "att-1",
                "name": "survey-report.pdf",
                "contentType": "application/pdf",
                "size": 184_320,
                "isInline": False,
            }
        )

        assert attachment.is_file is True
        assert attachment.size_bytes == 184_320
        assert attachment.content_type == "application/pdf"

    def test_an_embedded_item_is_not_a_file(self) -> None:
        attachment = parse_attachment(
            {"@odata.type": "#microsoft.graph.itemAttachment", "id": "att-2", "name": "Forwarded"}
        )

        assert attachment.is_file is False


class TestTokenProvider:
    @respx.mock
    async def test_a_token_is_fetched_once_and_reused(self) -> None:
        route = token_route()
        provider = GraphTokenProvider(graph_settings())

        assert await provider.token() == "token-value"
        assert await provider.token() == "token-value"
        assert route.call_count == 1

        await provider.aclose()

    @respx.mock
    async def test_an_invalidated_token_is_fetched_again(self) -> None:
        route = token_route()
        provider = GraphTokenProvider(graph_settings())

        await provider.token()
        provider.invalidate()
        await provider.token()

        assert route.call_count == 2
        await provider.aclose()

    @respx.mock
    async def test_a_token_about_to_expire_is_not_reused(self) -> None:
        # `expires_in` inside the refresh margin means the cached token is
        # already considered spent.
        route = token_route(expires_in=30)
        provider = GraphTokenProvider(graph_settings())

        await provider.token()
        await provider.token()

        assert route.call_count == 2
        await provider.aclose()

    @respx.mock
    async def test_refused_credentials_raise_an_auth_error_naming_the_reason(self) -> None:
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                401,
                json={
                    "error": "invalid_client",
                    "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace…",
                },
            )
        )
        provider = GraphTokenProvider(graph_settings())

        with pytest.raises(GraphAuthError, match="AADSTS7000215"):
            await provider.token()

        await provider.aclose()

    async def test_a_provider_cannot_be_built_without_credentials(self) -> None:
        # An explicitly empty settings object, not the bare constructor: a
        # developer's own `.env` may have real Graph credentials in it, and this
        # test must stay meaningful either way.
        with pytest.raises(ValueError, match="tenant id"):
            GraphTokenProvider(GraphSettings(tenant_id=None, client_id=None, client_secret=None))


class TestMailClient:
    async def test_a_client_cannot_be_built_without_credentials(self) -> None:
        with pytest.raises(GraphNotConfiguredError):
            GraphMailClient(
                GraphSettings(
                    tenant_id=None, client_id=None, client_secret=None, shared_mailbox=None
                )
            )

    @respx.mock
    async def test_listing_asks_only_for_unread_where_that_is_switched_on(self) -> None:
        token_route()
        route = respx.get(MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": [MESSAGE_PAYLOAD]})
        )
        client = GraphMailClient(graph_settings(unread_only=True))

        messages = await client.list_messages()

        assert [message.message_id for message in messages] == ["AAMk-1"]
        request = route.calls[0].request
        assert request.url.params["$filter"] == "isRead eq false"
        assert request.url.params["$orderby"] == "receivedDateTime asc"
        assert request.headers["Authorization"] == "Bearer token-value"
        await client.aclose()

    @respx.mock
    async def test_the_default_sweep_does_not_filter_on_the_read_flag(self) -> None:
        """The default, and the point of the default.

        `isRead` is cleared by anyone who opens the shared mailbox in Outlook.
        A sweep that filtered on it dropped those messages permanently while
        reporting a successful poll, which is the outage this default prevents.
        """
        token_route()
        route = respx.get(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        client = GraphMailClient(graph_settings())

        await client.list_messages()

        assert "$filter" not in route.calls[0].request.url.params
        await client.aclose()

    @respx.mock
    async def test_a_read_message_is_still_collected(self) -> None:
        token_route()
        respx.get(MESSAGES_URL).mock(
            return_value=httpx.Response(200, json={"value": [{**MESSAGE_PAYLOAD, "isRead": True}]})
        )
        client = GraphMailClient(graph_settings())

        messages = await client.list_messages()

        assert [message.message_id for message in messages] == ["AAMk-1"]
        assert messages[0].is_read is True
        await client.aclose()

    @respx.mock
    async def test_a_watermark_is_sent_as_a_received_after_filter(self) -> None:
        token_route()
        route = respx.get(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        client = GraphMailClient(graph_settings())

        await client.list_messages(since=datetime(2026, 8, 20, 9, 30, 0, 500, tzinfo=UTC))

        # To the second, with a literal Z: Graph rejects the microseconds and the
        # `+00:00` offset that `isoformat` produces by default.
        assert (
            route.calls[0].request.url.params["$filter"]
            == "receivedDateTime ge 2026-08-20T09:30:00Z"
        )
        await client.aclose()

    @respx.mock
    async def test_the_watermark_and_the_unread_filter_combine(self) -> None:
        token_route()
        route = respx.get(MESSAGES_URL).mock(return_value=httpx.Response(200, json={"value": []}))
        client = GraphMailClient(graph_settings(unread_only=True))

        await client.list_messages(since=datetime(2026, 8, 20, 9, 30, tzinfo=UTC))

        assert route.calls[0].request.url.params["$filter"] == (
            "isRead eq false and receivedDateTime ge 2026-08-20T09:30:00Z"
        )
        await client.aclose()

    @respx.mock
    async def test_a_folder_larger_than_one_page_is_read_to_the_end(self) -> None:
        """The regression that lost a live claim.

        With `$top` at the batch size, an ascending sort and no paging, a folder
        holding one more message than a page returned the same oldest page to
        every poll and the newest message was never in it. Nothing about that is
        visible from outside: the poll succeeds, on messages already collected.
        """
        token_route()
        next_link = f"{MESSAGES_URL}?%24skiptoken=page-2"
        respx.get(MESSAGES_URL, params__contains={"$orderby": "receivedDateTime asc"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [{**MESSAGE_PAYLOAD, "id": "AAMk-old"}],
                    "@odata.nextLink": next_link,
                },
            )
        )
        respx.get(next_link).mock(
            return_value=httpx.Response(
                200, json={"value": [{**MESSAGE_PAYLOAD, "id": "AAMk-new"}]}
            )
        )
        client = GraphMailClient(graph_settings(batch_size=1))

        messages = await client.list_messages()

        assert [message.message_id for message in messages] == ["AAMk-old", "AAMk-new"]
        await client.aclose()

    @respx.mock
    async def test_paging_stops_at_the_configured_page_ceiling(self) -> None:
        token_route()
        # Every page offers another; only `max_pages` of them are followed.
        respx.get(url__startswith=MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [MESSAGE_PAYLOAD],
                    "@odata.nextLink": f"{MESSAGES_URL}?%24skiptoken=more",
                },
            )
        )
        client = GraphMailClient(graph_settings(batch_size=1, max_pages=3))

        messages = await client.list_messages(limit=100)

        assert len(messages) == 3
        await client.aclose()

    @respx.mock
    async def test_an_explicit_limit_caps_what_paging_returns(self) -> None:
        token_route()
        respx.get(url__startswith=MESSAGES_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [MESSAGE_PAYLOAD, MESSAGE_PAYLOAD],
                    "@odata.nextLink": f"{MESSAGES_URL}?%24skiptoken=more",
                },
            )
        )
        client = GraphMailClient(graph_settings(batch_size=2, max_pages=10))

        messages = await client.list_messages(limit=3)

        assert len(messages) == 3
        await client.aclose()

    @respx.mock
    async def test_folder_counts_are_read_for_reconciliation(self) -> None:
        token_route()
        respx.get(f"{API}/users/claims%40carrier.test/mailFolders/inbox").mock(
            return_value=httpx.Response(200, json={"totalItemCount": 24, "unreadItemCount": 0})
        )
        client = GraphMailClient(graph_settings())

        assert await client.folder_stats() == (24, 0)
        await client.aclose()

    @respx.mock
    async def test_a_folder_that_is_gone_reconciles_to_zero(self) -> None:
        token_route()
        respx.get(f"{API}/users/claims%40carrier.test/mailFolders/inbox").mock(
            return_value=httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        )
        client = GraphMailClient(graph_settings())

        assert await client.folder_stats() == (0, 0)
        await client.aclose()

    @respx.mock
    async def test_attachment_metadata_is_requested_without_its_bytes(self) -> None:
        token_route()
        route = respx.get(f"{MESSAGE_URL}/attachments").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.fileAttachment",
                            "id": "att-1",
                            "name": "report.pdf",
                            "contentType": "application/pdf",
                            "size": 2048,
                        }
                    ]
                },
            )
        )
        client = GraphMailClient(graph_settings())

        attachments = await client.list_attachments("AAMk-1")

        assert [item.name for item in attachments] == ["report.pdf"]
        assert "contentBytes" not in route.calls[0].request.url.params["$select"]
        await client.aclose()

    @respx.mock
    async def test_an_attachment_downloads_as_raw_bytes(self) -> None:
        token_route()
        respx.get(f"{MESSAGE_URL}/attachments/att-1/$value").mock(
            return_value=httpx.Response(200, content=b"%PDF-1.7 body")
        )
        client = GraphMailClient(graph_settings())

        assert await client.download_attachment("AAMk-1", "att-1") == b"%PDF-1.7 body"
        await client.aclose()

    @respx.mock
    async def test_a_stale_token_is_refreshed_and_the_request_retried_once(self) -> None:
        token = token_route()
        messages = respx.get(MESSAGES_URL).mock(
            side_effect=[
                httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}),
                httpx.Response(200, json={"value": []}),
            ]
        )
        client = GraphMailClient(graph_settings())

        assert await client.list_messages() == []
        assert messages.call_count == 2
        assert token.call_count == 2  # the second one is the refresh
        await client.aclose()

    @respx.mock
    async def test_a_second_401_is_a_credentials_problem_and_is_raised(self) -> None:
        token_route()
        respx.get(MESSAGES_URL).mock(
            return_value=httpx.Response(
                401, json={"error": {"code": "InvalidAuthenticationToken", "message": "Expired."}}
            )
        )
        client = GraphMailClient(graph_settings())

        with pytest.raises(GraphAuthError, match="Expired"):
            await client.list_messages()

        await client.aclose()

    @respx.mock
    async def test_a_forbidden_mailbox_names_the_graph_code(self) -> None:
        token_route()
        respx.get(MESSAGES_URL).mock(
            return_value=httpx.Response(
                403,
                json={
                    "error": {
                        "code": "ErrorAccessDenied",
                        "message": "Access to OData is disabled.",
                    }
                },
            )
        )
        client = GraphMailClient(graph_settings())

        with pytest.raises(GraphAuthError) as caught:
            await client.list_messages()

        assert caught.value.graph_code == "ErrorAccessDenied"
        assert caught.value.upstream_status == 403
        await client.aclose()

    @respx.mock
    async def test_a_message_deleted_mid_poll_is_not_an_error(self) -> None:
        token_route()
        respx.patch(MESSAGE_URL).mock(
            return_value=httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
        )
        client = GraphMailClient(graph_settings())

        assert await client.mark_as_read("AAMk-1") is False
        await client.aclose()

    @respx.mock
    async def test_marking_read_sets_the_flag_and_nothing_else(self) -> None:
        token_route()
        route = respx.patch(MESSAGE_URL).mock(return_value=httpx.Response(200, json={"id": "1"}))
        client = GraphMailClient(graph_settings())

        assert await client.mark_as_read("AAMk-1") is True
        assert json.loads(route.calls[0].request.read()) == {"isRead": True}
        await client.aclose()

    @respx.mock
    async def test_moving_returns_the_new_id_graph_mints(self) -> None:
        token_route()
        respx.post(f"{MESSAGE_URL}/move").mock(
            return_value=httpx.Response(201, json={"id": "AAMk-moved"})
        )
        client = GraphMailClient(graph_settings())

        assert await client.move_message("AAMk-1", "processed") == "AAMk-moved"
        await client.aclose()

    @respx.mock
    async def test_an_unreachable_graph_becomes_a_graph_error(self) -> None:
        token_route()
        respx.get(MESSAGES_URL).mock(side_effect=httpx.ConnectError("no route to host"))
        client = GraphMailClient(graph_settings())

        with pytest.raises(GraphError, match="could not be reached"):
            await client.list_messages()

        await client.aclose()
