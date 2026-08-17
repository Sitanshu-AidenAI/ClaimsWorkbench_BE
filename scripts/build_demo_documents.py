#!/usr/bin/env python
"""Build the demo scenario's PDFs.

The scenario is one marine cargo claim told by four documents that agree with
each other, plus a covering email — the shape a real notification arrives in, and
the shape the extraction pipeline is built to read.

The PDFs are committed alongside this script rather than generated on demand, so
the demo runs with no build step; this exists so they are reviewable and
reproducible rather than opaque binaries nobody can correct. Re-running it
overwrites them byte-for-byte apart from the creation date reportlab stamps.

    uv run python scripts/build_demo_documents.py

Every fact below is invented. The company names, the vessel, the policy number
and the people are not real, and the email domains use `.example`, which is
reserved by RFC 2606 and can never be registered.
"""

from __future__ import annotations

from pathlib import Path

from demo_pdf import footer as _footer
from demo_pdf import header as _header
from demo_pdf import new_canvas
from demo_pdf import pair as _pair
from demo_pdf import paragraph as _paragraph
from demo_pdf import section as _section

#: Where the demo set lives, beside the mail-intake scenario that predates it.
DEMO_DIR = Path(__file__).resolve().parent.parent / "demo-data" / "document-intelligence"

# ---------------------------------------------------------------------------
# The scenario, stated once
#
# Held here as constants rather than typed into each document, because the whole
# point of a multi-document scenario is that the documents agree: a policy number
# that differs by a digit between the notice and the schedule would make every
# extraction look wrong and be nobody's fault but this file's.
# ---------------------------------------------------------------------------

BROKER = "Calder & Finch (Marine) Limited"
HANDLER = "Marianne Okafor"
HANDLER_ROLE = "Claims Account Handler"
HANDLER_EMAIL = "marianne.okafor@calderfinch.example"
HANDLER_PHONE = "+44 20 7946 0521"
BROKER_REFERENCE = "CF/MAR/2026/0884"

INSURED = "Ravensgate Marine Logistics Limited"
POLICY_NUMBER = "MAR-2026-77413"
POLICY_TYPE = "Marine Cargo — Open Cover"
POLICY_PERIOD = "01 January 2026 to 31 December 2026"
POLICY_LIMIT = "GBP 7,500,000 any one conveyance"
POLICY_DEDUCTIBLE = "GBP 25,000 each and every loss"

VESSEL = "MV Corsair Meridian"
IMO = "9483721"
VOYAGE = "CM-2607E"

DATE_OF_LOSS = "29 July 2026"
TIME_OF_LOSS = "03:40 UTC"
LOSS_LOCATION = "Berth 8, Port of Felixstowe, Suffolk IP11 3SY"
LOSS_COUNTRY = "United Kingdom"
CAUSE = "Heavy weather — container stow collapse"
INCIDENT_REFERENCE = "FXT/INC/2026/2214"

DESCRIPTION = (
    "The vessel encountered a severe south-westerly gale in the southern North Sea "
    "during the night of 28/29 July 2026. Lashings on bay 34 parted and the container "
    "stow partially collapsed. Three 40ft containers of packaged textile machinery "
    "were breached and took seawater over an extended period before the vessel berthed "
    "at Felixstowe. Damage was discovered on discharge on 29 July 2026."
)
AFFECTED_ASSETS = (
    "Three 40ft containers (RVGU4471820, RVGU4471835, RVGU4472104) carrying packaged "
    "textile finishing machinery and spare parts."
)

ESTIMATED_LOSS = "GBP 486,500"
REPAIR_ESTIMATE = "GBP 312,480"
CURRENCY = "GBP"


# ---------------------------------------------------------------------------
# 1. The loss notice — the document a citation is most often drawn from
# ---------------------------------------------------------------------------


def build_loss_notice(path: Path) -> None:
    """A completed FNOL form, deliberately spread over two pages.

    The split is the point rather than a consequence of length: the policy and
    the people are on page 1 and the loss and the money are on page 2, so a
    review that opens the right page for the right field is visibly doing
    something a whole-document search could not.
    """
    pdf = new_canvas(str(path), "First Notification of Loss")

    # --- Page 1: who is telling us, and under what policy --------------------
    y = _header(
        pdf,
        "FIRST NOTIFICATION OF LOSS",
        f"Submitted by {BROKER} · Broker reference {BROKER_REFERENCE}",
    )

    y = _section(pdf, y, "NOTIFICATION")
    y = _pair(pdf, y, "Reported by", HANDLER)
    y = _pair(pdf, y, "Reporting organisation", BROKER)
    y = _pair(pdf, y, "Role", HANDLER_ROLE)
    y = _pair(pdf, y, "Email", HANDLER_EMAIL)
    y = _pair(pdf, y, "Telephone", HANDLER_PHONE)
    y = _pair(pdf, y, "Date reported", "29 July 2026")
    y -= 12

    y = _section(pdf, y, "POLICY")
    y = _pair(pdf, y, "Policy number", POLICY_NUMBER)
    y = _pair(pdf, y, "Insured name", INSURED)
    y = _pair(pdf, y, "Insured organisation", INSURED)
    y = _pair(pdf, y, "Policy type", POLICY_TYPE)
    y = _pair(pdf, y, "Period of cover", POLICY_PERIOD)
    y -= 12

    y = _section(pdf, y, "CLAIMANT")
    y = _pair(pdf, y, "Claimant name", INSURED)
    y = _pair(pdf, y, "Claimant contact", "Douglas Ferrier, Logistics Director")
    y = _pair(pdf, y, "Claimant email", "d.ferrier@ravensgate-marine.example")

    _footer(pdf, f"{BROKER} · {BROKER_REFERENCE}", 1, 2)
    pdf.showPage()

    # --- Page 2: what happened, and what it is thought to cost ---------------
    y = _header(
        pdf,
        "FIRST NOTIFICATION OF LOSS (CONTINUED)",
        f"Policy {POLICY_NUMBER} · {INSURED}",
    )

    y = _section(pdf, y, "THE LOSS")
    y = _pair(pdf, y, "Date of loss", DATE_OF_LOSS)
    y = _pair(pdf, y, "Time of loss", TIME_OF_LOSS)
    y = _pair(pdf, y, "Loss location", LOSS_LOCATION)
    y = _pair(pdf, y, "Country", LOSS_COUNTRY)
    y = _pair(pdf, y, "Cause of loss", CAUSE)
    y = _pair(pdf, y, "Carrying vessel", f"{VESSEL} (IMO {IMO})")
    y = _pair(pdf, y, "Voyage", VOYAGE)
    y -= 6

    y = _paragraph(pdf, y, "Description of loss", DESCRIPTION)
    y = _paragraph(pdf, y, "Property affected", AFFECTED_ASSETS)

    y = _pair(pdf, y, "Injuries", "0")
    y = _pair(pdf, y, "Fatalities", "0")
    y = _pair(pdf, y, "Business interruption", "Yes")
    y = _pair(pdf, y, "Structural damage", "No")
    y = _pair(pdf, y, "Environmental exposure", "No")
    y -= 12

    y = _section(pdf, y, "FINANCIAL")
    y = _pair(pdf, y, "Currency", CURRENCY)
    y = _pair(pdf, y, "Estimated loss", ESTIMATED_LOSS)
    y = _pair(pdf, y, "Repair estimate", REPAIR_ESTIMATE)
    y -= 12

    y = _section(pdf, y, "ADDITIONAL")
    y = _pair(pdf, y, "Incident reference", INCIDENT_REFERENCE)
    y = _pair(pdf, y, "Authorities involved", "Port of Felixstowe Harbour Authority")
    y = _pair(pdf, y, "Potential litigation", "No")

    _footer(pdf, f"{BROKER} · {BROKER_REFERENCE}", 2, 2)
    pdf.save()


# ---------------------------------------------------------------------------
# 2. The policy schedule — the same policy, said by a different document
# ---------------------------------------------------------------------------


def build_policy_schedule(path: Path) -> None:
    """The cover the claim is made under.

    Carries the policy facts a second time and in a different vocabulary
    ("Assured" rather than "Insured name"), which is what the dataset's aliases
    are for and what makes retrieval rather than pattern-matching the thing being
    exercised.
    """
    pdf = new_canvas(str(path), "Certificate of Marine Cargo Insurance")

    y = _header(
        pdf,
        "CERTIFICATE OF MARINE CARGO INSURANCE",
        "Issued under open cover — this certificate is evidence of insurance",
    )

    y = _section(pdf, y, "THE COVER")
    y = _pair(pdf, y, "Certificate number", POLICY_NUMBER)
    y = _pair(pdf, y, "Assured", INSURED)
    y = _pair(pdf, y, "Class of business", POLICY_TYPE)
    y = _pair(pdf, y, "Period of insurance", POLICY_PERIOD)
    y = _pair(pdf, y, "Limit of liability", POLICY_LIMIT)
    y = _pair(pdf, y, "Deductible", POLICY_DEDUCTIBLE)
    y = _pair(pdf, y, "Broker", BROKER)
    y = _pair(pdf, y, "Broker reference", BROKER_REFERENCE)
    y -= 12

    y = _section(pdf, y, "THE INTEREST INSURED")
    y = _paragraph(
        pdf,
        y,
        "Interest",
        "Textile finishing machinery, spare parts and ancillary equipment, packed in "
        "containers, whilst in transit by sea, road and rail anywhere in the world, "
        "including whilst in store in the ordinary course of transit.",
    )
    y = _pair(pdf, y, "Conveyance", f"{VESSEL} (IMO {IMO}), voyage {VOYAGE}")
    y = _pair(pdf, y, "Voyage from", "Gdansk, Poland")
    y = _pair(pdf, y, "Voyage to", "Felixstowe, United Kingdom")
    y -= 12

    y = _section(pdf, y, "CONDITIONS")
    y = _paragraph(
        pdf,
        y,
        "Conditions",
        "Institute Cargo Clauses (A). Institute War Clauses (Cargo). Institute Strikes "
        "Clauses (Cargo). Claims payable in the United Kingdom in Pounds Sterling.",
    )

    _footer(pdf, f"{BROKER} · Certificate {POLICY_NUMBER}", 1, 1)
    pdf.save()


# ---------------------------------------------------------------------------
# 3. The surveyor's report — the same facts, three pages apart
# ---------------------------------------------------------------------------


def build_survey_report(path: Path) -> None:
    """A preliminary marine survey, with the quantum on the last page.

    The document that makes the citation worth having: an adjuster's figure sits
    on page 3 under a heading nobody would guess, and the covering email quotes it
    without saying where it came from. A review that shows page 3 is showing the
    source; one that shows the email is showing a copy of it.
    """
    pdf = new_canvas(str(path), "Preliminary Survey Report")

    # --- Page 1: instructions and what was surveyed --------------------------
    y = _header(
        pdf,
        "PRELIMINARY SURVEY REPORT",
        "Thurlow Marine Surveyors LLP · Our reference TMS/2026/1188",
    )

    y = _section(pdf, y, "INSTRUCTIONS")
    y = _pair(pdf, y, "Instructed by", BROKER)
    y = _pair(pdf, y, "On behalf of", INSURED)
    y = _pair(pdf, y, "Certificate number", POLICY_NUMBER)
    y = _pair(pdf, y, "Date of instruction", "29 July 2026")
    y = _pair(pdf, y, "Date of attendance", "30 July 2026")
    y = _pair(pdf, y, "Surveyor", "Captain R. J. Thurlow, MNI")
    y -= 12

    y = _section(pdf, y, "VESSEL AND VOYAGE PARTICULARS")
    y = _pair(pdf, y, "Vessel name", VESSEL)
    y = _pair(pdf, y, "IMO number", IMO)
    y = _pair(pdf, y, "Voyage number", VOYAGE)
    y = _pair(pdf, y, "Port of loading", "Gdansk, Poland")
    y = _pair(pdf, y, "Port of discharge", "Felixstowe, United Kingdom")
    y = _pair(pdf, y, "Date of discharge", "29 July 2026")
    y = _pair(pdf, y, "Place of survey", LOSS_LOCATION)

    _footer(pdf, "Thurlow Marine Surveyors LLP · TMS/2026/1188", 1, 3)
    pdf.showPage()

    # --- Page 2: what happened, and why --------------------------------------
    y = _header(pdf, "PRELIMINARY SURVEY REPORT", "Circumstances and findings")

    y = _section(pdf, y, "CIRCUMSTANCES")
    y = _paragraph(pdf, y, "Circumstances", DESCRIPTION)

    y = _section(pdf, y, "FINDINGS ON ATTENDANCE")
    y = _paragraph(
        pdf,
        y,
        "Findings",
        "All three containers showed distortion to the roof panels and door seals. "
        "Free water was present on the container floors to a depth of approximately "
        "40mm. Packaged machinery showed salt staining to unpainted surfaces and to "
        "electrical enclosures. Corrosion had begun on exposed bright steel. Seven "
        "control cabinets were found to have taken water internally.",
    )

    y = _pair(pdf, y, "Date of loss", DATE_OF_LOSS)
    y = _pair(pdf, y, "Cause of loss", CAUSE)
    y = _pair(pdf, y, "Proximate cause", "Perils of the sea — heavy weather")
    y = _pair(pdf, y, "Incident reference", INCIDENT_REFERENCE)
    y = _pair(pdf, y, "Injuries reported", "None")
    y = _pair(pdf, y, "Pollution observed", "None")

    _footer(pdf, "Thurlow Marine Surveyors LLP · TMS/2026/1188", 2, 3)
    pdf.showPage()

    # --- Page 3: the money ---------------------------------------------------
    y = _header(pdf, "PRELIMINARY SURVEY REPORT", "Preliminary quantum and reserve advice")

    y = _section(pdf, y, "PRELIMINARY QUANTUM")
    y = _paragraph(
        pdf,
        y,
        "Basis",
        "Figures below are preliminary and are given for reserving purposes only. They "
        "are subject to a full repair specification from the manufacturer and to "
        "salvage being tested at market.",
    )

    y = _pair(pdf, y, "Sound market value", "GBP 1,240,000")
    y = _pair(pdf, y, "Repair estimate", REPAIR_ESTIMATE)
    y = _pair(pdf, y, "Salvage allowance", "GBP 42,000")
    y = _pair(pdf, y, "Estimated loss", ESTIMATED_LOSS)
    y = _pair(pdf, y, "Currency", CURRENCY)
    y = _pair(pdf, y, "Deductible applicable", POLICY_DEDUCTIBLE)
    y -= 12

    y = _section(pdf, y, "BUSINESS INTERRUPTION")
    y = _paragraph(
        pdf,
        y,
        "Comment",
        "The assured advises that the machinery was sold forward to a customer in "
        "Bradford with an installation date of 12 August 2026. A delay of six to eight "
        "weeks is expected. A business interruption element is therefore anticipated "
        "but is not quantified in this report.",
    )

    y = _section(pdf, y, "RECOMMENDATION")
    y = _paragraph(
        pdf,
        y,
        "Recommendation",
        "That a reserve of GBP 486,500 be established, that the manufacturer be "
        "instructed to produce a full repair specification, and that recovery against "
        "the carrier be reserved pending sight of the vessel's weather routeing "
        "records and lashing certificates.",
    )

    _footer(pdf, "Thurlow Marine Surveyors LLP · TMS/2026/1188", 3, 3)
    pdf.save()


def main() -> None:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)

    build_loss_notice(DEMO_DIR / "harbourline-loss-notice.pdf")
    build_policy_schedule(DEMO_DIR / "harbourline-policy-schedule.pdf")
    build_survey_report(DEMO_DIR / "harbourline-survey-report.pdf")

    for name in sorted(path.name for path in DEMO_DIR.glob("*.pdf")):
        print(f"wrote demo-data/document-intelligence/{name}")


if __name__ == "__main__":
    main()
