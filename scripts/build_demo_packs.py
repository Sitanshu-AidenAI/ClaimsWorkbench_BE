#!/usr/bin/env python
"""Build the demo notification packs.

Five claims, each told the way one actually arrives: a broker's covering email
with a completed loss notice, the policy document it is made under, a
specialist's report, and a schedule of figures. Five different classes of
business, so the extraction is exercised against five different vocabularies
rather than five copies of one.

    uv run python scripts/build_demo_packs.py

Each pack is written to `demo-data/<slug>/` with its own README. The PDFs are
committed so a demo runs with no build step; this script is committed beside them
so they are reviewable and correctable rather than opaque binaries.

**Every fact is invented.** The companies, the people, the policy numbers, the
vehicle registrations and the addresses are not real. Every email address uses a
`.example` domain, which RFC 2606 reserves so it can never be registered. Nothing
here is derived from a real claim, a real client or a real person.

## What each pack is built to exercise

Every pack keeps the same three properties, because they are what the review
screen is checked against:

* **Fields split across pages.** The loss notice puts the policy and the people
  on page 1 and the loss and the money on page 2. A citation that opens the right
  page is doing something a whole-document answer cannot.
* **The same fact in two vocabularies.** The policy document says "Assured" or
  "Policyholder" where the notice says "Insured name". That is what the dataset's
  aliases are for.
* **A figure that appears twice.** The specialist's report states the quantum on
  its last page and the covering email repeats it. A citation naming the report is
  demonstrably the passage that was read, not the first match in the case file.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from demo_pdf import footer, header, new_canvas, pair, paragraph, section

DEMO_ROOT = Path(__file__).resolve().parent.parent / "demo-data"

Rows = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Scenario:
    """One claim, stated once and drawn into every document of its pack."""

    slug: str
    #: For the README and the pack listing.
    title: str
    line_of_business: str

    # -- Who is telling us ---------------------------------------------------
    broker: str
    handler: str
    handler_role: str
    handler_email: str
    handler_phone: str
    broker_reference: str
    reported_on: str

    # -- Under what policy ---------------------------------------------------
    insured: str
    policy_number: str
    policy_type: str
    policy_period: str
    policy_limit: str
    policy_deductible: str
    #: What the policy document calls the insured. Deliberately not "Insured
    #: name" — a schedule saying "Assured" is the normal case, not an edge one.
    policy_insured_label: str
    policy_number_label: str
    policy_title: str
    policy_subtitle: str
    policy_interest_label: str
    policy_interest: str
    policy_conditions: str
    policy_extra: Rows

    # -- Who is claiming -----------------------------------------------------
    claimant: str
    claimant_contact: str
    claimant_email: str

    # -- What happened -------------------------------------------------------
    date_of_loss: str
    time_of_loss: str
    loss_location: str
    loss_country: str
    cause: str
    description: str
    affected_assets: str
    injuries: str
    fatalities: str
    business_interruption: str
    structural_damage: str
    environmental_exposure: str
    authorities: str
    potential_litigation: str
    incident_reference: str
    police_reference: str | None

    #: The subject of the claim — a vehicle, a building, a project, a system.
    #: Drawn on page 2 of the notice and on page 1 of the report.
    asset_heading: str
    asset_rows: Rows

    # -- What it is thought to cost -----------------------------------------
    currency: str
    estimated_loss: str
    repair_estimate: str
    #: What the schedule's figures are called on the report's quantum page.
    repair_estimate_label: str

    # -- The specialist's report --------------------------------------------
    report_slug: str
    report_title: str
    report_firm: str
    report_reference: str
    report_author: str
    report_instructions: Rows
    report_circumstances: str
    report_findings: str
    report_quantum: Rows
    report_comment_heading: str
    report_comment: str
    report_recommendation: str

    # -- The schedule of figures --------------------------------------------
    schedule_slug: str
    schedule_title: str
    schedule_headers: tuple[str, ...]
    schedule_rows: tuple[tuple[str, ...], ...]
    schedule_total_label: str

    # -- The covering email --------------------------------------------------
    email_subject: str
    email_to: str
    email_cc: str
    email_date: str
    email_message_id: str
    email_opening: str
    email_narrative: str
    email_closing: str

    #: Extra `Label: value` lines quoted in the email body. The email is the one
    #: document a broker writes freehand, so it restates a handful of figures —
    #: which is exactly why a citation must not simply find the first match.
    email_facts: Rows = field(default_factory=tuple)

    @property
    def directory(self) -> Path:
        return DEMO_ROOT / self.slug

    @property
    def notice_file(self) -> str:
        return f"{self.slug}-loss-notice.pdf"

    @property
    def policy_file(self) -> str:
        return f"{self.slug}-policy-schedule.pdf"

    @property
    def report_file(self) -> str:
        return f"{self.report_slug}.pdf"

    @property
    def schedule_file(self) -> str:
        return f"{self.schedule_slug}.csv"

    @property
    def schedule_total(self) -> int:
        """The sum of the schedule's line totals, computed rather than typed."""
        return sum(int(row[-1]) for row in self.schedule_rows)


# ---------------------------------------------------------------------------
# The documents
# ---------------------------------------------------------------------------


def build_loss_notice(scenario: Scenario) -> None:
    """The completed claim form, split across two pages on purpose."""
    pdf = new_canvas(str(scenario.directory / scenario.notice_file), "First Notification of Loss")
    running = f"{scenario.broker} · {scenario.broker_reference}"

    # --- Page 1: who is telling us, and under what policy --------------------
    y = header(
        pdf,
        "FIRST NOTIFICATION OF LOSS",
        f"Submitted by {scenario.broker} · Broker reference {scenario.broker_reference}",
    )

    y = section(pdf, y, "NOTIFICATION")
    y = pair(pdf, y, "Reported by", scenario.handler)
    y = pair(pdf, y, "Reporting organisation", scenario.broker)
    y = pair(pdf, y, "Role", scenario.handler_role)
    y = pair(pdf, y, "Email", scenario.handler_email)
    y = pair(pdf, y, "Telephone", scenario.handler_phone)
    y = pair(pdf, y, "Date reported", scenario.reported_on)
    y -= 12

    y = section(pdf, y, "POLICY")
    y = pair(pdf, y, "Policy number", scenario.policy_number)
    y = pair(pdf, y, "Insured name", scenario.insured)
    y = pair(pdf, y, "Insured organisation", scenario.insured)
    y = pair(pdf, y, "Policy type", scenario.policy_type)
    y = pair(pdf, y, "Period of cover", scenario.policy_period)
    y -= 12

    y = section(pdf, y, "CLAIMANT")
    y = pair(pdf, y, "Claimant name", scenario.claimant)
    y = pair(pdf, y, "Claimant contact", scenario.claimant_contact)
    y = pair(pdf, y, "Claimant email", scenario.claimant_email)

    footer(pdf, running, 1, 2)
    pdf.showPage()

    # --- Page 2: what happened, and what it is thought to cost ---------------
    y = header(
        pdf,
        "FIRST NOTIFICATION OF LOSS (CONTINUED)",
        f"Policy {scenario.policy_number} · {scenario.insured}",
    )

    y = section(pdf, y, "THE LOSS")
    y = pair(pdf, y, "Date of loss", scenario.date_of_loss)
    y = pair(pdf, y, "Time of loss", scenario.time_of_loss)
    y = pair(pdf, y, "Loss location", scenario.loss_location)
    y = pair(pdf, y, "Country", scenario.loss_country)
    y = pair(pdf, y, "Cause of loss", scenario.cause)
    y -= 6

    y = paragraph(pdf, y, "Description of loss", scenario.description)
    y = paragraph(pdf, y, "Property affected", scenario.affected_assets)

    y = pair(pdf, y, "Injuries", scenario.injuries)
    y = pair(pdf, y, "Fatalities", scenario.fatalities)
    y = pair(pdf, y, "Business interruption", scenario.business_interruption)
    y = pair(pdf, y, "Structural damage", scenario.structural_damage)
    y = pair(pdf, y, "Environmental exposure", scenario.environmental_exposure)
    y -= 12

    y = section(pdf, y, "FINANCIAL")
    y = pair(pdf, y, "Currency", scenario.currency)
    y = pair(pdf, y, "Estimated loss", scenario.estimated_loss)
    y = pair(pdf, y, "Repair estimate", scenario.repair_estimate)
    y -= 12

    y = section(pdf, y, "ADDITIONAL")
    y = pair(pdf, y, "Incident reference", scenario.incident_reference)
    if scenario.police_reference:
        y = pair(pdf, y, "Police reference", scenario.police_reference)
    y = pair(pdf, y, "Authorities involved", scenario.authorities)
    y = pair(pdf, y, "Potential litigation", scenario.potential_litigation)

    footer(pdf, running, 2, 2)
    pdf.save()


def build_policy_document(scenario: Scenario) -> None:
    """The cover, in the policy document's own vocabulary."""
    pdf = new_canvas(str(scenario.directory / scenario.policy_file), scenario.policy_title)

    y = header(pdf, scenario.policy_title.upper(), scenario.policy_subtitle)

    y = section(pdf, y, "THE COVER")
    y = pair(pdf, y, scenario.policy_number_label, scenario.policy_number)
    y = pair(pdf, y, scenario.policy_insured_label, scenario.insured)
    y = pair(pdf, y, "Class of business", scenario.policy_type)
    y = pair(pdf, y, "Period of insurance", scenario.policy_period)
    y = pair(pdf, y, "Limit of indemnity", scenario.policy_limit)
    y = pair(pdf, y, "Deductible", scenario.policy_deductible)
    y = pair(pdf, y, "Broker", scenario.broker)
    y = pair(pdf, y, "Broker reference", scenario.broker_reference)
    y -= 12

    y = section(pdf, y, "THE INTEREST INSURED")
    y = paragraph(pdf, y, scenario.policy_interest_label, scenario.policy_interest)
    for label, value in scenario.policy_extra:
        y = pair(pdf, y, label, value)
    y -= 12

    y = section(pdf, y, "CONDITIONS")
    y = paragraph(pdf, y, "Conditions", scenario.policy_conditions)

    footer(pdf, f"{scenario.broker} · {scenario.policy_number}", 1, 1)
    pdf.save()


def build_report(scenario: Scenario) -> None:
    """The specialist's report, with the money on the last page.

    Three pages, and the ordering is the point: what was inspected, then what was
    found, then what it is expected to cost. The quantum sits alone on page 3
    under a heading nobody would guess, which is what makes a citation to it
    obviously better than a search of the whole file.
    """
    pdf = new_canvas(str(scenario.directory / scenario.report_file), scenario.report_title)
    running = f"{scenario.report_firm} · {scenario.report_reference}"

    # --- Page 1: instructions, and what was looked at ------------------------
    y = header(
        pdf,
        scenario.report_title.upper(),
        f"{scenario.report_firm} · Our reference {scenario.report_reference}",
    )

    y = section(pdf, y, "INSTRUCTIONS")
    y = pair(pdf, y, "Instructed by", scenario.broker)
    y = pair(pdf, y, "On behalf of", scenario.insured)
    y = pair(pdf, y, "Policy number", scenario.policy_number)
    y = pair(pdf, y, "Author", scenario.report_author)
    for label, value in scenario.report_instructions:
        y = pair(pdf, y, label, value)
    y -= 12

    y = section(pdf, y, scenario.asset_heading.upper())
    for label, value in scenario.asset_rows:
        y = pair(pdf, y, label, value)

    footer(pdf, running, 1, 3)
    pdf.showPage()

    # --- Page 2: what happened, and what was found ---------------------------
    y = header(pdf, scenario.report_title.upper(), "Circumstances and findings")

    y = section(pdf, y, "CIRCUMSTANCES")
    y = paragraph(pdf, y, "Circumstances", scenario.report_circumstances)

    y = section(pdf, y, "FINDINGS")
    y = paragraph(pdf, y, "Findings", scenario.report_findings)

    y = pair(pdf, y, "Date of loss", scenario.date_of_loss)
    y = pair(pdf, y, "Cause of loss", scenario.cause)
    y = pair(pdf, y, "Incident reference", scenario.incident_reference)
    y = pair(pdf, y, "Injuries reported", scenario.injuries)

    footer(pdf, running, 2, 3)
    pdf.showPage()

    # --- Page 3: the money ---------------------------------------------------
    y = header(pdf, scenario.report_title.upper(), "Preliminary quantum and reserve advice")

    y = section(pdf, y, "PRELIMINARY QUANTUM")
    y = paragraph(
        pdf,
        y,
        "Basis",
        "The figures below are preliminary and are given for reserving purposes only. "
        "They are subject to verification and may move as the investigation continues.",
    )
    for label, value in scenario.report_quantum:
        y = pair(pdf, y, label, value)
    y = pair(pdf, y, "Estimated loss", scenario.estimated_loss)
    y = pair(pdf, y, "Currency", scenario.currency)
    y -= 12

    y = section(pdf, y, scenario.report_comment_heading.upper())
    y = paragraph(pdf, y, "Comment", scenario.report_comment)

    y = section(pdf, y, "RECOMMENDATION")
    y = paragraph(pdf, y, "Recommendation", scenario.report_recommendation)

    footer(pdf, running, 3, 3)
    pdf.save()


def build_schedule(scenario: Scenario) -> None:
    """The itemised figures behind the repair estimate.

    A spreadsheet has no page geometry, so a value read from here resolves
    `text-only` — the quote is the highlight. It is in every pack for exactly
    that reason: a review screen has to handle the document it cannot draw on.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(scenario.schedule_headers)
    for row in scenario.schedule_rows:
        writer.writerow(row)

    # The total is computed from the lines above it. A schedule that does not add
    # up is the fastest way to make a demo look wrong to somebody who checks.
    #
    # The label sits in the description column and the figure under the line
    # totals, which is where a reader looks for them — and where the marine pack
    # puts them, so the two read the same way.
    leading = [""] * (len(scenario.schedule_headers) - 4)
    writer.writerow([*leading, scenario.schedule_total_label, "", "", str(scenario.schedule_total)])

    (scenario.directory / scenario.schedule_file).write_text(buffer.getvalue())


def build_email(scenario: Scenario) -> None:
    """The broker's covering note, as an `.eml` the pipeline can ingest."""
    facts = "\n".join(f"{label}: {value}" for label, value in scenario.email_facts)
    attachments = "\n".join(
        f"  * {name}"
        for name in (
            "the completed loss notice",
            "the policy document",
            scenario.report_title.lower(),
            "the itemised schedule behind the repair figure",
        )
    )

    body = f"""From: {scenario.handler} <{scenario.handler_email}>
To: {scenario.email_to}
Cc: {scenario.email_cc}
Subject: {scenario.email_subject}
Date: {scenario.email_date}
Message-ID: <{scenario.email_message_id}>
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: 8bit

{scenario.email_opening}

{facts}

{scenario.email_narrative}

Attached:

{attachments}

{scenario.email_closing}

Kind regards,

{scenario.handler}
{scenario.handler_role}
{scenario.broker}
{scenario.handler_email}
{scenario.handler_phone}
"""
    (scenario.directory / "broker-notification.eml").write_text(body)


def build_readme(scenario: Scenario) -> None:
    """A short, factual sheet per pack: the facts, the files, and the point."""
    facts = "\n".join(
        f"| {label} | {value} |"
        for label, value in (
            ("Line of business", scenario.line_of_business),
            ("Insured", scenario.insured),
            ("Claimant", scenario.claimant),
            ("Broker", f"{scenario.broker} — {scenario.handler}, {scenario.handler_role}"),
            ("Policy", f"`{scenario.policy_number}`, {scenario.policy_type}"),
            ("Broker reference", f"`{scenario.broker_reference}`"),
            ("Date of loss", f"{scenario.date_of_loss}, {scenario.time_of_loss}"),
            ("Loss location", f"{scenario.loss_location}, {scenario.loss_country}"),
            ("Cause", scenario.cause),
            ("Incident reference", f"`{scenario.incident_reference}`"),
            ("Estimated loss", scenario.estimated_loss),
            ("Repair estimate", scenario.repair_estimate),
        )
    )

    body = f"""# {scenario.title}

{scenario.line_of_business} claim, as one broker's notification: a covering email
with four attachments that agree with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
{facts}

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
{scenario.notice_file:<32} 2-page completed FNOL form
{scenario.policy_file:<32} the policy document
{scenario.report_file:<32} 3-page {scenario.report_title.lower()}
{scenario.schedule_file:<32} {scenario.schedule_title.lower()}
```

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Fields split across pages.** The loss notice carries the policy and the
  people on page 1, and the loss and the money on page 2. A citation that opens
  the right page is doing something a whole-document answer cannot.
* **The same fact in two vocabularies.** The policy document says
  "{scenario.policy_insured_label}" where the notice says "Insured name", and
  "{scenario.policy_number_label}" where the notice says "Policy number".
* **A figure stated twice.** {scenario.estimated_loss} appears on page 3 of the
  {scenario.report_title.lower()} *and* in the covering email. A citation naming
  page 3 is the passage that was read, not the first match in the case file.
* **A document with no page geometry.** `{scenario.schedule_file}` is a
  spreadsheet, so its evidence resolves `text-only` with the quote and no
  rectangles — the honest answer at one level less precision.

## Loading it

The demo runner ingests one pack at a time:

```bash
uv run python -m app.db.demo --pack {scenario.slug}
make demo-packs        # every pack in turn
```

The schedule's line items really add up to {scenario.schedule_total:,}, which
`tests/unit/test_demo_documents.py` checks along with the page split and the
resolution of a known quote to a rectangle.
"""
    (scenario.directory / "README.md").write_text(body)


# ---------------------------------------------------------------------------
# The five scenarios
# ---------------------------------------------------------------------------

NORTHGATE = Scenario(
    slug="northgate-warehouse-fire",
    title="Northgate — warehouse fire",
    line_of_business="Commercial property",
    broker="Halloway & Prine Insurance Brokers Limited",
    handler="Desmond Achebe",
    handler_role="Senior Claims Handler",
    handler_email="d.achebe@hallowayprine.example",
    handler_phone="+44 161 496 0812",
    broker_reference="HP/PROP/2026/1471",
    reported_on="14 June 2026",
    insured="Northgate Provisions Limited",
    policy_number="CP-2026-30582",
    policy_type="Commercial Combined",
    policy_period="01 April 2026 to 31 March 2027",
    policy_limit="GBP 12,000,000 buildings, stock and business interruption",
    policy_deductible="GBP 10,000 each and every loss",
    policy_insured_label="Policyholder",
    policy_number_label="Policy number",
    policy_title="Commercial Combined Policy Schedule",
    policy_subtitle="This schedule forms part of the policy and should be read with it",
    policy_interest_label="Property insured",
    policy_interest=(
        "Buildings, landlord's fixtures and fittings, refrigeration and chilled storage "
        "plant, racking, stock in trade and goods held in trust, at the premises stated "
        "below, together with business interruption on a gross profit basis with a "
        "twenty-four month indemnity period."
    ),
    policy_conditions=(
        "Subject to the Insurer's Commercial Combined wording CC/2024/03. Fire and "
        "special perils, including explosion and impact. Warranted an automatic fire "
        "detection system is maintained and tested annually. Claims payable in the "
        "United Kingdom in Pounds Sterling."
    ),
    policy_extra=(
        ("Premises", "Unit 12, Brookfield Industrial Estate, Rochdale OL16 2QZ"),
        ("Trade", "Chilled and ambient food distribution"),
        ("Sum insured — buildings", "GBP 4,600,000"),
        ("Sum insured — stock", "GBP 3,200,000"),
    ),
    claimant="Northgate Provisions Limited",
    claimant_contact="Alison Kerrigan, Operations Director",
    claimant_email="a.kerrigan@northgateprovisions.example",
    date_of_loss="14 June 2026",
    time_of_loss="02:15 BST",
    loss_location="Unit 12, Brookfield Industrial Estate, Rochdale OL16 2QZ",
    loss_country="United Kingdom",
    cause="Fire — electrical fault in chilled storage plant",
    description=(
        "A fire began overnight in the plant room serving the chilled storage hall and "
        "spread into the adjoining racking. The fire and rescue service attended within "
        "eleven minutes and the fire was extinguished by 04:40. The chilled hall and "
        "approximately one third of the ambient hall are fire damaged, and the remainder "
        "of the building has smoke and water damage throughout. The premises are closed "
        "and distribution has been moved to a third-party site."
    ),
    affected_assets=(
        "Single-storey distribution warehouse of approximately 4,200 square metres, "
        "chilled storage plant, pallet racking, and stock in trade held at the premises."
    ),
    injuries="0",
    fatalities="0",
    business_interruption="Yes",
    structural_damage="Yes",
    environmental_exposure="No",
    authorities="Greater Manchester Fire and Rescue Service",
    potential_litigation="No",
    incident_reference="GMFRS/2026/08841",
    police_reference=None,
    asset_heading="Premises particulars",
    asset_rows=(
        ("Premises", "Unit 12, Brookfield Industrial Estate, Rochdale OL16 2QZ"),
        ("Construction", "Steel portal frame, profiled metal cladding, concrete floor"),
        ("Floor area", "4,200 square metres"),
        ("Year built", "2011"),
        ("Occupancy", "Chilled and ambient food distribution"),
        ("Protections", "Automatic fire detection throughout; no sprinkler installation"),
    ),
    currency="GBP",
    estimated_loss="GBP 1,284,000",
    repair_estimate="GBP 742,600",
    repair_estimate_label="Reinstatement estimate",
    report_slug="northgate-fire-damage-assessment",
    report_title="Fire Damage Assessment",
    report_firm="Ardwick Loss Adjusting LLP",
    report_reference="ALA/2026/3341",
    report_author="Gerald Nkemelu FCILA, Chartered Loss Adjuster",
    report_instructions=(
        ("Date of instruction", "14 June 2026"),
        ("Date of attendance", "15 June 2026"),
        ("Attended with", "The insured's operations director and the site manager"),
    ),
    report_circumstances=(
        "The fire originated in the plant room serving the chilled storage hall. An "
        "electrical contractor attended on 9 June to replace a failed condenser fan "
        "motor. The fire and rescue service's initial view is that the seat of the fire "
        "is within or adjacent to that plant, and they have not treated the fire as "
        "suspicious. The premises were unoccupied at the time and the alarm was raised "
        "by the monitored detection system at 02:19."
    ),
    report_findings=(
        "The chilled hall is extensively fire damaged, with loss of the roof deck over "
        "approximately 900 square metres and distortion to four portal frames. Racking "
        "in the chilled hall is heat distorted and must be replaced. The ambient hall "
        "has smoke and water damage but the structure appears sound, subject to a "
        "structural engineer's opinion which we have commissioned. All chilled and "
        "frozen stock has been condemned by the environmental health officer. Ambient "
        "stock in the western third of the building is salvageable subject to cleaning."
    ),
    report_quantum=(
        ("Buildings reinstatement", "GBP 742,600"),
        ("Stock — condemned", "GBP 541,400"),
        ("Salvage allowance", "GBP 0"),
    ),
    report_comment_heading="Business interruption",
    report_comment=(
        "The insured has moved distribution to a third-party site at a cost we are told "
        "is approximately GBP 38,000 per month. Reinstatement is expected to take nine "
        "to twelve months. A business interruption element is therefore substantial but "
        "is not quantified in this report."
    ),
    report_recommendation=(
        "That a reserve of GBP 1,284,000 be established for material damage, that a "
        "structural engineer be instructed on the ambient hall, and that the electrical "
        "contractor's records be obtained with recovery in mind."
    ),
    schedule_slug="northgate-stock-loss-schedule",
    schedule_title="Condemned stock schedule",
    schedule_headers=(
        "Category",
        "Code",
        "Description",
        "Pallets",
        "Value per pallet GBP",
        "Line total GBP",
    ),
    schedule_rows=(
        ("Chilled", "CH-RM-01", "Chilled ready meals", "214", "600", "128400"),
        ("Frozen", "FR-PY-02", "Frozen poultry", "129", "750", "96750"),
        ("Ambient", "AM-DG-03", "Ambient dry goods", "371", "200", "74200"),
        ("Packaging", "PK-MT-04", "Packaging materials and outer cases", "413", "100", "41300"),
        ("Chilled", "CH-DY-05", "Chilled dairy", "197", "450", "88600"),
        ("Ambient", "AM-BV-06", "Beverages", "193", "300", "57900"),
        ("Returns", "RT-PL-07", "Palletised customer returns", "155", "350", "54250"),
    ),
    schedule_total_label="Condemned stock total",
    email_subject="FNOL - Northgate Provisions - warehouse fire - CP-2026-30582",
    email_to="fnol@carrier.example",
    email_cc="claims.support@hallowayprine.example",
    email_date="Sun, 14 Jun 2026 09:24:11 +0100",
    email_message_id="hp-prop-2026-1471-a@hallowayprine.example",
    email_opening=(
        "Good morning,\n\n"
        "We are instructed to notify a fire loss on behalf of our client Northgate "
        "Provisions Limited. Our reference is HP/PROP/2026/1471."
    ),
    email_narrative=(
        "A fire broke out overnight in the plant room serving the chilled storage hall "
        "at the insured's Rochdale distribution centre and spread into the adjoining\n"
        "racking. Greater Manchester Fire and Rescue attended and the fire was out by\n"
        "04:40. There are no injuries.\n\n"
        "The chilled hall is extensively damaged and all chilled and frozen stock has\n"
        "been condemned by environmental health. The premises are closed and our client\n"
        "has moved distribution to a third-party site, so there will be a business\n"
        "interruption element which is not yet quantified.\n\n"
        "Ardwick Loss Adjusting attended on 15 June and their assessment is attached."
    ),
    email_closing=(
        "Please confirm a claim reference and let me know what else you need. I am on\n"
        "+44 161 496 0812 if it is easier to talk it through."
    ),
    email_facts=(
        ("Policy number", "CP-2026-30582"),
        ("Insured name", "Northgate Provisions Limited"),
        ("Date of loss", "14 June 2026"),
        ("Loss location", "Unit 12, Brookfield Industrial Estate, Rochdale OL16 2QZ"),
        ("Cause", "Fire - electrical fault in chilled storage plant"),
        ("Estimated loss", "GBP 1,284,000"),
        ("Repair estimate", "GBP 742,600"),
    ),
)


PENNINE = Scenario(
    slug="pennine-fleet-collision",
    title="Pennine — HGV collision",
    line_of_business="Commercial motor fleet",
    broker="Marchmont Commercial Risks Limited",
    handler="Priya Raghunathan",
    handler_role="Motor Claims Handler",
    handler_email="p.raghunathan@marchmontrisks.example",
    handler_phone="+44 113 496 2270",
    broker_reference="MCR/MOT/2026/0663",
    reported_on="3 May 2026",
    insured="Pennine Haulage Group Limited",
    policy_number="FLT-2026-11907",
    policy_type="Commercial Motor Fleet",
    policy_period="01 February 2026 to 31 January 2027",
    policy_limit="GBP 5,000,000 third party property damage; unlimited third party injury",
    policy_deductible="GBP 2,500 each and every own damage claim",
    policy_insured_label="Policyholder",
    policy_number_label="Certificate number",
    policy_title="Certificate of Motor Insurance",
    policy_subtitle="Issued in accordance with the Road Traffic Act 1988",
    policy_interest_label="Vehicles insured",
    policy_interest=(
        "All commercial vehicles owned by or in the custody of the policyholder and "
        "declared to the insurer, together with attached trailers, whilst used for the "
        "carriage of the policyholder's own goods and goods of others for hire and "
        "reward within Great Britain, Northern Ireland and the European Union."
    ),
    policy_conditions=(
        "Cover is comprehensive. Drivers: any person holding a valid licence to drive "
        "the vehicle and named on the policyholder's driver schedule. Excludes use for "
        "the carriage of dangerous goods requiring an ADR licence unless declared."
    ),
    policy_extra=(
        ("Fleet size declared", "48 vehicles"),
        ("Rated use", "Carriage of own goods and goods for hire and reward"),
        ("Territorial limits", "Great Britain, Northern Ireland and the European Union"),
    ),
    claimant="Pennine Haulage Group Limited",
    claimant_contact="Martin Ashcroft, Transport Manager",
    claimant_email="m.ashcroft@penninehaulage.example",
    date_of_loss="3 May 2026",
    time_of_loss="06:52 BST",
    loss_location="A1(M) southbound, Junction 47, near Wetherby LS22 5HR",
    loss_country="United Kingdom",
    cause="Collision — loss of control on standing water",
    description=(
        "The insured vehicle was travelling south on the A1(M) in heavy rain when the "
        "driver lost control on standing water in lane one. The tractor unit struck the "
        "nearside barrier and jack-knifed across lanes one and two, and was then struck "
        "on the offside by a third-party refrigerated vehicle which was unable to stop. "
        "The third-party driver was taken to hospital with a suspected fractured wrist. "
        "The carriageway was closed for four hours. Approximately 180 litres of diesel "
        "escaped from the insured vehicle's ruptured tank and entered the drainage."
    ),
    affected_assets=(
        "DAF XF 480 tractor unit, registration YK24 PHG, and SDC curtainsider trailer, "
        "fleet number PT-118, together with a part load of palletised ambient goods."
    ),
    injuries="1",
    fatalities="0",
    business_interruption="No",
    structural_damage="No",
    environmental_exposure="Yes",
    authorities="West Yorkshire Police; National Highways",
    potential_litigation="Yes",
    incident_reference="NH/INC/2026/55180",
    police_reference="WYP/RTC/2026/14229",
    asset_heading="Vehicle particulars",
    asset_rows=(
        ("Vehicle", "DAF XF 480 FT 4x2 tractor unit"),
        ("Registration", "YK24 PHG"),
        ("VIN", "XLRTE47MS0E123456"),
        ("First registered", "March 2024"),
        ("Odometer", "184,220 km"),
        ("Trailer", "SDC curtainsider, fleet number PT-118"),
        ("Driver", "Kevin Braithwaite, licence held 14 years"),
    ),
    currency="GBP",
    estimated_loss="GBP 214,750",
    repair_estimate="GBP 96,480",
    repair_estimate_label="Repair and recovery estimate",
    report_slug="pennine-incident-report",
    report_title="Motor Incident Report",
    report_firm="Calverley Motor Assessors Limited",
    report_reference="CMA/2026/7712",
    report_author="Ian Hollingworth AMIMI, Senior Assessor",
    report_instructions=(
        ("Date of instruction", "3 May 2026"),
        ("Date of inspection", "5 May 2026"),
        ("Place of inspection", "Calverley recovery compound, Leeds LS28 5AB"),
    ),
    report_circumstances=(
        "Conditions at the time were heavy rain with standing water reported by National "
        "Highways on that section of the A1(M). Tachograph data shows the insured vehicle "
        "travelling at 84 km/h in the thirty seconds before the incident, within the "
        "limit for the road but, in our view, above what the conditions allowed. The "
        "driver reports aquaplaning in lane one and being unable to recover the vehicle "
        "before it struck the nearside barrier."
    ),
    report_findings=(
        "The tractor unit has severe impact damage to the nearside cab, the front axle "
        "and the steering assembly, and the offside has secondary impact damage from the "
        "third-party vehicle. The chassis is distorted forward of the fifth wheel. The "
        "trailer has curtain and body damage on the offside but the running gear is "
        "undamaged. The fuel tank is ruptured. Tyre tread depths were within legal "
        "limits on all axles. We consider the tractor unit economically repairable, but "
        "marginally so."
    ),
    report_quantum=(
        ("Repair and recovery", "GBP 96,480"),
        ("Third party vehicle and injury reserve", "GBP 105,000"),
        ("Load and disposal", "GBP 13,270"),
        ("Pre-accident value", "GBP 108,000"),
    ),
    report_comment_heading="Recovery and liability",
    report_comment=(
        "Liability rests with the insured on the balance of the evidence available. The "
        "third-party driver's injury claim has not yet been presented. National Highways "
        "has indicated it will recover the cost of the barrier repair and the "
        "carriageway closure, which we have allowed for within the third-party reserve."
    ),
    report_recommendation=(
        "That a reserve of GBP 214,750 be established, that the repair be authorised "
        "subject to a strip-down report on the chassis, and that the environmental "
        "clean-up invoice be obtained before the diesel spill element is settled."
    ),
    schedule_slug="pennine-repair-cost-schedule",
    schedule_title="Repair and recovery cost schedule",
    schedule_headers=(
        "Section",
        "Code",
        "Description",
        "Quantity",
        "Unit cost GBP",
        "Line total GBP",
    ),
    schedule_rows=(
        ("Tractor unit", "TU-CAB-01", "Cab shell replacement and paint", "1", "38200", "38200"),
        ("Tractor unit", "TU-AXL-02", "Front axle and steering assembly", "1", "14650", "14650"),
        ("Trailer", "TR-BDY-03", "Curtain and offside body repair", "1", "12940", "12940"),
        ("Recovery", "RC-RDS-04", "Roadside recovery and heavy lift", "1", "6380", "6380"),
        (
            "Environmental",
            "EN-SPL-05",
            "Diesel spill containment and clean-up",
            "1",
            "9720",
            "9720",
        ),
        ("Load", "LD-TRF-06", "Load transfer and damaged goods disposal", "1", "8410", "8410"),
        (
            "Mitigation",
            "MT-HIR-07",
            "Hire replacement tractor unit, 8 weeks",
            "8",
            "772.50",
            "6180",
        ),
    ),
    schedule_total_label="Repair and recovery total",
    email_subject="FNOL - Pennine Haulage - A1(M) collision - FLT-2026-11907",
    email_to="fnol@carrier.example",
    email_cc="motor.claims@marchmontrisks.example",
    email_date="Sun, 03 May 2026 08:41:52 +0100",
    email_message_id="mcr-mot-2026-0663-a@marchmontrisks.example",
    email_opening=(
        "Good morning,\n\n"
        "We are instructed to notify a motor claim on behalf of our client Pennine "
        "Haulage Group Limited. Our reference is MCR/MOT/2026/0663."
    ),
    email_narrative=(
        "The insured tractor unit YK24 PHG lost control on standing water on the A1(M)\n"
        "southbound near Junction 47 early this morning, struck the nearside barrier and\n"
        "jack-knifed. A third-party refrigerated vehicle was unable to stop and struck\n"
        "the offside.\n\n"
        "The third-party driver has been taken to hospital with a suspected fractured\n"
        "wrist, so please treat this as a potential injury claim. Around 180 litres of\n"
        "diesel escaped and entered the drainage; the environmental clean-up is in hand.\n\n"
        "Calverley Motor Assessors inspected on 5 May and their report is attached. They\n"
        "consider the unit economically repairable, but marginally so."
    ),
    email_closing=(
        "Please confirm a claim reference. I am on +44 113 496 2270 if you would rather\n"
        "talk it through."
    ),
    email_facts=(
        ("Policy number", "FLT-2026-11907"),
        ("Insured name", "Pennine Haulage Group Limited"),
        ("Date of loss", "3 May 2026"),
        ("Loss location", "A1(M) southbound, Junction 47, near Wetherby LS22 5HR"),
        ("Cause", "Collision - loss of control on standing water"),
        ("Vehicle", "DAF XF 480, registration YK24 PHG"),
        ("Estimated loss", "GBP 214,750"),
        ("Repair estimate", "GBP 96,480"),
    ),
)


CALDERWOOD = Scenario(
    slug="calderwood-professional-indemnity",
    title="Calderwood — professional indemnity",
    line_of_business="Professional indemnity",
    broker="Sterne Aldridge Professional Risks LLP",
    handler="Fiona Barrowman",
    handler_role="Professional Indemnity Claims Executive",
    handler_email="f.barrowman@sternealdridge.example",
    handler_phone="+44 20 7946 0388",
    broker_reference="SA/PI/2026/0219",
    reported_on="27 February 2026",
    insured="Calderwood Consulting LLP",
    policy_number="PI-2026-55041",
    policy_type="Professional Indemnity",
    policy_period="01 January 2026 to 31 December 2026",
    policy_limit="GBP 5,000,000 each and every claim and in the aggregate",
    policy_deductible="GBP 50,000 each and every claim including costs",
    policy_insured_label="Assured",
    policy_number_label="Policy reference",
    policy_title="Professional Indemnity Policy Schedule",
    policy_subtitle="Claims made basis — this schedule forms part of the policy",
    policy_interest_label="Professional business",
    policy_interest=(
        "Structural and civil engineering consultancy, including design, specification, "
        "inspection and contract administration, carried out by or on behalf of the "
        "assured anywhere in the United Kingdom."
    ),
    policy_conditions=(
        "Written on a claims made basis. Cover applies to claims first made against the "
        "assured and notified to the insurer during the period of insurance. "
        "Retroactive date: 01 January 2014. Excludes liability assumed under a "
        "contractual guarantee or fitness for purpose obligation."
    ),
    policy_extra=(
        ("Retroactive date", "01 January 2014"),
        ("Basis of cover", "Claims made and notified"),
        ("Fee income declared", "GBP 4,180,000"),
    ),
    claimant="Meridian Estates (Salford) Limited",
    claimant_contact="Grace Whitfield, Development Director",
    claimant_email="g.whitfield@meridianestates.example",
    date_of_loss="27 February 2026",
    time_of_loss="Not applicable — date of claim notification",
    loss_location="Riverside Quarter, Block C, Salford M50 3AZ",
    loss_country="United Kingdom",
    cause="Alleged negligent design — insufficient movement joints in facade",
    description=(
        "The assured was engaged as structural engineer for a residential development at "
        "Riverside Quarter. Cracking has appeared in the precast concrete facade panels "
        "of Block C, first reported by the building's managing agent in November 2025. "
        "The claimant's expert attributes the cracking to thermal movement which the "
        "facade design does not accommodate, and alleges that the assured's design "
        "omitted movement joints required by the relevant British Standard. A letter of "
        "claim was received on 24 February 2026 and is attached. The assured denies "
        "liability and has referred the matter under its professional indemnity policy."
    ),
    affected_assets=(
        "Precast concrete facade to Block C, Riverside Quarter — 18 panels reported as "
        "cracked across eight floors, of a total facade of 96 panels."
    ),
    injuries="0",
    fatalities="0",
    business_interruption="No",
    structural_damage="Yes",
    environmental_exposure="No",
    authorities="None",
    potential_litigation="Yes",
    incident_reference="ME/RQ/2026/0031",
    police_reference=None,
    asset_heading="Engagement particulars",
    asset_rows=(
        ("Project", "Riverside Quarter, Block C, Salford M50 3AZ"),
        ("Assured's role", "Structural engineer"),
        ("Appointment dated", "12 March 2021"),
        ("Practical completion", "8 September 2023"),
        ("Contract value", "GBP 24,600,000"),
        ("Assured's fee", "GBP 412,000"),
        ("Standard alleged breached", "BS EN 1992-1-1 and BS 8110 movement joint provisions"),
    ),
    currency="GBP",
    estimated_loss="GBP 875,000",
    repair_estimate="GBP 612,300",
    repair_estimate_label="Remedial works estimate",
    report_slug="calderwood-letter-of-claim",
    report_title="Letter of Claim and Expert Summary",
    report_firm="Hartlow Beckett LLP, solicitors for the claimant",
    report_reference="HB/MES/2026/0088",
    report_author="Simon Kettering, Partner",
    report_instructions=(
        ("Letter of claim dated", "24 February 2026"),
        ("Received by the assured", "26 February 2026"),
        ("Protocol", "Pre-Action Protocol for Construction and Engineering Disputes"),
        ("Response due", "26 April 2026"),
    ),
    report_circumstances=(
        "The claimant is the freehold owner of Riverside Quarter. Cracking was first "
        "reported by the managing agent in November 2025, some twenty-six months after "
        "practical completion. The claimant instructed Pellow Harrington Consulting to "
        "investigate. Their report of 6 February 2026 concludes that the cracking is "
        "consistent with restrained thermal movement and that the facade as designed "
        "contains no vertical movement joints over runs exceeding twelve metres."
    ),
    report_findings=(
        "The claimant's expert identifies 18 cracked panels across eight floors of the "
        "east and south elevations. Crack widths are between 0.4mm and 2.1mm. The expert "
        "considers the cracking is not structurally dangerous at present but will worsen, "
        "and that water ingress at the wider cracks presents a durability risk to the "
        "embedded reinforcement. The recommended remedy is the introduction of vertical "
        "movement joints at twelve-metre centres and replacement of the 18 affected "
        "panels. The assured has not yet had access to inspect."
    ),
    report_quantum=(
        ("Remedial works estimate", "GBP 612,300"),
        ("Claimant's professional fees", "GBP 148,700"),
        ("Alleged loss of rent during works", "GBP 114,000"),
    ),
    report_comment_heading="The assured's position",
    report_comment=(
        "The assured denies liability. It contends that the facade panel design was the "
        "responsibility of the specialist subcontractor under a design portion, that its "
        "own appointment was limited to the primary frame, and that the claim is in any "
        "event brought outside the period allowed by the appointment. No admission has "
        "been made and this notification is precautionary."
    ),
    report_recommendation=(
        "That a reserve of GBP 875,000 inclusive of costs be established, that solicitors "
        "be instructed to respond within the protocol period, and that the assured's "
        "appointment and the subcontractor's design portion agreement be obtained before "
        "any substantive response is given."
    ),
    schedule_slug="calderwood-remedial-cost-schedule",
    schedule_title="Remedial works cost schedule",
    schedule_headers=(
        "Section",
        "Code",
        "Description",
        "Quantity",
        "Unit cost GBP",
        "Line total GBP",
    ),
    schedule_rows=(
        (
            "Enabling",
            "EN-REM-01",
            "Facade panel removal and temporary storage",
            "18",
            "4694.44",
            "84500",
        ),
        (
            "Structural",
            "ST-MJT-02",
            "New vertical movement joint installation",
            "8",
            "19525",
            "156200",
        ),
        (
            "Facade",
            "FC-PNL-03",
            "Precast panel manufacture and replacement",
            "18",
            "11855.56",
            "213400",
        ),
        ("Access", "AC-SCF-04", "Scaffolding and access, 26 weeks", "26", "3034.62", "78900"),
        ("Fees", "FE-RDS-05", "Structural re-design and site supervision", "1", "41700", "41700"),
        ("Testing", "TS-CER-06", "Testing, inspection and certification", "1", "22600", "22600"),
        ("Contingency", "CT-GEN-07", "Contingency at 2.5 per cent", "1", "15000", "15000"),
    ),
    schedule_total_label="Remedial works total",
    email_subject="Notification - Calderwood Consulting - Riverside Quarter - PI-2026-55041",
    email_to="fnol@carrier.example",
    email_cc="pi.claims@sternealdridge.example",
    email_date="Fri, 27 Feb 2026 14:08:33 +0000",
    email_message_id="sa-pi-2026-0219-a@sternealdridge.example",
    email_opening=(
        "Dear Sirs,\n\n"
        "We notify a claim made against our client Calderwood Consulting LLP under the "
        "above policy. Our reference is SA/PI/2026/0219."
    ),
    email_narrative=(
        "Our client was the structural engineer for a residential development at\n"
        "Riverside Quarter in Salford. Cracking has appeared in the precast facade of\n"
        "Block C and the building owner, Meridian Estates (Salford) Limited, has served\n"
        "a letter of claim alleging negligent design - specifically that the facade\n"
        "design omitted movement joints required by the relevant standard.\n\n"
        "The letter of claim and the claimant's expert summary are attached. The\n"
        "claimant puts the remedial works at GBP 612,300 and its total claim, with fees\n"
        "and loss of rent, at GBP 875,000.\n\n"
        "Our client denies liability. It says the facade panel design sat with the\n"
        "specialist subcontractor under a design portion and that its own appointment\n"
        "was limited to the primary frame. No admission has been made. This notification\n"
        "is precautionary and made within the period of insurance.\n\n"
        "The protocol response is due by 26 April 2026, so we would be grateful for\n"
        "your confirmation of cover and your instructions on solicitors."
    ),
    email_closing="I am on +44 20 7946 0388 if you would like to discuss.",
    email_facts=(
        ("Policy number", "PI-2026-55041"),
        ("Insured name", "Calderwood Consulting LLP"),
        ("Claimant", "Meridian Estates (Salford) Limited"),
        ("Date of loss", "27 February 2026"),
        ("Loss location", "Riverside Quarter, Block C, Salford M50 3AZ"),
        ("Cause", "Alleged negligent design - insufficient movement joints"),
        ("Estimated loss", "GBP 875,000"),
        ("Repair estimate", "GBP 612,300"),
    ),
)


VERITY = Scenario(
    slug="verity-health-cyber",
    title="Verity Health — ransomware",
    line_of_business="Cyber and data risks",
    broker="Kingsmere Specialty Limited",
    handler="Tomas Lindqvist",
    handler_role="Cyber Claims Specialist",
    handler_email="t.lindqvist@kingsmere.example",
    handler_phone="+44 20 7946 0917",
    broker_reference="KS/CYB/2026/0084",
    reported_on="10 August 2026",
    insured="Verity Health Systems Limited",
    policy_number="CYB-2026-40219",
    policy_type="Cyber and Data Risks",
    policy_period="01 June 2026 to 31 May 2027",
    policy_limit="GBP 10,000,000 each and every claim and in the aggregate",
    policy_deductible="GBP 100,000 each and every claim; 12 hour waiting period",
    policy_insured_label="Named insured",
    policy_number_label="Policy number",
    policy_title="Cyber and Data Risks Policy Schedule",
    policy_subtitle="Claims made and circumstances notified basis",
    policy_interest_label="Business description",
    policy_interest=(
        "The provision of private diagnostic imaging and pathology services from eleven "
        "sites in England, including the processing of patient personal data and special "
        "category health data as data controller."
    ),
    policy_conditions=(
        "Cover includes incident response costs, business interruption following a "
        "network security failure subject to the waiting period, data restoration, "
        "regulatory defence and privacy liability. Insurer's nominated incident response "
        "panel must be engaged within 48 hours of discovery."
    ),
    policy_extra=(
        ("Business interruption sub-limit", "GBP 4,000,000"),
        ("Waiting period", "12 hours"),
        ("Nominated response panel", "Redwater Cyber Response"),
        ("Sites covered", "11"),
    ),
    claimant="Verity Health Systems Limited",
    claimant_contact="Dr Anita Raman, Chief Operating Officer",
    claimant_email="a.raman@verityhealth.example",
    date_of_loss="9 August 2026",
    time_of_loss="23:41 BST",
    loss_location="Primary data centre, Thames Reach Business Park, Reading RG6 1PT",
    loss_country="United Kingdom",
    cause="Ransomware — credential compromise via third-party VPN appliance",
    description=(
        "An unauthorised third party gained access to the insured's network through a "
        "third-party VPN appliance which had not been patched against a vulnerability "
        "disclosed in May 2026. Encryption of the imaging archive and the patient "
        "administration system began at 23:41 on 9 August and was detected by the "
        "insured's monitoring at 00:12 on 10 August. All eleven sites lost access to "
        "imaging and scheduling. A ransom demand of 40 bitcoin was left on affected "
        "hosts. The insured has not engaged with the threat actor. Services were "
        "restored progressively over four days from validated backups."
    ),
    affected_assets=(
        "Imaging archive (approximately 41TB), patient administration system, and the "
        "scheduling platform serving eleven diagnostic sites."
    ),
    injuries="0",
    fatalities="0",
    business_interruption="Yes",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Information Commissioner's Office; National Cyber Security Centre",
    potential_litigation="Yes",
    incident_reference="NCSC/IR/2026/3318",
    police_reference="ACTIONFRAUD/2026/9920145",
    asset_heading="Environment particulars",
    asset_rows=(
        ("Primary data centre", "Thames Reach Business Park, Reading RG6 1PT"),
        ("Sites affected", "11 of 11"),
        ("Systems encrypted", "Imaging archive, patient administration, scheduling"),
        ("Data volume", "Approximately 41TB"),
        ("Entry vector", "Unpatched third-party VPN appliance"),
        ("Records potentially affected", "218,000 patient records"),
        ("Backups", "Immutable, offsite, last validated 8 August 2026"),
    ),
    currency="GBP",
    estimated_loss="GBP 2,410,000",
    repair_estimate="GBP 1,486,000",
    repair_estimate_label="Business interruption and response estimate",
    report_slug="verity-incident-response-report",
    report_title="Incident Response Report",
    report_firm="Redwater Cyber Response Limited",
    report_reference="RCR/2026/1180",
    report_author="Nadia Osei-Bonsu, Lead Incident Responder",
    report_instructions=(
        ("Engaged", "10 August 2026, 02:20 BST"),
        ("Containment achieved", "10 August 2026, 07:55 BST"),
        ("Services restored", "13 August 2026"),
        ("Report status", "Preliminary — forensics continuing"),
    ),
    report_circumstances=(
        "Initial access was obtained through a third-party VPN appliance running firmware "
        "vulnerable to CVE-2026-1188, disclosed on 14 May 2026 with a patch available "
        "from 21 May. The appliance was unpatched at the time of the incident. The threat "
        "actor maintained access for approximately eleven days before deploying the "
        "encryptor, during which time credentials for a domain administrator account "
        "were obtained. Encryption began at 23:41 on 9 August and was detected by "
        "endpoint monitoring at 00:12 on 10 August."
    ),
    report_findings=(
        "The imaging archive, the patient administration system and the scheduling "
        "platform were encrypted. Backups were immutable and offsite and were not "
        "reached by the threat actor, which is the single reason this incident did not "
        "become a total data loss. We have confirmed exfiltration of approximately 240GB "
        "prior to encryption, consistent with a double extortion pattern. The insured "
        "did not engage with the threat actor and no payment was made. All eleven sites "
        "were unable to scan or schedule for four days."
    ),
    report_quantum=(
        ("Business interruption and response", "GBP 1,486,000"),
        ("Regulatory and privacy liability reserve", "GBP 620,000"),
        ("System hardening and remediation", "GBP 304,000"),
    ),
    report_comment_heading="Regulatory position",
    report_comment=(
        "The insured notified the Information Commissioner's Office within the 72 hour "
        "period on 11 August 2026 and has engaged with the National Cyber Security "
        "Centre. Given confirmed exfiltration of special category health data affecting "
        "approximately 218,000 patients, notification of data subjects has begun and a "
        "regulatory investigation should be assumed."
    ),
    report_recommendation=(
        "That a reserve of GBP 2,410,000 be established, that the vulnerability "
        "management process be reviewed as a condition of continued cover, and that "
        "privacy counsel be instructed ahead of the regulator's first request."
    ),
    schedule_slug="verity-business-interruption-schedule",
    schedule_title="Business interruption and response cost schedule",
    schedule_headers=(
        "Section",
        "Code",
        "Description",
        "Quantity",
        "Unit cost GBP",
        "Line total GBP",
    ),
    schedule_rows=(
        (
            "Interruption",
            "BI-IMG-01",
            "Lost imaging revenue, 4 days across 11 sites",
            "44",
            "13909.09",
            "612000",
        ),
        ("Mitigation", "MT-OUT-02", "Outsourced scanning capacity", "1", "284500", "284500"),
        (
            "Response",
            "RS-FOR-03",
            "Incident response and forensic investigation",
            "1",
            "196400",
            "196400",
        ),
        (
            "Restoration",
            "RE-DAT-04",
            "Data restoration and system rebuild",
            "1",
            "173600",
            "173600",
        ),
        ("Staffing", "ST-OVT-05", "Additional staffing and overtime", "1", "88300", "88300"),
        (
            "Notification",
            "NO-SUB-06",
            "Data subject notification and credit monitoring",
            "1",
            "74200",
            "74200",
        ),
        ("Legal", "LG-REG-07", "Legal and regulatory advice", "1", "57000", "57000"),
    ),
    schedule_total_label="Business interruption and response total",
    email_subject="URGENT FNOL - Verity Health Systems - ransomware - CYB-2026-40219",
    email_to="fnol@carrier.example",
    email_cc="cyber.claims@kingsmere.example",
    email_date="Mon, 10 Aug 2026 07:15:04 +0100",
    email_message_id="ks-cyb-2026-0084-a@kingsmere.example",
    email_opening=(
        "Good morning,\n\n"
        "We notify a ransomware incident on behalf of our client Verity Health Systems "
        "Limited. Our reference is KS/CYB/2026/0084. Please treat this as urgent."
    ),
    email_narrative=(
        "Encryption of our client's imaging archive and patient administration system\n"
        "began at 23:41 last night and was detected at 00:12. All eleven diagnostic\n"
        "sites are unable to scan or schedule. Redwater Cyber Response was engaged at\n"
        "02:20 this morning under the nominated panel provision and has since contained\n"
        "the incident.\n\n"
        "Entry was through an unpatched third-party VPN appliance. Backups were immutable\n"
        "and offsite and were not reached, so restoration is under way rather than a\n"
        "total loss. Redwater has confirmed exfiltration of around 240GB before\n"
        "encryption, so this is a double extortion case. Our client has not engaged with\n"
        "the threat actor and no payment has been made or is contemplated.\n\n"
        "The ICO was notified on 11 August within the 72 hour period. Around 218,000\n"
        "patient records are potentially affected, so please assume a regulatory\n"
        "investigation and data subject notification.\n\n"
        "Redwater's preliminary report is attached with the loss schedule behind the\n"
        "business interruption figure."
    ),
    email_closing=(
        "Please confirm cover and a claim reference today if you can. I am on\n"
        "+44 20 7946 0917 at any hour on this one."
    ),
    email_facts=(
        ("Policy number", "CYB-2026-40219"),
        ("Insured name", "Verity Health Systems Limited"),
        ("Date of loss", "9 August 2026"),
        ("Loss location", "Primary data centre, Thames Reach Business Park, Reading RG6 1PT"),
        ("Cause", "Ransomware - credential compromise via third-party VPN appliance"),
        ("Estimated loss", "GBP 2,410,000"),
        ("Repair estimate", "GBP 1,486,000"),
    ),
)


ASHWORTH = Scenario(
    slug="ashworth-construction-storm",
    title="Ashworth Bellamy — storm damage on site",
    line_of_business="Contractors all risks",
    broker="Trentham Risk Partners Limited",
    handler="Rachel Oyelaran",
    handler_role="Construction Claims Handler",
    handler_email="r.oyelaran@trenthamrisk.example",
    handler_phone="+44 121 496 5540",
    broker_reference="TRP/CAR/2026/0937",
    reported_on="21 October 2026",
    insured="Ashworth Bellamy Construction Limited",
    policy_number="CAR-2026-70155",
    policy_type="Contractors All Risks",
    policy_period="01 July 2026 to 30 June 2027",
    policy_limit="GBP 25,000,000 contract works any one contract",
    policy_deductible="GBP 25,000 each and every loss; GBP 50,000 storm and flood",
    policy_insured_label="Principal insured",
    policy_number_label="Policy number",
    policy_title="Contractors All Risks Policy Schedule",
    policy_subtitle="Contract works, plant and third party liability",
    policy_interest_label="Contract works",
    policy_interest=(
        "All contract works undertaken by the principal insured, together with "
        "temporary works, materials on site, own and hired-in plant, and constructional "
        "plant, at any contract site within Great Britain notified to the insurer."
    ),
    policy_conditions=(
        "Storm and flood damage to temporary works, scaffolding and formwork is covered "
        "only where erection and bracing complied with the relevant standard. Warranted "
        "that scaffolding is inspected at intervals not exceeding seven days and after "
        "any event likely to have affected its stability."
    ),
    policy_extra=(
        ("Contract", "Kingsway Regeneration Scheme, Plot 4, Swansea"),
        ("Contract value", "GBP 18,400,000"),
        ("Contract period", "March 2026 to November 2027"),
        ("Own plant sub-limit", "GBP 2,000,000"),
    ),
    claimant="Ashworth Bellamy Construction Limited",
    claimant_contact="Owen Prydderch, Contracts Manager",
    claimant_email="o.prydderch@ashworthbellamy.example",
    date_of_loss="21 October 2026",
    time_of_loss="04:10 BST",
    loss_location="Plot 4, Kingsway Regeneration Scheme, Swansea SA1 8QR",
    loss_country="United Kingdom",
    cause="Storm — high winds causing collapse of scaffold and formwork",
    description=(
        "Storm Fenwick crossed south Wales overnight on 20/21 October with gusts "
        "recorded at 78mph at Mumbles Head. Scaffolding to the east elevation of the "
        "Plot 4 residential block collapsed at approximately 04:10, bringing down "
        "falsework and formwork to the level six slab which had been poured on 19 "
        "October and had not reached design strength. Two operatives arriving on site "
        "at 06:30 sustained minor injuries from wind-blown debris. The site was closed "
        "for four days and the Health and Safety Executive was notified."
    ),
    affected_assets=(
        "Scaffolding to the east elevation, falsework and formwork to the level six "
        "slab, reinforcement, and a tower crane rendered inoperable by debris."
    ),
    injuries="2",
    fatalities="0",
    business_interruption="No",
    structural_damage="Yes",
    environmental_exposure="No",
    authorities="Health and Safety Executive; Swansea Council building control",
    potential_litigation="No",
    incident_reference="HSE/RIDDOR/2026/22714",
    police_reference=None,
    asset_heading="Contract particulars",
    asset_rows=(
        ("Contract", "Kingsway Regeneration Scheme, Plot 4"),
        ("Site address", "Plot 4, Kingsway Regeneration Scheme, Swansea SA1 8QR"),
        ("Scheme", "96 residential units over ground floor commercial"),
        ("Contract value", "GBP 18,400,000"),
        ("Contract period", "March 2026 to November 2027"),
        ("Progress at date of loss", "Level six slab, approximately 42 per cent complete"),
        ("Scaffold contractor", "Gwenllian Access Systems Limited"),
    ),
    currency="GBP",
    estimated_loss="GBP 1,047,500",
    repair_estimate="GBP 688,200",
    repair_estimate_label="Reinstatement estimate",
    report_slug="ashworth-engineers-report",
    report_title="Consulting Engineer's Report",
    report_firm="Pentreath Meyrick Consulting Engineers",
    report_reference="PMC/2026/4407",
    report_author="Dr Huw Pentreath CEng MIStructE",
    report_instructions=(
        ("Date of instruction", "21 October 2026"),
        ("Date of attendance", "22 October 2026"),
        ("Attended with", "The contracts manager and the scaffold contractor"),
        ("Weather data source", "Met Office station, Mumbles Head"),
    ),
    report_circumstances=(
        "Storm Fenwick was named on 18 October and warnings were issued for south Wales "
        "from 19 October. The Met Office station at Mumbles Head recorded a maximum gust "
        "of 78mph at 04:05 on 21 October. The scaffold to the east elevation had been "
        "inspected on 16 October, within the seven-day requirement, and the inspection "
        "record notes no defects. The site had been secured on 20 October and no work "
        "took place overnight."
    ),
    report_findings=(
        "The scaffold failed at the tie positions on the east elevation between levels "
        "four and seven. Nine of the fourteen ties examined had been installed into "
        "blockwork rather than into the structural frame, contrary to the scaffold "
        "design drawing. In our opinion the wind loading was severe but within the "
        "design envelope, and the tie installation is the proximate cause of the "
        "collapse. The level six slab was poured on 19 October and had reached an "
        "estimated 60 per cent of design strength; core samples show local damage where "
        "falsework was displaced but the slab is repairable rather than requiring "
        "demolition."
    ),
    report_quantum=(
        ("Reinstatement estimate", "GBP 688,200"),
        ("Plant standing and crane recommissioning", "GBP 214,300"),
        ("Prolongation and preliminaries", "GBP 145,000"),
    ),
    report_comment_heading="Recovery",
    report_comment=(
        "The tie installation departs from the scaffold design drawing issued by the "
        "scaffold contractor's own designer. Subject to the terms of the subcontract, "
        "there appears to be a recovery against Gwenllian Access Systems Limited and its "
        "public liability insurer. We recommend that the tie positions be photographed "
        "and preserved before the scaffold is cleared."
    ),
    report_recommendation=(
        "That a reserve of GBP 1,047,500 be established, that the damaged scaffold be "
        "preserved for inspection by the scaffold contractor's insurer, and that core "
        "testing of the level six slab be completed before reinstatement is authorised."
    ),
    schedule_slug="ashworth-reinstatement-schedule",
    schedule_title="Reinstatement cost schedule",
    schedule_headers=(
        "Section",
        "Code",
        "Description",
        "Quantity",
        "Unit cost GBP",
        "Line total GBP",
    ),
    schedule_rows=(
        (
            "Access",
            "AC-SCF-01",
            "Scaffold removal and replacement, east elevation",
            "1",
            "164800",
            "164800",
        ),
        ("Temporary works", "TW-FRM-02", "Falsework and formwork renewal", "1", "121500", "121500"),
        (
            "Structure",
            "ST-SLB-03",
            "Slab investigation, coring and remedial concrete",
            "1",
            "148300",
            "148300",
        ),
        ("Structure", "ST-RBR-04", "Reinforcement replacement, level six", "1", "63900", "63900"),
        ("Plant", "PL-CRN-05", "Crane and plant standing time", "1", "74200", "74200"),
        ("Site", "SI-CLR-06", "Site clearance and debris disposal", "1", "42600", "42600"),
        ("Survey", "SV-SET-07", "Re-survey and re-setting out", "1", "32900", "32900"),
        ("Protection", "PR-WTH-08", "Temporary weather protection", "1", "40000", "40000"),
    ),
    schedule_total_label="Reinstatement total",
    email_subject="FNOL - Ashworth Bellamy - storm damage Plot 4 Swansea - CAR-2026-70155",
    email_to="fnol@carrier.example",
    email_cc="construction.claims@trenthamrisk.example",
    email_date="Wed, 21 Oct 2026 11:36:47 +0100",
    email_message_id="trp-car-2026-0937-a@trenthamrisk.example",
    email_opening=(
        "Good morning,\n\n"
        "We are instructed to notify storm damage on behalf of our client Ashworth "
        "Bellamy Construction Limited. Our reference is TRP/CAR/2026/0937."
    ),
    email_narrative=(
        "Storm Fenwick crossed south Wales overnight with gusts of 78mph recorded at\n"
        "Mumbles Head. Scaffolding to the east elevation of the Plot 4 block collapsed\n"
        "at around 04:10, bringing down falsework and formwork to the level six slab\n"
        "which had been poured two days earlier.\n\n"
        "Two operatives arriving on site sustained minor injuries from wind-blown debris\n"
        "and the HSE has been notified under RIDDOR. The site is closed and the tower\n"
        "crane is out of action.\n\n"
        "Pentreath Meyrick attended on 22 October. Their report is attached and is worth\n"
        "reading in full: nine of the fourteen scaffold ties they examined were installed\n"
        "into blockwork rather than the structural frame, contrary to the scaffold\n"
        "design. They consider that the proximate cause and there may be a recovery\n"
        "against the scaffold contractor."
    ),
    email_closing=(
        "Please confirm a claim reference and let me know whether you wish to appoint\n"
        "your own engineer. I am on +44 121 496 5540."
    ),
    email_facts=(
        ("Policy number", "CAR-2026-70155"),
        ("Insured name", "Ashworth Bellamy Construction Limited"),
        ("Date of loss", "21 October 2026"),
        ("Loss location", "Plot 4, Kingsway Regeneration Scheme, Swansea SA1 8QR"),
        ("Cause", "Storm - high winds causing collapse of scaffold and formwork"),
        ("Estimated loss", "GBP 1,047,500"),
        ("Repair estimate", "GBP 688,200"),
    ),
)


SCENARIOS: tuple[Scenario, ...] = (NORTHGATE, PENNINE, CALDERWOOD, VERITY, ASHWORTH)


def build(scenario: Scenario) -> None:
    scenario.directory.mkdir(parents=True, exist_ok=True)
    build_loss_notice(scenario)
    build_policy_document(scenario)
    build_report(scenario)
    build_schedule(scenario)
    build_email(scenario)
    build_readme(scenario)


def main() -> None:
    for scenario in SCENARIOS:
        build(scenario)
        print(f"{scenario.slug}:")
        for name in sorted(path.name for path in scenario.directory.iterdir()):
            print(f"  demo-data/{scenario.slug}/{name}")


if __name__ == "__main__":
    main()
