"""Data access.

One repository per aggregate. Services never issue queries themselves: a service
that reaches for `select()` is a service that cannot be tested without a
database, and a query written twice is two queries that drift.

Every repository takes an `AsyncSession` and leaves the transaction to its
caller. Committing inside a repository would make a multi-step operation — create
the claim, link the notice, write the audit event — three transactions that can
half-succeed.
"""

from __future__ import annotations

from app.repositories.audit import AuditRepository
from app.repositories.catastrophe import CatEventRepository
from app.repositories.claim import ClaimRepository
from app.repositories.fnol import FNOLRepository
from app.repositories.handler import HandlerRepository
from app.repositories.policy import PolicyRepository
from app.repositories.reference import ReferenceRepository

__all__ = [
    "AuditRepository",
    "CatEventRepository",
    "ClaimRepository",
    "FNOLRepository",
    "HandlerRepository",
    "PolicyRepository",
    "ReferenceRepository",
]
