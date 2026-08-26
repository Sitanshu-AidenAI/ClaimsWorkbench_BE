"""The handler directory: who a claim can be put on the desk of.

Separate from `app.services.claims` because it is about *people* rather than about
a claim. The assignment engine scores this directory; the claims module consumes
the answer.
"""

from app.services.handlers.directory import HandlerDirectoryService

__all__ = ["HandlerDirectoryService"]
