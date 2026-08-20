"""What a persona may reach, and the default answer for each.

Roles say who somebody is; capabilities say what that lets them do. Keeping the two
apart is what makes the matrix configurable: a deployment can decide that claims
handlers reach the connectors board without anyone editing Python, and the roles
Keycloak issues do not change at all.

**This module is the seed and the fallback, not the authority.** The authority is
`role_capabilities` in Postgres, which an administrator edits. `DEFAULT_MATRIX` is
what that table is seeded with on first boot, and what
`app.services.access` falls back to if the table is empty — because a claims desk
whose grant table failed to seed should be a desk with the documented defaults, not
a desk where nobody can reach anything.

The matrix has five persona columns and `claims-admin` is the administrator. The
realm still carries `business-admin` from before the persona model was settled; it is
aliased onto the administrator rather than given a column of its own, so holders keep
working and there is still exactly one admin persona.
"""

from __future__ import annotations

from enum import StrEnum

from app.domain.enums import Role


class Capability(StrEnum):
    """One thing a persona may reach.

    Prefixed by kind so the matrix can group them and so a later action capability
    (`claim:settle`) reads distinctly from a page one. Values are the strings the
    wire and the database use, so renaming one is a migration.
    """

    # Pages, in the order the rail lists them.
    PAGE_CLAIMS = "page:claims"
    PAGE_INTAKE = "page:intake"
    PAGE_INSPECTION = "page:inspection"
    PAGE_POLICIES = "page:policies"
    PAGE_APPROVALS = "page:approvals"
    PAGE_INTELLIGENCE = "page:intelligence"
    PAGE_TEAM = "page:team"
    PAGE_ANALYTICS = "page:analytics"
    PAGE_ASSISTANT = "page:assistant"
    PAGE_ADMIN = "page:admin"
    PAGE_CONNECTORS = "page:connectors"
    PAGE_SETTINGS = "page:settings"


#: How each capability reads on screen, and which block of the matrix it sits in.
#:
#: Held here rather than in the frontend because the matrix is server-driven: a
#: board that enumerated these itself would silently omit whatever a later release
#: adds, and on an authorisation surface that means an administrator believing they
#: have seen the whole picture when they have not.
CAPABILITY_LABELS: dict[Capability, str] = {
    Capability.PAGE_CLAIMS: "Claims queue",
    Capability.PAGE_INTAKE: "FNOL intake",
    Capability.PAGE_INSPECTION: "Field inspection",
    Capability.PAGE_POLICIES: "Policies",
    Capability.PAGE_APPROVALS: "Approval queue",
    Capability.PAGE_INTELLIGENCE: "Claim intelligence",
    Capability.PAGE_TEAM: "Claims team",
    Capability.PAGE_ANALYTICS: "Analytics",
    Capability.PAGE_ASSISTANT: "AI assistant",
    Capability.PAGE_ADMIN: "Admin studio",
    Capability.PAGE_CONNECTORS: "External connectors",
    Capability.PAGE_SETTINGS: "Settings",
}

#: The matrix's row groups, in render order. The ids are the rail's own groups, so
#: the board reads in the same order as the navigation it governs.
CAPABILITY_GROUPS: list[tuple[str, str, tuple[Capability, ...]]] = [
    (
        "operate",
        "Operate",
        (
            Capability.PAGE_CLAIMS,
            Capability.PAGE_INTAKE,
            Capability.PAGE_INSPECTION,
            Capability.PAGE_POLICIES,
        ),
    ),
    (
        "oversee",
        "Oversee",
        (
            Capability.PAGE_APPROVALS,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_TEAM,
            Capability.PAGE_ANALYTICS,
            Capability.PAGE_ASSISTANT,
        ),
    ),
    (
        "administer",
        "Administer",
        (
            Capability.PAGE_ADMIN,
            Capability.PAGE_CONNECTORS,
            Capability.PAGE_SETTINGS,
        ),
    ),
]

#: The personas the matrix has a column for, in render order. Administrator first,
#: because its column states a fact rather than a choice.
PERSONAS: tuple[Role, ...] = (
    Role.CLAIMS_ADMIN,
    Role.CLAIMS_MANAGER,
    Role.CLAIMS_HANDLER,
    Role.LOSS_ADJUSTER,
    Role.FNOL_OFFICER,
)

PERSONA_LABELS: dict[Role, str] = {
    Role.CLAIMS_ADMIN: "Administrator",
    Role.CLAIMS_MANAGER: "Claims manager",
    Role.CLAIMS_HANDLER: "Claims handler",
    Role.LOSS_ADJUSTER: "Loss adjuster",
    Role.FNOL_OFFICER: "FNOL officer",
}

#: The administrator, whose grants are not editable.
#:
#: Not a special case in the enforcement path — the seed simply gives this role
#: every capability, and `require_capability` treats it like any other. What is
#: special is that the *matrix* refuses to change it: a deployment that could
#: revoke the administrator's access to Admin Studio could lock every person out of
#: the board that would let them undo it.
ADMINISTRATOR: Role = Role.CLAIMS_ADMIN

#: The documented default for each persona. Seeded into `role_capabilities`, and the
#: fallback when that table is empty.
#:
#: Two entries are worth a note because they are decisions rather than obvious:
#:
#: * **Claims handler reaches External connectors.** Narrower gates argued this was
#:   administrator-only because the credentials it holds open a path into the claims
#:   core. Granting it here was confirmed deliberately.
#: * **Claims manager reaches the Claims queue.** The persona list did not name it,
#:   but a manager who supervises a queue they cannot open is not supervising it.
DEFAULT_MATRIX: dict[Role, frozenset[Capability]] = {
    ADMINISTRATOR: frozenset(Capability),
    # `business-admin` predates the persona model and is still granted in the realm
    # and to the seeded `demo` user. It maps onto the administrator persona rather
    # than becoming a sixth column: the decision was five personas with one
    # administrator, and dropping the role from the enum would break the realm's
    # existing grants and `EXTRACTION_ADMIN_ROLES` at the same time. Removing it from
    # the realm is tidy-up for later, and until then holders keep working.
    Role.BUSINESS_ADMIN: frozenset(Capability),
    Role.CLAIMS_MANAGER: frozenset(
        {
            Capability.PAGE_CLAIMS,
            Capability.PAGE_POLICIES,
            Capability.PAGE_APPROVALS,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_TEAM,
            Capability.PAGE_ANALYTICS,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_ADMIN,
            Capability.PAGE_CONNECTORS,
            Capability.PAGE_SETTINGS,
        }
    ),
    Role.CLAIMS_HANDLER: frozenset(
        {
            Capability.PAGE_CLAIMS,
            Capability.PAGE_POLICIES,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_CONNECTORS,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    ),
    Role.LOSS_ADJUSTER: frozenset(
        {
            Capability.PAGE_INSPECTION,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    ),
    Role.FNOL_OFFICER: frozenset(
        {
            Capability.PAGE_INTAKE,
            Capability.PAGE_POLICIES,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    ),
}


def capabilities_for(roles: frozenset[str] | set[str]) -> frozenset[Capability]:
    """The union of what these roles allow, from the documented defaults.

    A union rather than a precedence order: somebody holding two personas can do
    what either allows, which is what "and also" means when an administrator grants
    a second role. The alternative — a ranking — would make a manager who also holds
    the officer role *lose* intake, which nobody would predict.

    Used by the seed and as the fallback; the live answer comes from
    `app.services.access.AccessService`, which reads the table.
    """
    granted: set[Capability] = set()
    for role in roles:
        try:
            granted |= DEFAULT_MATRIX[Role(role)]
        except ValueError:
            # A realm role this application does not model — `offline_access`, or one
            # an administrator added for another system. It grants nothing here, and
            # that is not an error worth raising on a request path.
            continue
    return frozenset(granted)


__all__ = [
    "ADMINISTRATOR",
    "CAPABILITY_GROUPS",
    "CAPABILITY_LABELS",
    "DEFAULT_MATRIX",
    "PERSONAS",
    "PERSONA_LABELS",
    "Capability",
    "capabilities_for",
]
