"""Every beat interval is a setting with an environment name, and stays one.

This file exists to hold a rule rather than to test a behaviour. The intervals in
`celery_app.py` were literals — `300.0`, `3600.0`, `60.0` — and a literal there is
a schedule that cannot be read out of the environment, cannot be changed without a
code edit, and cannot be *seen*. The last of those is the expensive one: mailbox
intake went days without collecting, and the reason nobody could tell was that
nothing anywhere stated what the scheduler was actually doing.

So the test that matters most here is `test_no_schedule_is_a_hardcoded_literal`,
which reads the module's source. It will fail on the next literal somebody adds,
which is the only way a convention survives contact with a deadline.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.core.config import Settings
from app.workers.celery_app import SCHEDULE_ENV_NAMES, celery_app

MODULE = Path("app/workers/celery_app.py")


def schedule_call_sources() -> list[str]:
    """The source text of every `"schedule": ...` value in the module."""
    tree = ast.parse(MODULE.read_text())
    source = MODULE.read_text()
    values: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == "schedule":
                values.append(ast.get_source_segment(source, value) or "")
    return values


class TestTheRule:
    def test_no_schedule_is_a_hardcoded_literal(self) -> None:
        """The guard. A number here is a schedule nobody can see or change."""
        literals = [
            text
            for text in schedule_call_sources()
            if re.fullmatch(r"[0-9_]+(\.[0-9]+)?", text.strip())
        ]
        assert literals == [], (
            f"Hardcoded beat interval(s) {literals} in {MODULE}. Every schedule must "
            "come from a settings field so it has one definition and one CWB_* name — "
            "see the comment above `beat_schedule` and add the field to config.py."
        )

    def test_every_schedule_reads_from_settings(self) -> None:
        for text in schedule_call_sources():
            assert "settings." in text, f"{text!r} does not read from settings"

    def test_every_registered_entry_has_a_documented_env_name(self) -> None:
        """A schedule with no name in `SCHEDULE_ENV_NAMES` is invisible at startup."""
        missing = set(celery_app.conf.beat_schedule) - set(SCHEDULE_ENV_NAMES)
        assert missing == set(), (
            f"Beat entries {sorted(missing)} have no entry in SCHEDULE_ENV_NAMES, so "
            "the startup log cannot tell anyone which variable changes them."
        )

    def test_every_documented_env_name_is_real(self) -> None:
        """Guards against a typo that would print a variable nobody can set.

        A wrong name in the startup log is worse than no name: it sends somebody
        to edit a variable that does nothing, and they conclude the setting is
        broken rather than that the log is.
        """
        settings = Settings()
        blocks = {
            "CWB_CELERY_": settings.celery,
            "CWB_DOCINT_": settings.docint,
            "CWB_POLICY_": settings.policy_library,
            "CWB_GRAPH_": settings.graph,
        }
        for entry, env_name in SCHEDULE_ENV_NAMES.items():
            prefix = next((p for p in blocks if env_name.startswith(p)), None)
            assert prefix is not None, f"{env_name} ({entry}) has no known settings block"
            field = env_name[len(prefix) :].lower()
            assert hasattr(blocks[prefix], field), (
                f"{env_name} for {entry!r} maps to {field!r}, which does not exist on "
                f"the {prefix} settings block"
            )


class TestTheIntervalsThemselves:
    def test_every_registered_entry_matches_the_field_its_env_name_points_at(self) -> None:
        """The live schedule equals the setting it claims to read.

        This used to assert literals — `("poll-mail-intake", 8.0)` — with a
        docstring explaining that 8s was "this deployment's `.env` value". Two
        things were wrong with that. It made a test out of one laptop's
        configuration, so tuning the interval to 600s broke it while nothing was
        actually wrong. And the file whose entire purpose is to forbid literal
        intervals in `celery_app.py` was asserting literals of its own.

        Deriving the expected value from settings tests the thing that can really
        be wrong — an entry wired to the wrong field, which is exactly the
        "rewiring mistake" the old docstring claimed to catch and could not — and
        it stays true whatever the intervals are set to. It also covers every
        registered entry rather than three chosen ones, so a new schedule is
        checked the moment it is added.
        """
        settings = Settings()
        blocks = {
            "CWB_CELERY_": settings.celery,
            "CWB_DOCINT_": settings.docint,
            "CWB_POLICY_": settings.policy_library,
            "CWB_GRAPH_": settings.graph,
        }

        checked = 0
        for entry, definition in celery_app.conf.beat_schedule.items():
            schedule = definition.get("schedule")
            if not isinstance(schedule, (int, float)):
                continue
            env_name = SCHEDULE_ENV_NAMES.get(entry)
            if env_name is None:
                # Owned by `test_every_registered_entry_has_a_documented_env_name`.
                # One rule, one test: failing here too would report the same gap
                # twice and hide this one behind it.
                continue

            prefix = next(p for p in blocks if env_name.startswith(p))
            field = env_name[len(prefix) :].lower()
            expected = float(getattr(blocks[prefix], field))
            assert float(schedule) == expected, (
                f"{entry!r} is scheduled every {float(schedule)}s but {env_name} is "
                f"{expected}s — the entry is reading a different settings field from "
                "the one its environment name advertises, so changing that variable "
                "would appear to do nothing."
            )
            checked += 1

        assert checked, "no numeric beat entries were checked — the schedule is empty"

    def test_the_heartbeat_entry_is_not_named_after_its_interval(self) -> None:
        """It was `heartbeat-every-5-minutes`, which a settings change made a lie.

        A name that states a value is a second place that value is written down,
        and the second place is the one that goes stale.
        """
        assert "heartbeat" in celery_app.conf.beat_schedule
        assert not any(re.search(r"\d", name) for name in celery_app.conf.beat_schedule), (
            "a beat entry name contains a number — name entries for what they do"
        )
