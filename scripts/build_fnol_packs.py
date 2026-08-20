#!/usr/bin/env python
"""Build the 24 FNOL notification packs in `case_data/`.

Each of the 24 notices in the matching dataset, told the way a claim actually
arrives: a broker's covering email, a completed loss notice, a specialist's
document, and a schedule of figures. The machinery is deliberately imported from
`build_demo_packs.py` rather than copied, so a pack here is the same shape as a
pack in `demo-data/` and a change to the page furniture reaches both.

    uv run python scripts/build_fnol_packs.py

**One deliberate divergence from `demo-data/`: no policy schedule in the pack.**
Those packs attach the policy document because they demonstrate extraction. These
demonstrate *identification* — the wording lives in `policy/` as the library and
the book is loaded by `scripts/load_policy_book.py`. Attaching the schedule would
hand the matcher the policy number and destroy the ten notices that deliberately
carry none.

**Every fact is invented.** Companies, people, policy numbers and addresses are
not real, and every email address uses a `.example` domain, which RFC 2606
reserves so it can never be registered.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_demo_packs as base  # noqa: E402
from build_demo_packs import (  # noqa: E402
    Scenario,
    build_loss_notice,
    build_report,
    build_schedule,
)

CASE_ROOT = Path(__file__).resolve().parent.parent / "case_data"
base.DEMO_ROOT = CASE_ROOT


def build_email(s: Scenario, expected: str) -> None:
    """The broker's covering note — short, because the detail is in the notice.

    A real covering email is a few lines and an attachment list; it is not a
    restatement of the form. The envelope matters as much as the body: the sender
    domain is the only source for the `broker_domain` signal, which no amount of
    document text can supply.
    """
    facts = "\n".join(f"{label}: {value}" for label, value in s.email_facts)
    attachments = "\n".join(
        f"  * {name}"
        for name in (
            "the completed loss notice",
            s.report_title.lower(),
            s.schedule_title.lower(),
        )
    )
    body = f"""From: {s.handler} <{s.handler_email}>
To: {s.email_to}
Cc: {s.email_cc}
Subject: {s.email_subject}
Date: {s.email_date}
Message-ID: <{s.email_message_id}>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

{s.email_opening}

{facts}

{s.email_narrative}

Attached:

{attachments}

{s.email_closing}

Kind regards,

{s.handler}
{s.handler_role}
{s.broker}
{s.handler_email}
{s.handler_phone}
"""
    (s.directory / "broker-notification.eml").write_text(body)


def build_readme(s: Scenario, expected: str, confidence: str, why: str,
                 exercises: tuple[str, ...]) -> None:
    facts = "\n".join(f"| {label} | {value} |" for label, value in (
        ("Line of business", s.line_of_business),
        ("Insured", s.insured),
        ("Broker", f"{s.broker} — {s.handler}, {s.handler_role}"),
        ("Policy number on the notice", f"`{s.policy_number}`" if s.policy_number else "*not stated*"),
        ("Broker reference", f"`{s.broker_reference}`"),
        ("Date of loss", f"{s.date_of_loss}, {s.time_of_loss}"),
        ("Loss location", s.loss_location),
        ("Cause", s.cause),
        ("Estimated loss", f"{s.currency} {s.estimated_loss}"),
    ))
    points = "\n".join(f"* {line}" for line in exercises)
    verdict = f"`{expected}`" if expected != "NO_MATCH" else "**NO_MATCH**"
    body = f"""# {s.title}

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
{facts}

## Expected matching result

| | |
|---|---|
| Expected policy | {verdict} |
| Confidence | {confidence} |

{why}

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
{s.notice_file:<32} 2-page completed loss notice
{s.report_file:<32} {s.report_title.lower()}
{s.schedule_file:<32} {s.schedule_title.lower()}
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

{points}
"""
    (s.directory / "README.md").write_text(body)


def build(spec: dict) -> None:
    s: Scenario = spec["scenario"]
    s.directory.mkdir(parents=True, exist_ok=True)
    build_loss_notice(s)
    build_report(s)
    build_schedule(s)
    build_email(s, spec["expected"])
    build_readme(s, spec["expected"], spec["confidence"], spec["why"], spec["exercises"])


def main() -> None:
    from fnol_scenarios import SPECS

    for spec in SPECS:
        build(spec)
        s = spec["scenario"]
        print(f"{s.slug}:  -> {spec['expected']} ({spec['confidence']})")
        for name in sorted(p.name for p in s.directory.iterdir()):
            print(f"    case_data/{s.slug}/{name}")
    print(f"\n{len(SPECS)} packs built.")


if __name__ == "__main__":
    main()
