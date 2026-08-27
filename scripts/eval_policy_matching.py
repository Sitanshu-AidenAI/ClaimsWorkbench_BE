#!/usr/bin/env python
"""Score the policy matcher against `case_data/_ground_truth/MATCHING_GROUND_TRUTH.json`.

The answer key has shipped with the fixtures since they were generated and nothing
read it. This is the report; `tests/unit/test_matching_ground_truth.py` is the guard
that stops the numbers moving without somebody noticing.

Pure: no database, no model, no network. The matcher is Python over two JSON files.

    uv run python scripts/eval_policy_matching.py
    uv run python scripts/eval_policy_matching.py --failures-only
    uv run python scripts/eval_policy_matching.py --json

Exits 1 when a pack recommends a policy the key says is wrong, which is the one
outcome that would put a claim on somebody else's contract.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matching_eval import EvalReport, PackOutcome, evaluate


def _band(outcome: PackOutcome) -> str:
    return outcome.matched_confidence.value if outcome.matched_confidence else "-"


def _want_band(outcome: PackOutcome) -> str:
    return outcome.expected_confidence.value if outcome.expected_confidence else "no match"


def _mark(outcome: PackOutcome) -> str:
    if not outcome.recommendation_correct:
        return "FAIL"
    if not outcome.confidence_correct:
        return "band"
    return "ok"


def render(report: EvalReport, *, failures_only: bool) -> str:
    lines = [
        f"{'':4} {'pack':32} {'expected':18} {'recommended':18} {'want band':10} {'got band':10}",
        "-" * 100,
    ]
    for outcome in report.outcomes:
        if failures_only and _mark(outcome) == "ok":
            continue
        lines.append(
            f"{_mark(outcome):4} {outcome.pack:32} "
            f"{outcome.expected_policy_number or 'NO_MATCH':18} "
            f"{outcome.recommended_policy_number or '—':18} "
            f"{_want_band(outcome):10} {_band(outcome):10}"
        )

    lines += [
        "",
        f"packs                      {report.packs}",
        f"right policy top-ranked    {report.top_correct}/{report.packs} "
        f"({report.rate(report.top_correct):.0%})",
        f"recommendation as keyed    {report.recommendation_correct}/{report.packs} "
        f"({report.rate(report.recommendation_correct):.0%})",
        f"confidence band exact      {report.confidence_correct}/{report.packs} "
        f"({report.rate(report.confidence_correct):.0%})",
        "",
        "A `band` row named the right policy in the right place and put it in a "
        "different confidence band from the key.",
        "Some of that is the eval rather than the matcher: this harness feeds the field",
        "values the key states, which is a cleaner notice than the pack's own documents,",
        "so a pack keyed `possible` for a thin notice can legitimately score `strong` here.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failures-only", action="store_true", help="only rows that are not ok")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    report = evaluate()

    if args.json:
        print(
            json.dumps(
                {
                    "packs": report.packs,
                    "top_correct": report.top_correct,
                    "recommendation_correct": report.recommendation_correct,
                    "confidence_correct": report.confidence_correct,
                    "outcomes": [
                        {
                            "pack": outcome.pack,
                            "expected": outcome.expected_policy_number,
                            "expected_band": _want_band(outcome),
                            "recommended": outcome.recommended_policy_number,
                            "top": outcome.matched_policy_number,
                            "band": _band(outcome),
                            "score": outcome.score,
                            "status": outcome.status,
                        }
                        for outcome in report.outcomes
                    ],
                },
                indent=2,
            )
        )
    else:
        print(render(report, failures_only=args.failures_only))

    wrong = [
        outcome
        for outcome in report.outcomes
        if outcome.recommended_policy_number is not None
        and outcome.recommended_policy_number != outcome.expected_policy_number
    ]
    if wrong:
        print(
            "\nA policy the key does not name was recommended for: "
            + ", ".join(outcome.pack for outcome in wrong),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
