"""The role-to-capability grants an administrator edits.

The authority for what a persona may reach. `app.domain.capabilities` holds the
documented default and seeds this table; from then on this is what
`app.services.access` reads and what the Access control board writes.

One row per granted pair, and **absence is a denial**. A three-state column
(granted / denied / unset) was the alternative and buys nothing: the matrix is a
grid of checkboxes, and "unset" would only ever be rendered as unchecked while
adding a state every reader has to reason about.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RoleCapability(Base):
    """One capability granted to one realm role."""

    __tablename__ = "role_capabilities"

    #: The realm role, hyphenated exactly as Keycloak issues it. Not a foreign key
    #: to anything: Keycloak owns the role list, and a grant for a role that has
    #: since been deleted there is inert rather than broken.
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: A member of `app.domain.capabilities.Capability`. Stored as its string so a
    #: capability the code no longer knows is ignored rather than failing a query.
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)

    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: The token subject of whoever granted it. Kept because an authorisation change
    #: is exactly the kind of edit somebody asks "who did this" about later.
    granted_by: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (Index("ix_role_capabilities_role", "role"),)
