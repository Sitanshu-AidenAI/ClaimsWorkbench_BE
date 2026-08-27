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

import pytest

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
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ("heartbeat", 300.0),
            ("poll-mail-intake", 8.0),
            ("prune-mail-intake-runs", 86400.0),
        ],
    )
    def test_the_effective_schedule_matches_the_settings(self, entry: str, expected: float) -> None:
        """Reads the live schedule, so a rewiring mistake shows up as a wrong number.

        `poll-mail-intake` at 8s is this deployment's `.env` value, not the 300s
        default — which is exactly the distinction that matters: the default in
        `config.py` is not what the process is running.
        """
        if entry not in celery_app.conf.beat_schedule:
            pytest.skip(f"{entry} is not registered in this environment")
        assert float(celery_app.conf.beat_schedule[entry]["schedule"]) == expected

    def test_the_heartbeat_entry_is_not_named_after_its_interval(self) -> None:
        """It was `heartbeat-every-5-minutes`, which a settings change made a lie.

        A name that states a value is a second place that value is written down,
        and the second place is the one that goes stale.
        """
        assert "heartbeat" in celery_app.conf.beat_schedule
        assert not any(re.search(r"\d", name) for name in celery_app.conf.beat_schedule), (
            "a beat entry name contains a number — name entries for what they do"
        )
