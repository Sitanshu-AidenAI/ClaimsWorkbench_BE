"""Shared-mailbox intake for FNOL.

`intake` orchestrates one poll; `runner` assembles it for a worker or a route.
Microsoft-specific code lives in `app.integrations.graph` and does not appear
here beyond the client this package is handed.
"""

from __future__ import annotations

from app.services.mail.intake import INTAKE_ACTOR, MailIntakeService, MailIntakeSummary
from app.services.mail.runner import build_mail_intake_service, run_mail_intake

__all__ = [
    "INTAKE_ACTOR",
    "MailIntakeService",
    "MailIntakeSummary",
    "build_mail_intake_service",
    "run_mail_intake",
]
