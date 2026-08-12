"""Domain layer — business rules with no I/O.

Everything in this package is pure: enums, state machines, scoring functions and
the configuration they read. Nothing here touches the database, the network or
the request. That is what makes the rules testable on their own, and what stops
"a loss over half a million is a major loss" from being a number buried in a
controller.

Services in `app.services` orchestrate; repositories in `app.repositories`
persist; this package decides.
"""
