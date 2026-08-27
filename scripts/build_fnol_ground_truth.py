#!/usr/bin/env python
"""Generate the matching answer key from the pack scenarios.

Read from `fnol_scenarios.SPECS` rather than maintained beside it, so the answer
key and the documents cannot drift apart: change a pack and the table changes with
it. Written to `case_data/_ground_truth/` so it is never uploaded with a pack —
`.json`, `.csv` and `.md` are all in `ALLOWED_EXTENSIONS`.

    uv run python scripts/build_fnol_ground_truth.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fnol_scenarios import SPECS

OUT = Path(__file__).resolve().parent.parent / "case_data" / "_ground_truth"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for spec in SPECS:
        s = spec["scenario"]
        rows.append(
            {
                "pack": s.slug,
                "title": s.title,
                "line_of_business": s.line_of_business,
                "policy_number_mentioned": "Yes" if s.policy_number else "No",
                "mentioned_policy_number": s.policy_number or None,
                "broker_reference": s.broker_reference,
                "expected_matched_policy_number": spec["expected"],
                "match_confidence": spec["confidence"],
                "insured_name": s.insured,
                "claimant": s.claimant,
                "date_of_loss": s.date_of_loss,
                "loss_location": s.loss_location,
                "cause_of_loss": s.cause,
                "estimated_loss": f"{s.currency} {s.estimated_loss}",
                "broker_domain": s.handler_email.split("@")[-1],
                "reasoning": spec["why"],
                "exercises": list(spec["exercises"]),
            }
        )

    (OUT / "MATCHING_GROUND_TRUTH.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    )

    flat = [
        {k: (v if not isinstance(v, list) else " | ".join(v)) for k, v in r.items()} for r in rows
    ]
    with (OUT / "MATCHING_GROUND_TRUTH.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
        w.writeheader()
        w.writerows(flat)

    md = [
        "# Matching ground truth",
        "",
        "Generated from `scripts/fnol_scenarios.py` by "
        "`scripts/build_fnol_ground_truth.py`. Do not edit by hand — edit the scenario "
        "and rebuild, so the packs and the answer key cannot drift apart.",
        "",
        "24 packs against the 12 policies in `../../policy/`.",
        "",
        "| Pack | Number on notice | Broker ref | Expected | Confidence |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        num = f"`{r['mentioned_policy_number']}`" if r["mentioned_policy_number"] else "—"
        exp = (
            f"`{r['expected_matched_policy_number']}`"
            if r["expected_matched_policy_number"] != "NO_MATCH"
            else "**NO_MATCH**"
        )
        md.append(
            f"| `{r['pack']}` | {num} | `{r['broker_reference']}` | {exp} "
            f"| {r['match_confidence']} |"
        )
    md += ["", "## Detail", ""]
    for r in rows:
        exp = r["expected_matched_policy_number"]
        md += [
            f"### {r['pack']}",
            "",
            f"**{r['title']}** — {r['line_of_business']}",
            "",
            "- **Policy number on the notice:** "
            + (
                f"`{r['mentioned_policy_number']}`"
                if r["mentioned_policy_number"]
                else "*not stated*"
            ),
            f"- **Broker reference:** `{r['broker_reference']}`",
            f"- **Insured:** {r['insured_name']}",
            f"- **Claimant:** {r['claimant']}",
            f"- **Date of loss:** {r['date_of_loss']}",
            f"- **Loss location:** {r['loss_location']}",
            f"- **Cause:** {r['cause_of_loss']}",
            f"- **Estimated loss:** {r['estimated_loss']}",
            f"- **Expected match:** `{exp}`",
            f"- **Confidence:** {r['match_confidence']}",
            "",
            f"{r['reasoning']}",
            "",
            "What it exercises:",
            "",
        ]
        md += [f"- {e}" for e in r["exercises"]]
        md.append("")
    (OUT / "MATCHING_GROUND_TRUTH.md").write_text("\n".join(md) + "\n")

    print(
        f"wrote {len(rows)} rows to case_data/_ground_truth/MATCHING_GROUND_TRUTH.{{json,csv,md}}"
    )


if __name__ == "__main__":
    main()
