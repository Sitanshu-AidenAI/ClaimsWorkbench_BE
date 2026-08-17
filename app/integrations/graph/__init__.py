"""Microsoft Graph.

`auth` mints application tokens, `messages` maps Graph's JSON onto dataclasses,
and `client` is the only thing that speaks HTTP. Nothing in this package knows
what a claim is — see `app.services.mail` for what intake does with a message.
"""

from __future__ import annotations

from app.integrations.graph.auth import GraphTokenProvider
from app.integrations.graph.client import (
    GraphMailClient,
    GraphNotConfiguredError,
    close_mail_client,
    get_mail_client,
    set_mail_client,
)
from app.integrations.graph.errors import GraphAuthError, GraphError
from app.integrations.graph.messages import (
    GraphAttachmentMetadata,
    GraphMessage,
    GraphRecipient,
    parse_attachment,
    parse_message,
)

__all__ = [
    "GraphAttachmentMetadata",
    "GraphAuthError",
    "GraphError",
    "GraphMailClient",
    "GraphMessage",
    "GraphNotConfiguredError",
    "GraphRecipient",
    "GraphTokenProvider",
    "close_mail_client",
    "get_mail_client",
    "parse_attachment",
    "parse_message",
    "set_mail_client",
]
