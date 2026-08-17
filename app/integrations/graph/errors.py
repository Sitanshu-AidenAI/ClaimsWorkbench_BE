"""Failures that come back from Microsoft Graph.

Both are `ExternalServiceError`s, so a route that lets one escape renders the
project's one error shape with a 502 rather than a stack trace. The distinction
between them is who has to fix it: `GraphAuthError` is a wrong secret, a missing
consent or an expired registration — a person's job — while `GraphError` is a
request that failed and may well succeed on the next poll.
"""

from __future__ import annotations

from app.core.errors import ExternalServiceError


class GraphError(ExternalServiceError):
    """Graph returned something the caller cannot use."""

    code = "graph_error"
    message = "Microsoft Graph returned an unexpected response."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        graph_code: str | None = None,
    ) -> None:
        #: The upstream HTTP status, kept separate from `self.status_code`, which
        #: is what *this* API answers with.
        self.upstream_status = status_code
        self.graph_code = graph_code
        super().__init__(
            message,
            details={
                key: value
                for key, value in (("status", status_code), ("graph_code", graph_code))
                if value
            },
        )


class GraphAuthError(GraphError):
    """A token could not be obtained, or the one obtained was refused."""

    code = "graph_auth_error"
    message = "Microsoft Graph refused the application's credentials."
