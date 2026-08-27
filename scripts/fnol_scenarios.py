"""The 24 FNOL scenarios, one dict per pack.

`Scenario` is imported from `build_demo_packs` so these packs are the same shape
as the five in `demo-data/`. The `policy_*` fields on that dataclass drive
`build_policy_document`, which this dataset deliberately does not call — the
wording belongs to the library in `policy/`, not to the notice — so `pack()`
fills them with placeholders rather than making every scenario state them.

Each dict carries the answer key beside the scenario: `expected`, `confidence`,
`why` and `exercises`. The README generator reads them, and
`case_data/_ground_truth/` is generated from the same source, so the pack and the
answer key cannot drift apart.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_demo_packs import Scenario

Rows = tuple[tuple[str, str], ...]

#: Placeholders for the fields only `build_policy_document` reads. Stated once
#: here rather than in twenty-four scenarios that never use them.
_POLICY_DOC_UNUSED = {
    "policy_insured_label": "Named insured",
    "policy_number_label": "Policy number",
    "policy_title": "",
    "policy_subtitle": "",
    "policy_interest_label": "Interest insured",
    "policy_interest": "",
    "policy_conditions": "",
    "policy_extra": (),
}


def pack(*, expected: str, confidence: str, why: str,
         exercises: tuple[str, ...], **kw) -> dict:
    """One pack: a `Scenario` plus the answer it is supposed to produce."""
    kw.setdefault("loss_country", "United States")
    kw.setdefault("currency", "USD")
    kw.setdefault("injuries", "0")
    kw.setdefault("fatalities", "0")
    kw.setdefault("police_reference", None)
    kw.setdefault("potential_litigation", "None indicated")
    kw.setdefault("email_cc", "")
    kw.setdefault("repair_estimate_label", "Estimated cost")
    kw.setdefault("schedule_total_label", "Total")
    return {
        "scenario": Scenario(**_POLICY_DOC_UNUSED, **kw),
        "expected": expected,
        "confidence": confidence,
        "why": why,
        "exercises": exercises,
    }


SPECS: list[dict] = []

# ---------------------------------------------------------------------------
# 1 — Harborline, ammonia release. Policy number stated. Clean match.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="harborline-ammonia-release",
    title="Harborline Cold Storage — ammonia release and product loss",
    line_of_business="Commercial property",
    broker="Talbot & Rennick Insurance Brokers, Inc.",
    handler="Diane Whitcomb-Reyes", handler_role="Account Executive",
    handler_email="d.whitcomb-reyes@talbotrennick.example",
    handler_phone="+1 410 555 0182",
    broker_reference="TR/PROP/2025/4471",
    reported_on="12 January 2026",
    insured="Harborline Cold Storage & Logistics, LLC",
    policy_number="CP-4471-88210",
    policy_type="Commercial Property",
    policy_period="1 March 2025 to 1 March 2026",
    policy_limit="USD 44,650,000", policy_deductible="USD 100,000",
    claimant="Harborline Cold Storage & Logistics, LLC",
    claimant_contact="Nathaniel Osei-Barrow, VP Operations",
    claimant_email="n.osei-barrow@harborlinecold.example",
    date_of_loss="10 January 2026", time_of_loss="04:20 EST",
    loss_location="2870 Patapsco Industrial Parkway, Baltimore, MD 21226",
    cause="Ammonia release — weld failure on a liquid ammonia header",
    description=(
        "A welded elbow on the 4-inch liquid ammonia header serving the low-temp "
        "evaporators in Rooms 4 to 6 failed overnight. Plant detection alarmed at "
        "04:18 and the refrigeration plant went into automatic shutdown as designed. "
        "Ammonia entered the three freezer rooms and the customer-owned product in "
        "them is presumed contaminated pending testing. With the plant down, room "
        "temperatures rose from -10F to +26F over 31 hours before rental cooling was "
        "cut in. A defrost line split as the plant was isolated and thawed, putting "
        "water through the engine room and the electrical room housing the compressor "
        "drive panels."),
    affected_assets=(
        "Customer-owned frozen product in Rooms 4, 5 and 6; the insured's own stock; "
        "two compressor VFD cabinets; approximately 3,600 sq ft of engine room floor; "
        "insulated metal panel ceiling in Room 5"),
    business_interruption="Yes — Rooms 4 to 6 out of service, rental cooling in place",
    structural_damage="Yes — ceiling panel delamination in Room 5",
    environmental_exposure="Yes — anhydrous ammonia release, HAZMAT attended",
    authorities="Baltimore City Fire Department HAZMAT",
    incident_reference="BCFD-2026-001188",
    asset_heading="Affected rooms and plant",
    asset_rows=(
        ("Room 4 — freezer", "Customer product, ammonia contamination"),
        ("Room 5 — freezer", "Customer product, ceiling panel damage"),
        ("Room 6 — freezer", "Customer product, ammonia contamination"),
        ("Engine room", "Water damage, 3,600 sq ft"),
        ("Electrical room", "Two compressor VFD cabinets wetted"),
    ),
    estimated_loss="3,700,000", repair_estimate="1,410,000",
    repair_estimate_label="Reinstatement and plant repair",
    report_slug="harborline-refrigeration-engineers-report",
    report_title="Refrigeration engineer's report",
    report_firm="Cold Chain Mechanical Group",
    report_reference="CCMG-2026-0114",
    report_author="Ing. Marguerite Delacroix-Iwu, PE",
    report_instructions=(
        ("Instructed by", "Talbot & Rennick Insurance Brokers, Inc."),
        ("Instructed on", "11 January 2026"),
        ("Site attendance", "11 and 12 January 2026"),
        ("Scope", "Cause of the header failure and the extent of plant damage"),
    ),
    report_circumstances=(
        "The plant is a two-stage anhydrous ammonia system serving five freezer rooms "
        "and a dock. The failure occurred at a welded elbow in the ceiling space of "
        "Room 5, on a header installed as part of an evaporator retrofit completed in "
        "2023. Detection alarmed at 25 ppm and the plant tripped at 150 ppm, both as "
        "designed."),
    report_findings=(
        "The failed elbow shows a circumferential crack initiating at the weld toe on "
        "the inside radius. The weld cap is undercut over approximately 40% of its "
        "circumference and the root pass is incompletely fused across a 22 mm arc. "
        "The failure is consistent with fatigue propagating from a weld defect under "
        "normal pressure cycling, not with overpressure. The section has been cut out, "
        "tagged and retained. The protective safeguards required by the policy — "
        "detection, automatic shutdown and room temperature alarms — were all in "
        "service and functioned; the release was limited by them rather than worsened."),
    report_quantum=(
        ("Ammonia piping repair and re-commissioning", "USD 186,000"),
        ("Two compressor VFD cabinets", "USD 148,000"),
        ("Evaporator coil cleaning and re-certification", "USD 86,000"),
        ("Engine room and electrical room reinstatement", "USD 310,000"),
        ("Room 5 insulated panel ceiling", "USD 120,000"),
    ),
    report_comment_heading="Comment on the 2023 retrofit",
    report_comment=(
        "The retrofit was carried out by Northern Bay Refrigeration Services. The weld "
        "in question is theirs. Their weld procedure qualification records and the "
        "radiographic inspection reports for that scope should be preserved; on the "
        "evidence available a recovery against that contractor is worth pursuing."),
    report_recommendation=(
        "Do not return the affected rooms to service until the replacement spool has "
        "been radiographed and the system has held a 24-hour pressure test."),
    schedule_slug="harborline-product-loss-schedule",
    schedule_title="Product loss schedule",
    schedule_headers=("Room", "Customer", "Pallets", "Description", "Value USD"),
    schedule_rows=(
        ("Room 4", "Cedarbrook Provisions", "412", "Frozen poultry, case-ready", "684000"),
        ("Room 4", "Harborline own stock", "88", "Packaging and consumables", "62000"),
        ("Room 5", "Marrow & Vine Foods", "506", "Prepared meals, frozen", "910000"),
        ("Room 5", "Cedarbrook Provisions", "134", "Frozen beef primals", "388000"),
        ("Room 6", "Tidewater Seafood Co.", "298", "Frozen shellfish", "604000"),
        ("Room 6", "Harborline own stock", "51", "Corrugate and film", "34000"),
    ),
    schedule_total_label="Total product exposure",
    email_subject="FNOL - Harborline Cold Storage - ammonia release, Baltimore MD - CP-4471-88210",
    email_to="claims@meridianatlantic-ins.example",
    email_cc="claims.desk@talbotrennick.example",
    email_date="Mon, 12 Jan 2026 07:41:08 -0500",
    email_message_id="tr-prop-2026-4471-a@talbotrennick.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify an ammonia release and consequent "
        "product loss on behalf of our client Harborline Cold Storage & Logistics, LLC. "
        "Our reference is TR/PROP/2025/4471."),
    email_facts=(
        ("Policy number", "CP-4471-88210"),
        ("Insured name", "Harborline Cold Storage & Logistics, LLC"),
        ("Date of loss", "10 January 2026"),
        ("Loss location", "2870 Patapsco Industrial Parkway, Baltimore, MD 21226"),
        ("Cause", "Ammonia release - weld failure on a liquid ammonia header"),
        ("Estimated loss", "USD 3,700,000"),
    ),
    email_narrative=(
        "A welded elbow on the liquid ammonia header failed overnight on Saturday. The\n"
        "plant tripped as designed and Baltimore City Fire HAZMAT attended. Product in\n"
        "three freezer rooms is presumed contaminated and a 31-hour temperature\n"
        "excursion followed before rental cooling was cut in.\n\n"
        "Most of the product at risk is customer-owned and stored under warehousing\n"
        "agreements, so there is a bailee element as well as our client's own stock.\n"
        "Nine of their customers have been notified. There is also water damage in the\n"
        "engine room from a defrost line that split during isolation.\n\n"
        "Emergency mitigation is running at about USD 140,000 to date and is ongoing."),
    email_closing=(
        "Please confirm a claim reference and an adjuster today - our client has product\n"
        "at risk and a customer notification obligation running. I am on +1 410 555 0182\n"
        "if it is easier to talk it through."),
    expected="CP-4471-88210", confidence="Exact",
    why=("The policy number is stated and every corroborating axis agrees: insured name, "
         "the loss address is scheduled Location 001, the broker reference is the one on "
         "the policy, and the sender domain is the policy's broker domain. This is the "
         "control case — if this does not match, nothing will."),
    exercises=(
        "**Every signal firing at once.** Policy number, broker reference, insured name, "
        "risk location, broker domain and policy period all agree. The ladder should reach "
        "`exact` on the number alone and be corroborated on five further axes.",
        "**Bailee exposure.** Most of the product is customer-owned, so the value at risk "
        "sits under the personal-property-of-others limit rather than the insured's own stock.",
        "**A figure stated twice.** USD 3,700,000 appears in the covering email and on the "
        "loss notice; the product schedule totals separately. A citation naming the right "
        "document is doing real work.",
    ),
))

# ---------------------------------------------------------------------------
# 2 — Windrow Grove, hail and wind. Policy number stated. Clean match.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="windrow-grove-hail",
    title="Windrow Grove Apartments — hail and straight-line wind",
    line_of_business="Commercial property",
    broker="Kettleridge Risk Partners, LLC",
    handler="Marcus Adeyemi-Croft", handler_role="Producer",
    handler_email="m.adeyemi-croft@kettleridgerisk.example",
    handler_phone="+1 918 555 0347",
    broker_reference="KRP/HAB/2025/7735",
    reported_on="22 April 2026",
    insured="Sundale Property Group, LLC",
    policy_number="CP-7735-19042",
    policy_type="Commercial Property - Habitational",
    policy_period="15 June 2025 to 15 June 2026",
    policy_limit="USD 40,210,000", policy_deductible="2% per affected building, min USD 100,000",
    claimant="Windrow Grove Apartments, LP",
    claimant_contact="Priyanka Raghavan-Doyle, Regional Portfolio Manager",
    claimant_email="p.raghavan-doyle@sundalepg.example",
    date_of_loss="19 April 2026", time_of_loss="19:45 CDT",
    loss_location="6120 East 91st Street, Tulsa, OK 74137",
    cause="Hail and straight-line wind",
    description=(
        "A severe thunderstorm crossed south Tulsa on the evening of 19 April producing "
        "hail to 2.25 inches and measured gusts of 71 mph at the Jones Riverside ASOS "
        "4.6 miles west. Six residential roofs show dense hail bruising with mat "
        "fracture on test squares and multiple wind-lifted and creased tabs. "
        "Approximately 240 sq ft of shingles and decking were removed from the north "
        "slope of Building 05, letting water into four upper-floor units with ceiling "
        "collapse in two of them. The clubhouse skylight shattered and nine of "
        "thirty-six carport bays were crushed, one onto a resident vehicle."),
    affected_assets=(
        "Composition shingle roofs on Buildings 03, 04, 05, 06, 08 and 11; units 5-301, "
        "5-303, 5-305 and 5-307; 31 HVAC condensing units; the clubhouse skylight and "
        "1,100 sq ft of flooring; nine carport bays; vinyl siding on three west elevations"),
    business_interruption="Yes — four units uninhabitable, rental value exposure",
    structural_damage="Yes — decking loss and ceiling collapse in two units",
    environmental_exposure="No",
    authorities="None attended",
    incident_reference="NWS-TSA-2026-0419",
    asset_heading="Affected buildings",
    asset_rows=(
        ("Buildings 03, 04, 06, 08, 11", "Hail bruising with mat fracture, creased tabs"),
        ("Building 05", "240 sq ft decking loss, water into four units"),
        ("Building 13 — clubhouse", "Skylight shattered, 1,100 sq ft flooring"),
        ("Building 15 — carports", "Nine of 36 bays crushed or displaced"),
    ),
    estimated_loss="2,400,000", repair_estimate="1,986,000",
    repair_estimate_label="Reinstatement estimate",
    report_slug="windrow-grove-roof-consultants-report",
    report_title="Roof consultant's report",
    report_firm="Ridgeline Roof Consultants",
    report_reference="RRC-2026-0442",
    report_author="Étienne Baumgartner-Osei, RRO",
    report_instructions=(
        ("Instructed by", "Kettleridge Risk Partners, LLC"),
        ("Instructed on", "20 April 2026"),
        ("Site attendance", "20 and 21 April 2026"),
        ("Scope", "Hail and wind damage assessment across all fifteen roofs"),
    ),
    report_circumstances=(
        "The community comprises twelve three-storey residential buildings, a clubhouse, "
        "a maintenance building and detached carports, built in 2004. All composition "
        "shingle roofs were replaced in 2019. Drone imagery was flown over all fifteen "
        "roofs and 4 ft by 4 ft test squares were cut on Buildings 03, 05 and 11."),
    report_findings=(
        "Test squares on Buildings 03, 05 and 11 returned 9, 11 and 8 hail impacts "
        "respectively per square, with mat fracture confirmed on 24 of the 28 strikes "
        "examined. Mat fracture is functional damage, not cosmetic marring: the mat is "
        "the water-shedding layer and a fractured mat will fail within one to three "
        "seasons regardless of appearance. Buildings 04, 06 and 08 show comparable "
        "density on drone imagery and warrant the same conclusion. Buildings 01, 02, "
        "07, 09, 10, 12 and 14 show granule loss without mat fracture and are, in this "
        "consultant's opinion, cosmetic only. Full replacement is indicated on six "
        "buildings, not fifteen."),
    report_quantum=(
        ("Roof replacement, six buildings", "USD 1,284,000"),
        ("Building 05 decking, interior repair, four units", "USD 268,000"),
        ("Clubhouse skylight and interior", "USD 96,000"),
        ("Carport structures, nine bays", "USD 142,000"),
        ("HVAC condensing units, 31 off", "USD 118,000"),
        ("Siding, signage, site lighting, landscaping", "USD 78,000"),
    ),
    report_comment_heading="On the cosmetic damage exclusion",
    report_comment=(
        "The policy carries a cosmetic damage exclusion for roof surfacing. That "
        "exclusion is written to defeat claims for appearance, and this consultant's "
        "test squares distinguish the two conditions on evidence rather than assertion: "
        "the six buildings claimed exhibit mat fracture and the nine excluded from the "
        "claim do not."),
    report_recommendation=(
        "Replace the six identified roofs to current code including ice and water "
        "shield at the eaves. Re-inspect the remaining nine at the next renewal."),
    schedule_slug="windrow-grove-reinstatement-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Building", "Element", "Quantity", "Unit", "Total USD"),
    schedule_rows=(
        ("03", "Composition shingle roof replacement", "27400", "sq ft", "214000"),
        ("04", "Composition shingle roof replacement", "27400", "sq ft", "214000"),
        ("05", "Roof replacement including decking", "27400", "sq ft", "242000"),
        ("06", "Composition shingle roof replacement", "27400", "sq ft", "214000"),
        ("08", "Composition shingle roof replacement", "27400", "sq ft", "214000"),
        ("11", "Composition shingle roof replacement", "27400", "sq ft", "186000"),
        ("05", "Interior reinstatement, four units", "4", "units", "268000"),
        ("13", "Clubhouse skylight and flooring", "1", "item", "96000"),
        ("15", "Carport structures", "9", "bays", "142000"),
        ("Site", "HVAC condensers, siding, signage, lighting", "1", "lot", "196000"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="FNOL - Windrow Grove Apartments - hail and wind, Tulsa OK - CP-7735-19042",
    email_to="newloss@cascadiamutual.example",
    email_cc="p.raghavan-doyle@sundalepg.example",
    email_date="Wed, 22 Apr 2026 09:12:44 -0500",
    email_message_id="krp-hab-2026-7735-a@kettleridgerisk.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify hail and wind damage on behalf of "
        "our client Sundale Property Group, LLC at their Windrow Grove community. Our "
        "reference is KRP/HAB/2025/7735."),
    email_facts=(
        ("Policy number", "CP-7735-19042"),
        ("Insured name", "Sundale Property Group, LLC"),
        ("Date of loss", "19 April 2026"),
        ("Loss location", "6120 East 91st Street, Tulsa, OK 74137"),
        ("Cause", "Hail and straight-line wind"),
        ("Estimated loss", "USD 2,400,000"),
    ),
    email_narrative=(
        "A severe thunderstorm crossed south Tulsa on Sunday evening. Hail to 2.25\n"
        "inches and gusts to 71 mph were recorded nearby. Six of the twelve residential\n"
        "roofs are damaged, the clubhouse skylight is out, and nine carport bays are\n"
        "crushed - one onto a resident's car.\n\n"
        "Four upper-floor units in Building 05 took water when the wind removed decking.\n"
        "Two of those have had ceilings come down and all four are uninhabitable. Our\n"
        "client has moved two households into vacant units on site and two into a hotel.\n\n"
        "I should flag that we expect a scope discussion on the cosmetic damage\n"
        "exclusion. Our consultant has cut test squares and documented mat fracture\n"
        "rather than granule loss, and his report addresses the distinction directly."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Our client would also like\n"
        "your confirmation of how the per-building wind and hail deductible will be\n"
        "applied across the seven affected buildings."),
    expected="CP-7735-19042", confidence="Exact",
    why=("Policy number stated and corroborated by the insured, the additional named insured "
         "as claimant, the scheduled Location 001 address, the broker reference and the "
         "broker's sender domain."),
    exercises=(
        "**Claimant is not the named insured.** The notice names Sundale Property Group as "
        "insured and Windrow Grove Apartments, LP as claimant. Both are on the policy — the "
        "second as insured organisation — so `insured_name` must compare against joint names.",
        "**A coverage argument that is not an identity signal.** The cosmetic damage dispute "
        "belongs in warnings, not in the score.",
        "**Per-location excess.** The candidate card should show Location 001's deductible, "
        "not the policy headline.",
    ),
))

# ---------------------------------------------------------------------------
# 3 — Fairmount, dye house fire. Policy number stated. Clean match.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="fairmount-dye-house-fire",
    title="Fairmount Textile Mills — dye house fire",
    line_of_business="Commercial property",
    broker="Ridgeway Kessler Brokerage Services",
    handler="Priya Raghunathan-Lowe", handler_role="Wholesale Broker",
    handler_email="p.raghunathan-lowe@ridgewaykessler.example",
    handler_phone="+1 864 555 0913",
    broker_reference="RK/MFG/2024/2210",
    reported_on="15 August 2025",
    insured="Fairmount Textile Mills Holdings, Inc.",
    policy_number="CP-2210-55870",
    policy_type="Manufacturing All Risk Property",
    policy_period="1 September 2024 to 1 September 2025",
    policy_limit="USD 85,000,000", policy_deductible="USD 250,000",
    claimant="Piedmont Dye & Finish, LLC",
    claimant_contact="Auberon Mkhize-Calloway, Director of Manufacturing Operations",
    claimant_email="a.mkhize-calloway@fairmounttextile.example",
    date_of_loss="13 August 2025", time_of_loss="21:04 EDT",
    loss_location="1180 Woodruff Industrial Road, Greenville, SC 29607",
    cause="Fire — hydraulic fluid release onto a hot thermal oil line",
    description=(
        "Second-shift operators reported flame at a hydraulic hose on Jet Dye Machine "
        "No. 4 in Building 1B. Pressurised fluid from a hose replaced two days earlier "
        "released onto a hot thermal oil line and auto-ignited. Fire spread into the "
        "overhead cable tray and into lint accumulation in the dust collection ductwork "
        "serving the finishing line. Two sprinkler zones operated and the plant fire "
        "brigade deployed hose lines; the fire was out at 22:47."),
    affected_assets=(
        "Jet Dye Machine No. 4 (total loss) and Machines 3 and 5; approximately 18,000 "
        "sq ft of Building 1B; motor control centre MCC-1B-2 and the finishing line PLC "
        "cabinets; the baghouse and dust collection ductwork; 214,000 linear yards of "
        "technical fabric"),
    injuries="2",
    business_interruption="Yes — the dye house is the plant bottleneck with no redundancy",
    structural_damage="Yes — roof deck distortion over 3,200 sq ft, membrane burn-through",
    environmental_exposure="No",
    authorities="Greenville City Fire Department",
    incident_reference="GCFD-25-0081144",
    asset_heading="Affected plant and buildings",
    asset_rows=(
        ("Jet Dye Machine No. 4", "Total loss"),
        ("Jet Dye Machines 3 and 5", "Heat distortion, smoke and water"),
        ("Building 1B", "18,000 sq ft fire, smoke and sprinkler water"),
        ("MCC-1B-2 and PLC cabinets", "Destroyed"),
        ("Building 1A east", "Soot deposition over 20,000 sq ft"),
    ),
    estimated_loss="30,500,000", repair_estimate="17,900,000",
    repair_estimate_label="Reinstatement and plant replacement",
    report_slug="fairmount-forensic-engineers-report",
    report_title="Forensic engineer's report",
    report_firm="Halberd Forensic Engineering",
    report_reference="HFE-2025-1188",
    report_author="P. Vanterpool-Aziz, PE, CFEI",
    report_instructions=(
        ("Instructed by", "Allegheny Commercial Risk Company"),
        ("Instructed on", "15 August 2025"),
        ("Site attendance", "16 August 2025"),
        ("Scope", "Origin and cause, and the preservation of evidence"),
    ),
    report_circumstances=(
        "Building 1B houses six jet dyeing machines on a common thermal oil ring main. "
        "Machine No. 4 underwent a planned hydraulic hose replacement on 11 August. The "
        "machine ran two shifts on 12 August without incident and failed on the second "
        "shift of 13 August."),
    report_findings=(
        "The origin is at the hydraulic power unit on the drive end of Machine No. 4, "
        "approximately 600 mm above the thermal oil supply line. The replaced hose has "
        "separated at the crimped ferrule. The ferrule bore shows no crimp witness marks "
        "over roughly a third of its circumference, consistent with an undersized or "
        "incorrectly seated crimp rather than with in-service fatigue. Thermal oil supply "
        "temperature at the time was logged at 271 degrees C, above the 230 degrees C "
        "auto-ignition temperature of the hydraulic fluid in use. The failed hose "
        "assembly, the ferrule and the crimp die records have been secured."),
    report_quantum=(
        ("Jet Dye Machine No. 4 replacement", "USD 4,100,000"),
        ("Machines 3 and 5 refurbishment", "USD 2,300,000"),
        ("MCC, PLC and cable tray", "USD 800,000"),
        ("Building 1B structure, roof and MEP", "USD 4,800,000"),
        ("Baghouse and ductwork", "USD 340,000"),
        ("Smoke remediation, Buildings 1A and 1B", "USD 1,600,000"),
    ),
    report_comment_heading="On the lint removal warranty",
    report_comment=(
        "The policy warrants documented weekly lint removal from the weave room and the "
        "dust collection ductwork. Cleaning logs were produced for Building 1A through "
        "8 August, but the Building 1B finishing-line duct logs record no cleaning after "
        "19 July, a gap of twenty-five days. Duct interior photography shows accumulation "
        "consistent with that interval. The lint did not cause the fire; it materially "
        "extended it. This is recorded as a fact for the coverage file, not as an opinion "
        "on cover."),
    report_recommendation=(
        "Preserve the hose assembly, the crimp records and the maintenance work order. "
        "Notify the hose supplier and Upstate Industrial Maintenance, LLC, who performed "
        "the 11 August replacement, before any destructive testing."),
    schedule_slug="fairmount-stock-loss-schedule",
    schedule_title="Stock loss schedule",
    schedule_headers=("Location", "Description", "Linear yards", "Basis", "Value USD"),
    schedule_rows=(
        ("1B staging", "Technical fabric, finished, water and smoke", "96000", "Selling price", "1740000"),
        ("1B staging", "Coated fabric, finished, fire damaged", "38000", "Selling price", "1180000"),
        ("1B line", "Work in process on the dye line", "44000", "Replacement", "620000"),
        ("1A east", "Loom fabric, soot affected", "36000", "Replacement", "360000"),
    ),
    schedule_total_label="Total stock exposure",
    email_subject="FNOL - Fairmount Textile Mills - dye house fire, Greenville SC - CP-2210-55870",
    email_to="firstnotice@alleghenycommercialrisk.example",
    email_cc="service@carolinabra.example",
    email_date="Fri, 15 Aug 2025 08:40:11 -0400",
    email_message_id="rk-mfg-2025-2210-a@ridgewaykessler.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a fire loss on behalf of our client "
        "Fairmount Textile Mills Holdings, Inc. The affected operating company is "
        "Piedmont Dye & Finish, LLC. Our reference is RK/MFG/2024/2210."),
    email_facts=(
        ("Policy number", "CP-2210-55870"),
        ("Insured name", "Fairmount Textile Mills Holdings, Inc."),
        ("Date of loss", "13 August 2025"),
        ("Loss location", "1180 Woodruff Industrial Road, Greenville, SC 29607"),
        ("Cause", "Fire - hydraulic fluid release onto a hot thermal oil line"),
        ("Estimated loss", "USD 30,500,000"),
    ),
    email_narrative=(
        "Fire broke out on the second shift at a hydraulic hose on one of the jet dye\n"
        "machines in Building 1B and spread into the overhead cable tray and the dust\n"
        "collection ductwork. Sprinklers operated and the plant brigade held it; it was\n"
        "out by 22:47. No injuries beyond two brigade members treated for smoke\n"
        "inhalation and released.\n\n"
        "The dye house is the plant bottleneck and there is no redundancy, so the\n"
        "business interruption element is likely to exceed the property damage. Our\n"
        "client is already sourcing outsourced dyeing capacity.\n\n"
        "Notice is given within the 48-hour requirement. Nothing has been moved and the\n"
        "fire marshal's hold is still in place."),
    email_closing=(
        "Please confirm a claim reference and an adjuster today. Our client will be\n"
        "requesting an advance payment within the next fortnight."),
    expected="CP-2210-55870", confidence="Exact",
    why=("Policy number stated, with the insured, the additional named insured as claimant, "
         "scheduled Location 001, the broker reference and the wholesale broker's sender "
         "domain all agreeing."),
    exercises=(
        "**A subsidiary as claimant.** Piedmont Dye & Finish, LLC is an additional named "
        "insured, not the first named insured — `insured_name` must match against joint names.",
        "**A warranty breach that is not a matching signal.** The lint log gap is a coverage "
        "defence recorded in the engineer's report; it must not move the rank.",
        "**Quantum split across documents.** The stock schedule totals separately from the "
        "engineer's reinstatement figures, and the email states a third, higher number.",
    ),
))

# ---------------------------------------------------------------------------
# 4 — Kestrel Ridge, copper theft. NO policy number. Contextual match.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="kestrel-ridge-copper-theft",
    title="Kestrel Ridge Retail Center — copper theft and consequent water damage",
    line_of_business="Commercial property",
    broker="Harkness-Vaillancourt Insurance Agency",
    handler="Renata Sjoberg-Ellis", handler_role="Account Manager",
    handler_email="r.sjoberg-ellis@hvagency.example",
    handler_phone="+1 208 555 0466",
    broker_reference="HV/RET/2025/9083",
    reported_on="3 March 2026",
    insured="Kestrel Ridge Holdings, LP",
    policy_number="",
    policy_type="Commercial Property - Retail",
    policy_period="Renewed January 2026 — schedule with the Boise office",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Kestrel Ridge Holdings, LP",
    claimant_contact="Adaeze Ferrante-Whitlock, Senior Property Manager",
    claimant_email="a.ferrante-whitlock@aspenwallcre.example",
    date_of_loss="2 March 2026", time_of_loss="between 23:00 and 03:00, discovered 06:35",
    loss_location="4820 - 4890 North Eagle Ridge Boulevard, Boise, ID 83713",
    cause="Theft of copper, and water damage consequent on a cut live water line",
    description=(
        "Entry was forced through the rear service door of vacant Suite 116 and copper "
        "stripped from the ceiling plenum: about 90 feet of 2-inch and 1.5-inch domestic "
        "water pipe, four rooftop unit refrigerant line sets, the whip conduit for RTU-7 "
        "and RTU-8, and about 60 feet of branch circuit wire. Cutting the live 2-inch "
        "domestic line put water into the ceiling and down the demising walls for "
        "between three and six hours before the morning porter shut the main at the "
        "street. Water reached three occupied suites and the rear common corridor."),
    affected_assets=(
        "Copper pipe, refrigerant line sets and branch wire; Suite 114 ceiling grid, "
        "flooring and tenant stock; Suite 112 drywall and insulation; Suite 110 drywall "
        "and cabinetry; corridor carpet tile; the Building 01 fire alarm panel"),
    business_interruption="Yes — Suite 114 closed, Suite 110 at reduced capacity",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Boise Police Department",
    police_reference="2026-014882",
    incident_reference="ACR-2026-0302",
    asset_heading="Affected suites",
    asset_rows=(
        ("Suite 116 — vacant", "Point of entry, copper stripped from plenum"),
        ("Suite 114 — Willow & Cedar Bath Co.", "Ceiling collapse over 600 sq ft, stock saturated"),
        ("Suite 112 — vacant", "Drywall saturated to 4 ft"),
        ("Suite 110 — Arbor & Ash Salon", "Rear shampoo area, drywall and cabinetry"),
        ("Building 01 fire alarm panel", "Water at the terminal strip, trouble condition"),
    ),
    estimated_loss="425,000", repair_estimate="319,000",
    repair_estimate_label="Reinstatement estimate",
    report_slug="kestrel-ridge-restoration-assessment",
    report_title="Restoration contractor's assessment",
    report_firm="Sagebrush Restoration",
    report_reference="SR-2026-1188",
    report_author="Corinne Adeyemi-Vasquez, IICRC WRT/ASD",
    report_instructions=(
        ("Instructed by", "Aspenwall Commercial Realty, LLC"),
        ("Instructed on", "2 March 2026, 07:40"),
        ("Site attendance", "2 to 6 March 2026, ongoing"),
        ("Scope", "Emergency mitigation, drying and scope of reinstatement"),
    ),
    report_circumstances=(
        "A neighbourhood retail centre of four buildings. The affected run is Building "
        "01, in-line retail, Suites 100 to 118. Two suites in that run were vacant at "
        "the date of loss under a written vacancy permit requiring weekly secured and "
        "heated inspections; the last logged inspection was 27 February."),
    report_findings=(
        "Water tracked from the severed 2-inch line above the Suite 116 plenum along the "
        "demising walls into Suites 114, 112 and 110 and into the rear corridor. Moisture "
        "mapping on arrival recorded 38% to 44% WME in the Suite 114 ceiling grid and "
        "wall base, 31% to 39% in Suite 112, and 22% to 28% at the Suite 110 shampoo "
        "wall. Twenty-two air movers, six dehumidifiers and containment were placed. "
        "Photographs were taken before any equipment was set, as the policy requires. "
        "Cabinetry in Suite 110 and the Suite 114 ceiling grid are not salvageable."),
    report_quantum=(
        ("Emergency mitigation, extraction and drying", "USD 55,000"),
        ("Building reinstatement, four suites and corridor", "USD 185,000"),
        ("Plumbing and electrical re-pipe and re-wire", "USD 74,000"),
        ("RTU refrigerant line sets, whips, condensate", "USD 38,000"),
        ("Fire alarm panel and devices", "USD 22,000"),
    ),
    report_comment_heading="On the split between landlord and tenant",
    report_comment=(
        "Tenant improvements in Suite 114 were installed at the tenant's expense under "
        "the lease and are excluded from the figures above, as is tenant stock. The "
        "landlord scope is the shell, the ceiling grid, the base building MEP and the "
        "common corridor."),
    report_recommendation=(
        "Continue drying to a stable standard before any reinstatement. Replace rather "
        "than clean the Suite 114 ceiling grid."),
    schedule_slug="kestrel-ridge-mitigation-schedule",
    schedule_title="Mitigation and reinstatement schedule",
    schedule_headers=("Area", "Trade", "Description", "Total USD"),
    schedule_rows=(
        ("Suite 116", "Plumbing", "Re-pipe 90 ft domestic water, 2 in and 1.5 in", "34000"),
        ("Suite 116", "Electrical", "Re-pull 60 ft branch circuit, panel make good", "18000"),
        ("Roof", "Mechanical", "RTU-7 and RTU-8 line sets, whips, condensate", "38000"),
        ("Suite 114", "Building", "Ceiling grid, flooring, drywall, paint", "96000"),
        ("Suite 112", "Building", "Drywall to 4 ft, insulation, paint", "31000"),
        ("Suite 110", "Building", "Drywall, cabinetry, flooring", "44000"),
        ("Corridor", "Building", "Carpet tile and base", "14000"),
        ("Building 01", "Fire alarm", "Panel, two devices, recommission", "22000"),
        ("All", "Mitigation", "Extraction, drying, containment, monitoring", "55000"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="New loss - Kestrel Ridge Retail Center, Boise ID - overnight copper theft and water damage",
    email_to="claims@northshorespecialty.example",
    email_cc="a.ferrante-whitlock@aspenwallcre.example",
    email_date="Tue, 03 Mar 2026 09:12:03 -0700",
    email_message_id="hv-ret-2026-9083-a@hvagency.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a theft and consequent water damage "
        "loss at our client's Kestrel Ridge Retail Center in Boise. Our reference is "
        "HV/RET/2025/9083.\n\n"
        "I do not have the policy number to hand - the account renewed in January and the "
        "binder is with our Boise office - so please search on the property address and "
        "our reference. The owner entity is the Delaware limited partnership and "
        "Aspenwall Commercial Realty are the managing agent named on the policy."),
    email_facts=(
        ("Broker reference", "HV/RET/2025/9083"),
        ("Insured name", "Kestrel Ridge Holdings, LP"),
        ("Managing agent", "Aspenwall Commercial Realty, LLC"),
        ("Date of loss", "2 March 2026"),
        ("Loss location", "4820 - 4890 North Eagle Ridge Boulevard, Boise, ID 83713"),
        ("Cause", "Theft of copper and consequent water damage"),
        ("Estimated loss", "USD 425,000"),
    ),
    email_narrative=(
        "Someone forced the rear service door of a vacant in-line suite overnight on\n"
        "Sunday and stripped copper out of the ceiling plenum. Cutting a live 2-inch\n"
        "domestic line is what did the damage - it ran for somewhere between three and\n"
        "six hours before the morning porter found it.\n\n"
        "Three occupied suites and the rear corridor took water. One tenant is closed and\n"
        "another is trading at reduced capacity. Boise PD attended and there is camera\n"
        "footage of two individuals and a white pickup, which we have preserved.\n\n"
        "Two suites in that run are vacant under the permit written into the policy at\n"
        "renewal, and the weekly inspection log is available."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Our client would like your\n"
        "view on whether the theft deductible or the water damage deductible applies,\n"
        "and whether the copper theft sublimit reaches the resulting water damage."),
    expected="CP-9083-64115", confidence="Strong",
    why=("No policy number is stated. The broker reference `HV/RET/2025/9083`, the property "
         "address matching scheduled Location 001, the named insured, the managing agent who "
         "is the scheduled additional insured, and the sender domain `hvagency.example` "
         "resolve to one policy with no competing candidate in the book."),
    exercises=(
        "**Identification without a policy number.** The heaviest signal is absent, so the "
        "match has to be carried by broker reference at 2.5 and insured name at 2.0, "
        "corroborated across the risk, broker and cover axes.",
        "**The broker reference doing the work.** This is the case the `broker_reference` "
        "signal exists for — the broker's own scheme reference is the only hard identifier present.",
        "**Reporter is not the insured.** The reporter is the broker; the managing agent is the "
        "site contact; the insured is a Delaware LP that never appears as a sender.",
    ),
))

# ---------------------------------------------------------------------------
# 5 — Rivergate Commons, dropped load. Builders risk number stated.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="rivergate-dropped-load",
    title="Rivergate Commons Phase II — dropped load during a crane pick",
    line_of_business="Construction — builder's risk",
    broker="Ashcombe Vail Commercial Insurance Services",
    handler="Gerald Nwosu-Fitzgerald", handler_role="Construction Claims Handler",
    handler_email="g.nwosu-fitzgerald@ashcombevail.example",
    handler_phone="+1 704 555 0731",
    broker_reference="AV/BR/2025/3358",
    reported_on="26 February 2026",
    insured="Rivergate Development Partners, LLC",
    policy_number="BR-3358-20471",
    policy_type="Builder's Risk - Completed Value",
    policy_period="15 April 2025 to 15 October 2026",
    policy_limit="USD 65,550,000", policy_deductible="USD 50,000",
    claimant="Vanterra Construction Group, LLC",
    claimant_contact="Callum Baptiste-Ngo, Project Superintendent",
    claimant_email="c.baptiste-ngo@vanterracg.example",
    date_of_loss="26 February 2026", time_of_loss="14:52 EST",
    loss_location="2650 Rivergate Parkway, Charlotte, NC 28273",
    cause="Dropped load — wind gust during a crane pick",
    description=(
        "A bundled load of engineered floor joists and LVL beams being hoisted to Level "
        "4 met a recorded 41 mph gust at about 42 feet. The load swung, the signal person "
        "called for a set-down, and during the set-down the load struck the north-east "
        "corner of the Level 3 framed wall. The choker then slipped and roughly "
        "two-thirds of the bundle fell to the podium deck below. The exclusion zone held "
        "and nobody was struck."),
    affected_assets=(
        "26 linear feet of Level 3 framed and sheathed exterior wall; two impact craters "
        "in the precast podium topping, one with exposed strand; 38 engineered joists and "
        "6 LVL beams; 60 feet of edge protection; 46 sheets of subfloor sheathing; a "
        "construction hoist mast tie"),
    business_interruption="No — schedule impact assessed at three to five weeks",
    structural_damage="Yes — podium deck strand exposed, wall section racked out of plumb",
    environmental_exposure="No",
    authorities="None — not OSHA reportable",
    incident_reference="RGC2-IR-2026-014",
    asset_heading="Project and works affected",
    asset_rows=(
        ("Project", "Rivergate Commons Phase II, 214 units over a two-level podium"),
        ("Level 3 north-east", "26 lin ft wall destroyed, 340 sq ft sheathing and WRB torn"),
        ("Podium deck", "Two impact craters, shored pending engineer's detail"),
        ("Materials", "38 joists, 6 LVLs, 46 sheets sheathing"),
        ("Construction hoist", "Mast tie bent, hoist out of service"),
    ),
    estimated_loss="407,200", repair_estimate="407,200",
    repair_estimate_label="Reinstatement estimate",
    report_slug="rivergate-structural-engineers-report",
    report_title="Structural engineer's report",
    report_firm="Braeburn Structural Group",
    report_reference="BSG-2026-0227",
    report_author="Ingrid Achterberg-Nakamura, PE, SE",
    report_instructions=(
        ("Instructed by", "Vanterra Construction Group, LLC"),
        ("Instructed on", "26 February 2026"),
        ("Site attendance", "27 February 2026"),
        ("Scope", "Podium deck integrity and the Level 3 wall section"),
    ),
    report_circumstances=(
        "The building is a five-storey light-gauge and wood-framed structure over a "
        "two-level precast concrete parking podium. At the date of loss the podium and "
        "Levels 2 and 3 were framed and sheathed, Level 4 was in progress, and the "
        "building was not dried in."),
    report_findings=(
        "Two impact locations on the podium topping. The larger, approximately 460 mm by "
        "610 mm, has fractured the topping through to the plank and exposed two "
        "prestressing strands over a 300 mm length. Neither strand is severed. "
        "Hammer sounding either side of the crater indicates delamination extending "
        "roughly 400 mm beyond the visible edge. The second location is surface spalling "
        "only. The Level 3 wall section is racked and out of plumb by 38 mm over the "
        "storey height and is not repairable in place; it requires demolition and rebuild. "
        "The bay has been shored and no load is permitted on it."),
    report_quantum=(
        ("Podium deck repair to engineer's detail", "USD 165,000"),
        ("Level 3 wall demolition and rebuild", "USD 74,000"),
        ("Materials — joists, LVLs, sheathing", "USD 96,500"),
        ("Temporary shoring and edge protection", "USD 31,000"),
        ("Construction hoist inspection and repair", "USD 18,500"),
    ),
    report_comment_heading="On the strands",
    report_comment=(
        "Neither exposed strand is severed and neither shows section loss. Core sampling "
        "either side of the crater is required to confirm that before the repair detail "
        "is finalised; if section loss is found the repair becomes a plank replacement "
        "and the figure above will not hold."),
    report_recommendation=(
        "Maintain the shoring until the repair is complete and cored. Do not reload the "
        "bay. Re-survey the Level 3 frame either side of the rebuilt section."),
    schedule_slug="rivergate-reinstatement-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Element", "Description", "Quantity", "Unit", "Total USD"),
    schedule_rows=(
        ("Podium deck", "Crater repair, coring, topping reinstatement", "2", "locations", "165000"),
        ("Level 3 wall", "Demolition and rebuild including sheathing and WRB", "26", "lin ft", "74000"),
        ("Materials", "Engineered floor joists", "38", "each", "58500"),
        ("Materials", "LVL beams", "6", "each", "22000"),
        ("Materials", "Subfloor sheathing", "46", "sheets", "16000"),
        ("Temporary works", "Shoring, guardrail, edge protection", "1", "lot", "31000"),
        ("Plant", "Construction hoist mast tie and inspection", "1", "item", "18500"),
        ("Preliminaries", "Crane standby, re-rig, cleanup and disposal", "1", "lot", "22200"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="FNOL - Rivergate Commons Phase II - dropped load, Charlotte NC - BR-3358-20471",
    email_to="constructionclaims@pinnacleinland.example",
    email_cc="c.baptiste-ngo@vanterracg.example",
    email_date="Thu, 26 Feb 2026 17:40:22 -0500",
    email_message_id="av-br-2026-3358-a@ashcombevail.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify a dropped-load incident on the "
        "Rivergate Commons Phase II project. Our reference is AV/BR/2025/3358."),
    email_facts=(
        ("Policy number", "BR-3358-20471"),
        ("Project name", "Rivergate Commons Phase II"),
        ("Contract number", "AIA-A102-2025-RGC2"),
        ("Insured name", "Rivergate Development Partners, LLC"),
        ("Date of loss", "26 February 2026"),
        ("Loss location", "2650 Rivergate Parkway, Charlotte, NC 28273"),
        ("Estimated loss", "USD 407,200"),
    ),
    email_narrative=(
        "A bundle of engineered joists being flown to Level 4 caught a 41 mph gust, swung\n"
        "into the Level 3 frame and then dropped to the podium when the choker slipped.\n"
        "Nobody was hurt - the exclusion zone was maintained and the site cleared per the\n"
        "lift plan. It is not OSHA reportable.\n\n"
        "The podium deck has two impact craters, one with exposed prestressing strand,\n"
        "and the engineer has shored the bay and will not release it pending coring. A\n"
        "26-foot section of Level 3 wall is racked and comes down.\n\n"
        "This is a first-party loss to the works. There is no third-party injury and no\n"
        "damage to anyone else's property, so our client's liability policy is not\n"
        "engaged and we are not notifying it."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. The failed choker, the slings\n"
        "and the load chart have been bagged and retained, and nothing has been cleared."),
    expected="BR-3358-20471", confidence="Exact",
    why=("Policy number stated, and corroborated by the project name and contract number, "
         "the site address, both named insureds appearing as insured and claimant, the broker "
         "reference and the sender domain."),
    exercises=(
        "**Construction identity signals.** `project_name` at 1.6 and `contract_number` at "
        "2.2 both fire and both agree, which on a construction risk is stronger evidence "
        "than the insured name.",
        "**The insured is not the contractor.** The owner is the first named insured and "
        "Vanterra is the contractor and the claimant; both are on the policy.",
        "**Ruling the other policy out in the notice.** The email states there is no "
        "third-party element, which is what keeps this off the contractor's CGL — the "
        "discriminator pack 17 makes the matcher work out for itself.",
    ),
))

# ---------------------------------------------------------------------------
# 6 — Northfield, water intrusion. NO policy number. Contextual match.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="northfield-water-intrusion",
    title="Northfield Senior Living — water intrusion into an undried structure",
    line_of_business="Construction — builder's risk",
    broker="Kettleridge Risk Partners, LLC",
    handler="Marcus Adeyemi-Croft", handler_role="Producer",
    handler_email="m.adeyemi-croft@kettleridgerisk.example",
    handler_phone="+1 918 555 0347",
    broker_reference="KRP/CAR/2025/6612",
    reported_on="22 October 2025",
    insured="Sundale Property Group, LLC",
    policy_number="",
    policy_type="Builder's Risk - Completed Value",
    policy_period="1 February 2025 to 1 February 2027",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Halcyon Builders, LLC",
    claimant_contact="Owen Kastellec-Brandt, Project Executive",
    claimant_email="o.kastellec-brandt@halcyonbuilders.example",
    date_of_loss="18 October 2025", time_of_loss="overnight, discovered 20 October 06:40",
    loss_location="1725 Northfield Commons Drive, Madison, WI 53704",
    cause="Water intrusion — wind-driven rain into an incomplete structure",
    description=(
        "A frontal system crossed Dane County over the weekend. NWS Madison recorded 2.81 "
        "inches of rain over about thirty hours with gusts to 44 mph. Water entered "
        "Building A at two unflashed mechanical curbs and at the elevator overrun, and "
        "tracked down the shaft to the pit. Wind tore roughly 60% of the temporary poly "
        "on the north elevation at Levels 3 and 4, admitting wind-driven rain across two "
        "floors. Nobody was on site over the weekend."),
    affected_assets=(
        "11,400 sq ft of hung gypsum board and batt insulation on Levels 3 and 4; 4,200 "
        "sq ft of Level 2 ceiling assemblies; 190 sheets of stored board and 84 rolls of "
        "insulation; the elevator pit, rails and buffers; 14 panelboards and 320 device "
        "boxes; 700 linear feet of installed ductwork"),
    business_interruption="No — schedule float absorbs two to three weeks",
    structural_damage="No — floor sheathing edge swell under assessment",
    environmental_exposure="No",
    authorities="None",
    incident_reference="HAL-NSL-2025-0088",
    asset_heading="Project and works affected",
    asset_rows=(
        ("Project", "Northfield Senior Living, 132 units in three buildings"),
        ("Building A Levels 3 and 4", "11,400 sq ft board and insulation wet"),
        ("Building A Level 2", "4,200 sq ft ceiling assemblies"),
        ("Elevator shaft", "Pit flooded to 14 in, rails and buffers wetted"),
        ("Rough electrical", "14 panelboards, 320 device boxes quarantined"),
    ),
    estimated_loss="915,000", repair_estimate="805,000",
    repair_estimate_label="Reinstatement estimate",
    report_slug="northfield-drying-and-moisture-report",
    report_title="Drying and moisture report",
    report_firm="Servpro of Dane County",
    report_reference="SDC-2025-4471",
    report_author="Halvard Nkemelu-Sorensen, IICRC CDS",
    report_instructions=(
        ("Instructed by", "Halcyon Builders, LLC"),
        ("Instructed on", "20 October 2025, 09:00"),
        ("Site attendance", "20 to 28 October 2025"),
        ("Scope", "Emergency drying and the extent of affected materials"),
    ),
    report_circumstances=(
        "Building A is a four-storey independent living building over a podium, at "
        "rough-in. The roof membrane was installed but not fully flashed at two "
        "mechanical curbs or at the elevator overrun. Windows were in on Levels 1 and 2; "
        "Levels 3 and 4 were sheeted with temporary poly on the north and east elevations."),
    report_findings=(
        "Moisture readings on 21 October ranged from 28% to 42% WME across the north and "
        "east zones of Levels 3 and 4, against a dry standard of 16% for the gypsum in "
        "use. Sixty-eight air movers, fourteen LGR dehumidifiers and containment were "
        "placed on Levels 3 and 4 and controlled demolition of wet insulation began the "
        "same day. Board hung below 1.2 m on the affected elevations is not recoverable "
        "and is being cut back. The elevator pit was pumped and the rails and buffers "
        "are corrosion-treated pending the lift contractor's inspection; the car and "
        "controller had not been delivered."),
    report_quantum=(
        ("Board and insulation, removal and replacement", "USD 340,000"),
        ("Stored materials destroyed", "USD 62,000"),
        ("Electrical panelboards, boxes, re-pull", "USD 185,000"),
        ("Duct cleaning, drying and partial replacement", "USD 95,000"),
        ("Elevator pit remediation", "USD 48,000"),
        ("Emergency mitigation and drying", "USD 110,000"),
    ),
    report_comment_heading="On the mould window",
    report_comment=(
        "The water event was discovered on 20 October and reported to insurers on 22 "
        "October. Daily moisture logs have been kept from first attendance. Provided "
        "drying continues to a documented standard, secondary microbial growth should "
        "not arise; the logs are the evidence that it did not."),
    report_recommendation=(
        "Continue drying to 16% WME before any board is re-hung. Do not close any "
        "assembly on the north elevation without a logged final reading."),
    schedule_slug="northfield-reinstatement-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Location", "Trade", "Description", "Total USD"),
    schedule_rows=(
        ("Level 3 north/east", "Drywall", "Cut back and re-hang board, insulation", "168000"),
        ("Level 4 north/east", "Drywall", "Cut back and re-hang board, insulation", "172000"),
        ("Level 2", "Drywall", "Ceiling assemblies, 4,200 sq ft", "58000"),
        ("Level 3 store", "Materials", "190 sheets board, 84 rolls insulation", "62000"),
        ("Levels 3 and 4", "Electrical", "14 panelboards, 320 boxes, re-pull", "185000"),
        ("Levels 3 and 4", "Mechanical", "Duct clean, dry and partial replacement", "95000"),
        ("Elevator", "Lift", "Pit remediation, rails, buffers, ladder", "48000"),
        ("Building A", "Mitigation", "Extraction, drying, containment, logging", "110000"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="New loss - Northfield Senior Living, Madison WI - water intrusion Building A",
    email_to="buildersrisk.claims@cascadiamutual.example",
    email_cc="o.kastellec-brandt@halcyonbuilders.example",
    email_date="Wed, 22 Oct 2025 16:38:51 -0500",
    email_message_id="krp-car-2025-6612-a@kettleridgerisk.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify a water intrusion loss on the "
        "Northfield Senior Living development in Madison. Our reference is "
        "KRP/CAR/2025/6612.\n\n"
        "I am travelling and do not have the policy schedule with me - the binder is with "
        "our Tulsa office - so please locate on our reference and the project. The first "
        "named insured is Sundale Property Group, LLC of Tulsa and the general contractor "
        "is Halcyon Builders, LLC of Madison."),
    email_facts=(
        ("Broker reference", "KRP/CAR/2025/6612"),
        ("Project name", "Northfield Senior Living"),
        ("Contract number", "AIA-A133-2024-NSL"),
        ("Insured name", "Sundale Property Group, LLC"),
        ("General contractor", "Halcyon Builders, LLC"),
        ("Date of loss", "18 October 2025"),
        ("Loss location", "1725 Northfield Commons Drive, Madison, WI 53704"),
        ("Estimated loss", "USD 915,000"),
    ),
    email_narrative=(
        "A frontal system put 2.81 inches of rain through Dane County over the weekend\n"
        "with gusts to 44 mph. Two unflashed mechanical curbs and the elevator overrun\n"
        "took water directly, and the wind tore most of the temporary poly off the north\n"
        "elevation at Levels 3 and 4.\n\n"
        "Nobody was on site over the weekend so it ran until Monday morning. Servpro were\n"
        "in by 09:00 Monday and there are daily moisture logs from first attendance.\n\n"
        "There is schedule float, but the drying and controlled demolition will cost two\n"
        "to three weeks on the Building A critical path. If that becomes a delay we will\n"
        "revert on the soft costs section - the owner entities are the named insureds for it."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Owen at Halcyon can meet\n"
        "anyone on site with 24 hours' notice."),
    expected="BR-6612-77309", confidence="Strong",
    why=("No policy number. The broker reference `KRP/CAR/2025/6612`, the project name and "
         "contract number, the site address, both owner entities and the general contractor "
         "all match one construction policy. Sundale's other policy is a Tulsa habitational "
         "property risk that cannot answer a Wisconsin construction loss."),
    exercises=(
        "**One insured, two policies, different lines.** Sundale Property Group is the first "
        "named insured on both `CP-7735-19042` and `BR-6612-77309`. `line_of_business` and "
        "`project_name` are what separate them.",
        "**Project identity over insured identity.** The project name and contract number "
        "resolve the risk more reliably than the insured, which is the case the construction "
        "signals exist for.",
        "**Reporter, insured and contractor are three different parties**, on three different "
        "domains, none of which is the carrier.",
    ),
))

# ---------------------------------------------------------------------------
# 7 — Cypress Landing, copper theft. AMBIGUOUS: a real number, wrong policy.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="cypress-landing-copper-theft",
    title="Cypress Landing Data Hall B — copper theft from the works",
    line_of_business="Construction — builder's risk",
    broker="Sonoran Ridge Risk Advisors, LLC",
    handler="Desmond Achterberg", handler_role="Producer",
    handler_email="d.achterberg@sonoranridgerisk.example",
    handler_phone="+1 602 555 0510",
    broker_reference="SRR/BR/2025/1147",
    reported_on="13 April 2026",
    insured="Meridian Grid Holdings, LLC",
    policy_number="IM-2298-66401",
    policy_type="Stated as contractors equipment — see the risk manager's report",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Ironbark Constructors, Inc.",
    claimant_contact="Alina Petrosyan-Ward, Corporate Risk Manager",
    claimant_email="a.petrosyan-ward@ironbarkconstructors.example",
    date_of_loss="11 April 2026", time_of_loss="between 01:20 and 03:05, discovered 06:15",
    loss_location="14900 South Cypress Landing Way, Mesa, AZ 85212",
    cause="Theft — copper stripped from the works",
    description=(
        "Perimeter fence fabric was cut on the north-east boundary after two site cameras "
        "covering that approach were disabled. Roughly 4,800 linear feet of 500 and 750 "
        "kcmil copper feeder cable was cut from reels and from partially pulled runs in "
        "duct bank sections DB-4 and DB-5, 1,100 feet of 4/0 bare copper was stripped "
        "from the substation pad ground ring, and sixteen copper busway sections were "
        "removed. Two owner-supplied UPS module cabinets were damaged in an attempt to "
        "strip their bus stabs."),
    affected_assets=(
        "4,800 lin ft of copper feeder cable; 1,100 lin ft of ground ring conductor; 16 "
        "copper busway sections; two 500 kVA UPS module cabinets; duct bank DB-4 and "
        "DB-5 conduit and six handhole lids; one engine drive welder and hand tools from "
        "a job trailer; 220 lin ft of perimeter fence; two site cameras"),
    business_interruption="No — delay in start-up not purchased on this programme",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Mesa Police Department",
    police_reference="MPD-2026-0338114",
    incident_reference="IBC-SEC-2026-0071",
    asset_heading="Project and property affected",
    asset_rows=(
        ("Project", "Cypress Landing Data Hall B, campus building 2 of 4"),
        ("Permanent works", "Feeder cable, ground ring, busway, UPS cabinets, duct bank"),
        ("Contractors equipment", "One engine drive welder, hand tools from trailer T-4"),
        ("Temporary works", "220 lin ft perimeter fence, two site cameras"),
        ("Not affected", "Data Hall A, which is a separate operating asset"),
    ),
    estimated_loss="1,318,100", repair_estimate="1,264,000",
    repair_estimate_label="Permanent works replacement",
    report_slug="cypress-landing-risk-managers-report",
    report_title="Risk manager's allocation report",
    report_firm="Ironbark Constructors, Inc.",
    report_reference="IBC-SEC-2026-0071",
    report_author="Alina Petrosyan-Ward, Corporate Risk Manager",
    report_instructions=(
        ("Prepared for", "Sonoran Ridge Risk Advisors and Desert Basin Specialty"),
        ("Prepared on", "13 April 2026"),
        ("Scope", "What was taken, and which policy answers for it"),
        ("Reviewed by", "Hector Zaldivar-Boone, Executive Vice President"),
    ),
    report_circumstances=(
        "The project team submitted this loss under the company's contractors equipment "
        "floater because site inventory is tracked in the equipment system. Risk "
        "Management does not agree with that allocation and sets out the position here "
        "so that the broker can direct it correctly rather than have it corrected later."),
    report_findings=(
        "Of USD 1,318,100 taken or damaged, USD 1,264,000 is copper feeder cable, ground "
        "grid conductor, busway and owner-supplied UPS cabinets. Every one of those items "
        "is material intended to become a permanent part of the insured project and was "
        "staged at the project site awaiting installation. The contractors equipment "
        "floater excludes, in terms, property that is or is intended to become a permanent "
        "part of a building, structure or the insured's construction project, and directs "
        "that exposure to a builder's risk or installation floater. Only USD 42,800 of "
        "contractors equipment and USD 11,300 of temporary works properly sit on the "
        "equipment floater. The primary notice belongs on the project's builder's risk "
        "policy, on which this company is a named insured alongside the owner, and on "
        "which the owner-supplied UPS modules are specifically scheduled."),
    report_quantum=(
        ("Copper feeder cable, 500 and 750 kcmil", "USD 612,000"),
        ("Ground ring conductor and re-testing", "USD 148,000"),
        ("Copper busway, 16 sections", "USD 224,000"),
        ("UPS module cabinets, factory re-certification", "USD 186,000"),
        ("Duct bank, handholes and re-pull", "USD 94,000"),
        ("Contractors equipment and temporary works", "USD 54,100"),
    ),
    report_comment_heading="Two coverage issues, raised rather than left to be found",
    report_comment=(
        "First, the site protection warranty requires a licensed guard patrol between "
        "18:00 and 06:00. A guard was on duty and logged rounds at 22:40, 00:55 and "
        "04:10, but the instruction is hourly and the gap covers the whole incident "
        "window. Second, the copper-specific theft sublimit is USD 500,000 against "
        "roughly USD 984,000 of copper taken. Both should be addressed early rather than "
        "at settlement."),
    report_recommendation=(
        "Submit to the builder's risk carrier as the primary notice. Notify the equipment "
        "floater for the welder, the hand tools and the temporary works only."),
    schedule_slug="cypress-landing-loss-allocation-schedule",
    schedule_title="Loss allocation schedule",
    schedule_headers=("Category", "Item", "Belongs on", "Total USD"),
    schedule_rows=(
        ("Permanent works", "Copper feeder cable, 4,800 lin ft", "Builder's risk", "612000"),
        ("Permanent works", "Ground ring conductor, 1,100 lin ft", "Builder's risk", "148000"),
        ("Permanent works", "Copper busway, 16 sections", "Builder's risk", "224000"),
        ("Permanent works", "UPS module cabinets, two off", "Builder's risk", "186000"),
        ("Permanent works", "Duct bank, handholes, re-pull", "Builder's risk", "94000"),
        ("Contractors equipment", "Engine drive welder", "Equipment floater", "9400"),
        ("Contractors equipment", "Hand tools, crimpers, grips", "Equipment floater", "31600"),
        ("Contractors equipment", "Trailer T-4 door and hasp", "Equipment floater", "1800"),
        ("Temporary works", "Perimeter fence, 220 lin ft", "Equipment floater", "6400"),
        ("Temporary works", "Site cameras, two off", "Equipment floater", "4900"),
    ),
    schedule_total_label="Total loss",
    email_subject="Loss notification - Cypress Landing Data Hall B, Mesa AZ - copper theft from site",
    email_to="report@desertbasinspecialty.example",
    email_cc="a.petrosyan-ward@ironbarkconstructors.example",
    email_date="Mon, 13 Apr 2026 09:04:17 -0700",
    email_message_id="srr-br-2026-1147-a@sonoranridgerisk.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a theft from the Cypress Landing "
        "Data Hall B project. Our reference is SRR/BR/2025/1147.\n\n"
        "A note on the policy number below. The client's project team submitted this to us "
        "quoting IM-2298-66401, which is their contractors equipment floater, because "
        "that is where site inventory is tracked. Their own risk manager does not agree "
        "and has set out why in the attached report. Please read her allocation before "
        "the file is set up."),
    email_facts=(
        ("Policy number quoted by the client", "IM-2298-66401"),
        ("Broker reference", "SRR/BR/2025/1147"),
        ("Project name", "Cypress Landing Data Hall B"),
        ("Contract number", "CONSENSUSDOCS-410-2025-CLB"),
        ("Insured name", "Meridian Grid Holdings, LLC"),
        ("Contractor", "Ironbark Constructors, Inc."),
        ("Date of loss", "11 April 2026"),
        ("Loss location", "14900 South Cypress Landing Way, Mesa, AZ 85212"),
        ("Estimated loss", "USD 1,318,100"),
    ),
    email_narrative=(
        "Fence fabric was cut on the north-east boundary overnight on Saturday, after two\n"
        "cameras covering that approach were disabled. They took copper feeder cable off\n"
        "the reels and out of two duct bank runs, stripped the substation ground ring,\n"
        "and cut out sixteen busway sections. Two owner-supplied UPS cabinets were\n"
        "damaged in an attempt to strip them.\n\n"
        "About 96% of the value is material staged for installation into the building.\n"
        "That is permanent works, not contractors equipment, and the equipment floater\n"
        "excludes it in terms. The welder, the hand tools and the fence are the only part\n"
        "that belongs on the floater.\n\n"
        "The UPS cabinets have not been moved, opened or cleaned and are tagged do not\n"
        "move pending your inspection."),
    email_closing=(
        "Please open this against the project's builder's risk policy and confirm a claim\n"
        "reference. We will notify the equipment floater separately for the USD 54,100."),
    expected="BR-1147-30926", confidence="Possible",
    why=("The notice quotes `IM-2298-66401`, which is a **real policy for the same corporate "
         "group but the wrong one for this loss**. The `policy_number` signal will find that "
         "policy and score it — and every other signal contradicts it: the project name, the "
         "contract number, the site address, the broker reference and the owner as insured all "
         "belong to `BR-1147-30926`. A correct answer overrides the stated number on the weight "
         "of the other eleven signals."),
    exercises=(
        "**The heaviest signal pointing at the wrong answer.** `policy_number` carries 5.0 and "
        "resolves cleanly to the equipment floater. Everything else — project, contract, site, "
        "broker reference, insured — points at the builder's risk. This is the case that tests "
        "whether the ladder reads *which axes* agreed rather than only the total.",
        "**A defensible split.** USD 54,100 genuinely does belong on `IM-2298-66401`, so a "
        "two-policy allocation is a better answer than either policy alone.",
        "**A warning that is not a rank.** The guard patrol gap is a warranty question and must "
        "not lower the builder's risk candidate below the equipment floater.",
    ),
))

# ---------------------------------------------------------------------------
# 8 — Vanterra, falling brick. Third-party liability. Policy number stated.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="sable-creek-falling-brick",
    title="Sable Creek Medical Office Building — falling brick, third-party injury",
    line_of_business="Liability — commercial general liability",
    broker="Ashcombe Vail Commercial Insurance Services",
    handler="Gerald Nwosu-Fitzgerald", handler_role="Construction Claims Handler",
    handler_email="g.nwosu-fitzgerald@ashcombevail.example",
    handler_phone="+1 704 555 0731",
    broker_reference="AV/GL/2025/5529",
    reported_on="18 March 2026",
    insured="Vanterra Construction Group, LLC",
    policy_number="GL-5529-41836",
    policy_type="Commercial General Liability - Occurrence",
    policy_period="1 April 2025 to 1 April 2026",
    policy_limit="USD 1,000,000 each occurrence", policy_deductible="USD 25,000 per claim",
    claimant="Rosalind Achterberg-Nwankwo",
    claimant_contact="Ferriday & Oduya, LLP — M. Oduya-Prentice",
    claimant_email="m.oduya-prentice@ferridayoduya.example",
    date_of_loss="17 March 2026", time_of_loss="10:20 EDT",
    loss_location="4405 Sable Creek Drive, Fort Mill, SC 29715",
    cause="Falling brick — banding failure during a telehandler unload",
    description=(
        "The insured's masonry subcontractor was unloading banded cube packs of brick "
        "from a flatbed using a telehandler staged on the shared access drive. A pack was "
        "raised over the drive to clear a stockpile and the banding failed, dropping "
        "roughly 200 bricks. A member of the public walking to the adjacent occupied "
        "medical practice was struck on the right shoulder and forearm. Two parked "
        "vehicles and the adjacent property's landscape wall and bollard lighting were "
        "also struck."),
    affected_assets=(
        "Third-party bodily injury to one member of the public; a 2023 Subaru Outback "
        "and a 2019 Toyota RAV4; approximately 40 linear feet of the adjacent property's "
        "landscape wall and its bollard lighting"),
    injuries="1",
    business_interruption="Yes — the adjacent practice lost a half day of appointments",
    structural_damage="No",
    environmental_exposure="No",
    authorities="York County EMS; SC LLR OSHA attended 19 March, no citation",
    incident_reference="VCG-INC-2026-0317",
    potential_litigation="Yes — claimant has instructed Ferriday & Oduya, LLP",
    asset_heading="Third-party property and persons",
    asset_rows=(
        ("Claimant", "Rosalind Achterberg-Nwankwo, 61, patient of the adjacent practice"),
        ("Injuries", "Comminuted right distal radius fracture, clavicle fracture, lacerations"),
        ("Vehicle 1", "2023 Subaru Outback — roof, windshield, hood, towed"),
        ("Vehicle 2", "2019 Toyota RAV4 — hood and quarter panel"),
        ("Adjacent property", "Landscape wall 40 lin ft, bollard lighting"),
    ),
    estimated_loss="663,000", repair_estimate="74,000",
    repair_estimate_label="Third-party property damage",
    report_slug="sable-creek-liability-investigation-report",
    report_title="Liability investigation report",
    report_firm="Cardinal Adjusting Services",
    report_reference="CAS-2026-0331",
    report_author="Beatriz Oyelaran-Whitfield, AIC",
    report_instructions=(
        ("Instructed by", "Piedmont Casualty & Surety Company"),
        ("Instructed on", "19 March 2026"),
        ("Site attendance", "19 and 24 March 2026"),
        ("Scope", "Liability, quantum and recovery prospects"),
    ),
    report_circumstances=(
        "The insured is the general contractor on a medical office building under "
        "construction at 4405 Sable Creek Drive, which is designated project P-3 on its "
        "liability policy. The adjacent building at 4415 is occupied and operating. The "
        "two properties share an access drive and a parking field."),
    report_findings=(
        "The banding on the failed cube pack was 19 mm polyester strapping applied by the "
        "supplier, Carolina Brick & Supply. Two retained straps from the same delivery "
        "show seal slippage at the crimp rather than tensile failure of the strap. The "
        "telehandler was within its load chart and its operator held a current "
        "certification; the machine was inspected and released on 19 March. The lift "
        "passed over a shared access drive that had not been barricaded to pedestrians, "
        "which is the exposure the insured could have controlled. Liability against the "
        "insured is probable; contribution from the brick supplier is realistic."),
    report_quantum=(
        ("Bodily injury indemnity, range", "USD 400,000 to 750,000"),
        ("Medical specials to date", "USD 118,000"),
        ("Third-party vehicles, two", "USD 46,000"),
        ("Landscape wall and bollard lighting", "USD 28,000"),
        ("Adjacent tenant business interruption", "USD 14,000"),
    ),
    report_comment_heading="On the subcontractor and the deductible",
    report_comment=(
        "A written subcontract dated 4 November 2025 was executed before the loss. It "
        "carries an indemnity in the insured's favour and requires additional insured "
        "status on a primary and non-contributory basis for ongoing and completed "
        "operations, and a certificate evidencing USD 1,000,000 each occurrence was on "
        "file before work began. The subcontractor insurance warranty is therefore "
        "satisfied and the per-claim deductible is USD 25,000, not the USD 250,000 the "
        "warranty imposes when it is not."),
    report_recommendation=(
        "Maintain the tender to the subcontractor's carrier. Preserve the retained "
        "banding straps and the remaining packs from the same delivery for the supplier."),
    schedule_slug="sable-creek-quantum-schedule",
    schedule_title="Quantum schedule",
    schedule_headers=("Head of claim", "Party", "Basis", "Total USD"),
    schedule_rows=(
        ("Bodily injury indemnity", "R. Achterberg-Nwankwo", "Reserve, mid-range", "575000"),
        ("Vehicle — Subaru Outback", "Practice employee", "Likely total loss", "31000"),
        ("Vehicle — Toyota RAV4", "Practice employee", "Repair estimate", "15000"),
        ("Landscape wall and lighting", "Adjacent owner", "Contractor estimate", "28000"),
        ("Business interruption", "Palmetto Foot & Ankle", "Half day cancelled list", "14000"),
    ),
    schedule_total_label="Total probable",
    email_subject="Liability notice - Vanterra Construction Group - falling brick, Fort Mill SC - GL-5529-41836",
    email_to="gl.claims@piedmontcasualty.example",
    email_cc="risk@vanterracg.example",
    email_date="Wed, 18 Mar 2026 11:02:36 -0400",
    email_message_id="av-gl-2026-5529-a@ashcombevail.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a third-party injury and property "
        "damage occurrence on behalf of our client Vanterra Construction Group, LLC. Our "
        "reference is AV/GL/2025/5529. Telephone notice was given yesterday as the policy "
        "requires for an occurrence involving hospitalisation."),
    email_facts=(
        ("Policy number", "GL-5529-41836"),
        ("Insured name", "Vanterra Construction Group, LLC"),
        ("Designated project", "P-3 — Sable Creek Medical Office Building"),
        ("Date of loss", "17 March 2026"),
        ("Loss location", "4405 Sable Creek Drive, Fort Mill, SC 29715"),
        ("Cause", "Falling brick - banding failure during a telehandler unload"),
        ("Estimated loss", "USD 663,000"),
    ),
    email_narrative=(
        "Banding on a raised brick cube pack failed during unloading and around 200 bricks\n"
        "fell across a shared access drive. A member of the public walking to the adjacent\n"
        "medical practice was struck and has a fractured wrist requiring surgical fixation\n"
        "and a fractured clavicle. She was admitted for three days and has instructed\n"
        "solicitors; a representation and preservation letter arrived on 26 March.\n\n"
        "Two parked vehicles and the adjacent property's landscape wall were also hit.\n\n"
        "This is a third-party occurrence only. No claim is made under the project's\n"
        "builder's risk - our client's own works were not damaged beyond the brick itself,\n"
        "which is not being claimed."),
    email_closing=(
        "Please confirm a claim reference and assign counsel. We have tendered to the\n"
        "masonry subcontractor and its carrier, and the banding straps are retained."),
    expected="GL-5529-41836", confidence="Exact",
    why=("Policy number stated, with the insured, the designated project P-3 on the policy's "
         "own schedule, the broker reference and the sender domain all agreeing. The loss type "
         "is third-party bodily injury and property damage, which is the liability policy and "
         "not the insured's builder's risk."),
    exercises=(
        "**Line of business as a discriminator.** Vanterra is a named insured on a builder's "
        "risk too. This loss is `liability`; that one is `construction`.",
        "**Claimant is a third party, not the insured.** `claimant_name` is a member of the "
        "public and must not be read as the insured.",
        "**A scheduled project on a liability policy.** P-3 appears on the GL's location "
        "schedule, so `risk_location` corroborates on an axis a property-only reading would miss.",
    ),
))

# ---------------------------------------------------------------------------
# 9 — Beacon at Cherry Creek. AMBIGUOUS: no number, GL vs installation floater.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="cherry-creek-water-damage",
    title="Cherry Creek Medical Campus — escape of water from a new chilled water riser",
    line_of_business="Liability — commercial general liability",
    broker="Front Range Commercial Insurance Group, Inc.",
    handler="Hollis Baumgartner-Reyes", handler_role="Account Executive",
    handler_email="h.baumgartner-reyes@frcig.example",
    handler_phone="+1 303 555 0198",
    broker_reference="FRC/GL/2025/8804",
    reported_on="9 February 2026",
    insured="Beacon Mechanical Services, Inc.",
    policy_number="",
    policy_type="Not stated — client holds both a liability and an installation floater",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Cherry Creek Medical Campus Owner, LLC",
    claimant_contact="Terrance Achebe-Lindgren, Project Manager (insured)",
    claimant_email="t.achebe-lindgren@beaconmech.example",
    date_of_loss="7 February 2026", time_of_loss="from about 19:30, discovered 07:10 on 8 February",
    loss_location="3400 South Cherry Creek Drive North, Denver, CO 80209",
    cause="Escape of water — grooved coupling separation on a newly installed riser",
    description=(
        "The insured's crew filled and pressurised a new chilled water riser on Saturday "
        "afternoon and left site at about 17:00. A 6-inch grooved coupling on the riser "
        "separated at Level 4 during the evening and water discharged for approximately "
        "eleven and a half hours before it was found on Sunday morning. Water spread from "
        "the Level 4 riser closet into the corridor and three tenant suites, down through "
        "slab penetrations to Level 3, and into the Level 1 mechanical room."),
    affected_assets=(
        "Third-party: Level 4 corridor and Suites 410 and 415; Suite 315 including a "
        "wall-mounted panoramic X-ray unit and two operatory cabinets; Level 1 and 2 "
        "common areas. The insured's own: installation materials in the mechanical room, "
        "a pipe threading machine and hand tools, and the new riser itself"),
    business_interruption="Yes — three tenant practices closed",
    structural_damage="No",
    environmental_exposure="No",
    authorities="None",
    incident_reference="BMS-2026-0207",
    potential_litigation="Yes — the general contractor has served an indemnity demand",
    asset_heading="Property affected, by owner",
    asset_rows=(
        ("Suite 410 — third party", "Front Range Orthopaedic, ceiling and flooring"),
        ("Suite 415 — third party", "Cherry Creek Dermatology, ceiling and flooring"),
        ("Suite 315 — third party", "Cherry Creek Dental Arts, panoramic X-ray unit"),
        ("Level 1 mechanical room", "Insured's installation materials, 400 ft copper"),
        ("Level 1 mechanical room", "Insured's tools — threading machine, crimpers"),
    ),
    estimated_loss="590,000", repair_estimate="425,000",
    repair_estimate_label="Third-party reinstatement",
    report_slug="cherry-creek-mechanical-failure-report",
    report_title="Mechanical failure investigation report",
    report_firm="Aspen Forensic Mechanical",
    report_reference="AFM-2026-0211",
    report_author="Lucien Okonkwo-Hartley, PE",
    report_instructions=(
        ("Instructed by", "Front Range Mutual Casualty Insurance Company"),
        ("Instructed on", "9 February 2026"),
        ("Site attendance", "10 February 2026"),
        ("Scope", "Cause of the coupling separation and the allocation of loss"),
    ),
    report_circumstances=(
        "The insured is the mechanical subcontractor on a chiller plant replacement and "
        "hydronic distribution upgrade in an occupied and operating medical office "
        "building. Its scope is the rooftop plant, the Level 1 mechanical room and the "
        "risers. Riser CHW-3 was filled and pressurised on 7 February."),
    report_findings=(
        "The separated coupling is a 6-inch rigid grooved coupling. The gasket is rolled "
        "over approximately 90 degrees of its circumference and shows extrusion into the "
        "bolt pad gap. Bolt torque measured on the retained housing halves is 61 and 68 "
        "N·m against a specified 136 N·m. The joint held during the fill and separated "
        "under overnight thermal and pressure cycling, which is consistent with an "
        "under-torqued joint and a rolled gasket at assembly rather than with a "
        "manufacturing defect. The joint was assembled by the insured's own foreman. "
        "The coupling, gasket, bolts and pipe ends are retained."),
    report_quantum=(
        ("Third-party building reinstatement, Levels 1 to 4", "USD 240,000"),
        ("Third-party tenant contents and equipment", "USD 185,000"),
        ("Insured's own installation materials", "USD 46,000"),
        ("Insured's own tools and equipment", "USD 38,000"),
        ("Riser and coupling rework", "USD 22,000"),
        ("Mitigation billed to the building owner", "USD 60,000"),
    ),
    report_comment_heading="On which policy answers, and the testing condition",
    report_comment=(
        "Roughly four fifths of the loss is third-party damage to an occupied building "
        "and its tenants, which is a liability exposure. The insured's own installation "
        "materials, its tools and the rework of the defective joint are first-party and "
        "belong on its installation and equipment floater, on which the cost of making "
        "good the defective coupling itself is in any event excluded. Both policies "
        "carry a mirrored water damage control condition requiring that systems are not "
        "left charged outside working hours unless the supply is isolated or leak "
        "detection with automatic shut-off is operational. The system was left charged "
        "over a weekend with nobody on site; whether either exception was satisfied has "
        "not yet been established and materially changes the retention."),
    report_recommendation=(
        "Set the primary file under the liability policy and open a companion file on "
        "the installation floater. Obtain the fill and pressure test record and the "
        "written notice to the general contractor before drawing any conclusion on the "
        "condition."),
    schedule_slug="cherry-creek-loss-allocation-schedule",
    schedule_title="Loss allocation schedule",
    schedule_headers=("Category", "Item", "Belongs on", "Total USD"),
    schedule_rows=(
        ("Third party", "Building reinstatement, Levels 1 to 4", "Liability", "240000"),
        ("Third party", "Tenant contents, Suites 410 and 415", "Liability", "62000"),
        ("Third party", "Panoramic X-ray unit and cabinets, Suite 315", "Liability", "123000"),
        ("Third party", "Mitigation billed by the building owner", "Liability", "60000"),
        ("Own property", "Installation materials, 400 ft copper and valves", "Installation floater", "46000"),
        ("Own property", "Tools — threading machine, crimpers, band saw", "Equipment floater", "38000"),
        ("Own work", "Riser and coupling rework", "Excluded — defective work", "22000"),
    ),
    schedule_total_label="Total loss",
    email_subject="New loss - Beacon Mechanical Services - escape of water, Cherry Creek Denver CO",
    email_to="newclaim@frontrangemutual.example",
    email_cc="r.kirkbride-osei@beaconmech.example",
    email_date="Mon, 09 Feb 2026 10:22:41 -0700",
    email_message_id="frc-gl-2026-8804-a@frcig.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify an escape of water on behalf of our "
        "client Beacon Mechanical Services, Inc. Our reference is FRC/GL/2025/8804.\n\n"
        "The client's controller rang us this morning and was candid that she did not know "
        "which of their two placements this belongs on. We hold both their liability policy "
        "and their combined installation and equipment floater. Our view, and the "
        "engineer's, is that the primary file is liability with a companion floater file; "
        "the allocation schedule is attached."),
    email_facts=(
        ("Broker reference", "FRC/GL/2025/8804"),
        ("Insured name", "Beacon Mechanical Services, Inc."),
        ("Insured trading as", "Beacon HVAC & Plumbing"),
        ("Date of loss", "7 February 2026"),
        ("Loss location", "3400 South Cherry Creek Drive North, Denver, CO 80209"),
        ("Cause", "Escape of water - grooved coupling separation on a new riser"),
        ("Estimated loss", "USD 590,000"),
    ),
    email_narrative=(
        "Our client's crew filled and pressurised a new chilled water riser on Saturday\n"
        "afternoon and left site. A coupling let go at Level 4 in the evening and ran for\n"
        "about eleven and a half hours before it was found on Sunday morning.\n\n"
        "The building is occupied and operating. Three tenant practices are affected and\n"
        "all three are closed - a dental practice has lost a wall-mounted panoramic X-ray\n"
        "unit. The general contractor has already served an indemnity demand.\n\n"
        "The building owner is a scheduled additional insured on our client's liability\n"
        "policy for this project. Our client is not disputing that it was their joint.\n"
        "The failed coupling, gasket and bolts have been retained and not discarded."),
    email_closing=(
        "Please confirm a claim reference on the liability file and a companion reference\n"
        "on the floater. We are chasing the fill and pressure test record."),
    expected="GL-8804-27153", confidence="Possible",
    why=("No policy number and the insured holds two policies with the same carrier, the same "
         "broker and the same broker domain. The discriminators are the loss type — four fifths "
         "is third-party damage to an occupied building, which is liability — and the fact that "
         "the building owner is a scheduled additional insured on the liability policy for this "
         "very project. The `FRC/GL/…` broker reference points the same way."),
    exercises=(
        "**Two policies, one insured, one carrier, one broker.** Insured name, insured domain, "
        "broker name and broker domain are all identical across both candidates and therefore "
        "discriminate nothing. Only line of business and the broker reference separate them.",
        "**A split allocation is the right answer.** The schedule states which head belongs on "
        "which policy, so a matcher that returns one policy and ignores the other is only "
        "four-fifths right.",
        "**A condition that changes the retention, not the rank.** The water damage control "
        "condition is a warning.",
    ),
))

# ---------------------------------------------------------------------------
# 10 — Tidewater, open roof at a school. Policy number stated.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="driver-middle-school-open-roof",
    title="Driver Middle School — rain into an open roof during a re-roof",
    line_of_business="Liability — commercial general liability",
    broker="Coastline Brantley Insurance Advisors",
    handler="Sylvia Okonjo-Pratt", handler_role="Producer",
    handler_email="s.okonjo-pratt@coastlinebrantley.example",
    handler_phone="+1 757 555 0271",
    broker_reference="CB/ROOF/2024/3376",
    reported_on="19 June 2025",
    insured="Tidewater Roofing & Exteriors, LLC",
    policy_number="GL-3376-90284",
    policy_type="Commercial General Liability - Occurrence",
    policy_period="1 November 2024 to 1 November 2025",
    policy_limit="USD 1,000,000 each occurrence", policy_deductible="USD 10,000 property damage",
    claimant="Suffolk City Public Schools",
    claimant_contact="Marguerite Delacroix-Iwu, Facilities Director",
    claimant_email="m.delacroix-iwu@suffolkcps.example",
    date_of_loss="18 June 2025", time_of_loss="16:40 EDT",
    loss_location="4652 Driver Lane, Suffolk, VA 23435",
    cause="Water damage — rain into a roof left open during tear-off",
    description=(
        "The insured tore off approximately 9,400 sq ft of existing built-up roofing "
        "across Sections C and D under a summer-recess re-roof contract. Tear-off "
        "finished at 15:30 and the crew began temporary dry-in. A fast-developing "
        "thunderstorm arrived at about 16:40, roughly ninety minutes before planned "
        "dry-in completion. Approximately 6,200 sq ft had been dried in; approximately "
        "3,200 sq ft had not. Water entered over eight classrooms and part of the media "
        "centre."),
    affected_assets=(
        "Eight classrooms C-104 to C-111 with ceiling collapse in six; the media centre "
        "including approximately 1,850 library volumes and two computer carts; Corridor "
        "C ceiling and four troffers; two 277/480V lighting panels; eight smoke detectors"),
    business_interruption="No — the school was on summer recess",
    structural_damage="No — deck and insulation saturated but sound",
    environmental_exposure="No",
    authorities="None",
    incident_reference="SCPS-2025-RF-011",
    asset_heading="Third-party property affected",
    asset_rows=(
        ("Classrooms C-104 to C-111", "Ceiling grid, VCT flooring, drywall, casework"),
        ("Media centre", "2,400 sq ft, carpet, 1,850 volumes, 28 Chromebooks"),
        ("Corridor C", "Ceiling tile and four troffers over 120 lin ft"),
        ("C-wing electrical closet", "Two 277/480V lighting panels de-energised"),
        ("Fire alarm", "Eight detectors in trouble, 36-hour fire watch"),
    ),
    estimated_loss="510,000", repair_estimate="422,000",
    repair_estimate_label="Third-party reinstatement",
    report_slug="driver-school-roofing-consultants-report",
    report_title="Roofing consultant's report",
    report_firm="Sentinel Roof Consulting",
    report_reference="SRC-2025-0619",
    report_author="Aurelio Bekele-Thornton, RRC",
    report_instructions=(
        ("Instructed by", "Suffolk City Public Schools as owner's representative"),
        ("Instructed on", "19 June 2025"),
        ("Site attendance", "19 June 2025"),
        ("Scope", "Whether the open area was protected to the standard the contract requires"),
    ),
    report_circumstances=(
        "A phased TPO re-roof of a single-storey academic wing under a summer-recess "
        "contract. The system is mechanically fastened; no torch, kettle or open flame is "
        "used. All labour on 18 June was the contractor's own."),
    report_findings=(
        "The contractor's daily record shows a National Weather Service forecast check at "
        "06:15 returning a 30% probability of precipitation for the work period. The "
        "tear-off quantity of 9,400 sq ft is consistent with the crew's demonstrated "
        "dry-in capability of about 10,000 sq ft per day. Dry-in photographs were taken "
        "at 09:20, 13:05 and 16:35. The 6,200 sq ft that had been mechanically fastened "
        "and lapped did not leak; every point of entry is within the 3,200 sq ft that had "
        "not been reached when the cell arrived. In this consultant's opinion the "
        "contractor's sequencing was reasonable and the storm was not forecast at a "
        "probability that should have stopped the tear-off."),
    report_quantum=(
        ("Building interior, eight classrooms", "USD 268,000"),
        ("Electrical, lighting, fire alarm and fire watch", "USD 74,000"),
        ("Library volumes and media centre contents", "USD 61,000"),
        ("Computer carts and devices", "USD 19,000"),
        ("Cleaning, drying and mitigation", "USD 88,000"),
    ),
    report_comment_heading="On the open roof condition",
    report_comment=(
        "The liability policy conditions water damage cover on daily forecast checking, "
        "not opening more roof than can be dried in, mechanically fastened temporary "
        "covering and daily photographs. On the documents produced, each limb is "
        "satisfied. The sublimit rather than the exclusion should therefore apply."),
    report_recommendation=(
        "Complete drying before reinstatement begins. Re-inspect the deck fastener "
        "pattern in the saturated area before the new system is laid."),
    schedule_slug="driver-school-reinstatement-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Area", "Trade", "Description", "Total USD"),
    schedule_rows=(
        ("C-104 to C-111", "Building", "Ceiling grid and tile, eight rooms", "96000"),
        ("C-104 to C-111", "Building", "VCT flooring and drywall", "112000"),
        ("C-104 to C-111", "Joinery", "Casework, three rooms", "60000"),
        ("Media centre", "Building", "Carpet and ceiling, 2,400 sq ft", "38000"),
        ("Media centre", "Contents", "1,850 volumes, 900 unsalvageable", "42000"),
        ("Media centre", "IT", "Two carts, 28 devices", "19000"),
        ("Corridor C", "Building", "Ceiling tile and four troffers", "18000"),
        ("C-wing", "Electrical", "Two lighting panels, megger and recommission", "36000"),
        ("C-wing", "Fire alarm", "Eight detectors and 36-hour fire watch", "38000"),
        ("All", "Mitigation", "Extraction, drying, dehumidification", "88000"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="Liability notice - Tidewater Roofing - water damage, Driver Middle School VA - GL-3376-90284",
    email_to="claims@tidewaterexchange.example",
    email_cc="b.tsigaridas-cole@tidewaterroofing.example",
    email_date="Thu, 19 Jun 2025 09:05:14 -0400",
    email_message_id="cb-roof-2025-3376-a@coastlinebrantley.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify water damage to a third party's "
        "building on behalf of our client Tidewater Roofing & Exteriors, LLC. Our "
        "reference is CB/ROOF/2024/3376. This is within the 48-hour requirement for water "
        "intrusion into a building our client has worked on."),
    email_facts=(
        ("Policy number", "GL-3376-90284"),
        ("Insured name", "Tidewater Roofing & Exteriors, LLC"),
        ("Contract number", "SCPS-2025-RF-011"),
        ("Date of loss", "18 June 2025"),
        ("Loss location", "4652 Driver Lane, Suffolk, VA 23435"),
        ("Cause", "Rain into a roof left open during tear-off"),
        ("Estimated loss", "USD 510,000"),
    ),
    email_narrative=(
        "Our client is part way through a phased TPO re-roof at Driver Middle School under\n"
        "a summer-recess contract with Suffolk City Public Schools, who are a scheduled\n"
        "certificate holder on the policy.\n\n"
        "They tore off about 9,400 sq ft on Wednesday and had 6,200 sq ft dried in when a\n"
        "fast-developing cell arrived about ninety minutes early. Water came in over the\n"
        "3,200 sq ft that had not been reached, into eight classrooms and the media centre.\n\n"
        "There was no hot work - it is a mechanically fastened system - and no\n"
        "subcontractors; all labour was our client's own W-2 crew. The morning forecast\n"
        "check, the tear-off photographs and the dry-in photographs are all retained and\n"
        "are with the consultant's report."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. The division has referenced\n"
        "liquidated damages under the construction contract; our client's position is that\n"
        "those are contractual and are not tendered as an insured loss."),
    expected="GL-3376-90284", confidence="Exact",
    why=("Policy number stated, corroborated by the insured, the school and division which are "
         "scheduled certificate holders on this policy, the contract number, the broker "
         "reference and the sender domain."),
    exercises=(
        "**A named certificate holder as the risk location.** The loss is at a third party's "
        "premises, not the insured's yard, so `risk_location` must reach the certificate holder "
        "schedule rather than the insured's own address.",
        "**Conditions precedent evidenced rather than asserted.** The consultant's report "
        "addresses each limb of the open roof condition on documents.",
        "**A head of loss expressly not claimed.** Liquidated damages are named and disclaimed.",
    ),
))

# ---------------------------------------------------------------------------
# 11 — Ironbark, excavator fire in transit. Equipment floater number stated.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="ironbark-excavator-fire",
    title="Ironbark Constructors — excavator fire in transit on I-10",
    line_of_business="Inland marine — contractors equipment",
    broker="Sonoran Ridge Risk Advisors, LLC",
    handler="Desmond Achterberg", handler_role="Producer",
    handler_email="d.achterberg@sonoranridgerisk.example",
    handler_phone="+1 602 555 0510",
    broker_reference="SRR/IM/2025/2298",
    reported_on="18 November 2025",
    insured="Ironbark Constructors, Inc.",
    policy_number="IM-2298-66401",
    policy_type="Contractors Equipment Floater",
    policy_period="1 May 2025 to 1 May 2026",
    policy_limit="USD 10,000,000 any one occurrence", policy_deductible="USD 25,000",
    claimant="Ironbark Constructors, Inc.",
    claimant_contact="Alina Petrosyan-Ward, Corporate Risk Manager",
    claimant_email="a.petrosyan-ward@ironbarkconstructors.example",
    date_of_loss="14 November 2025", time_of_loss="13:05 MST",
    loss_location="Interstate 10 westbound, milepost 168, near Casa Grande, AZ",
    cause="Fire — hydraulic line failure onto the exhaust while in transit",
    description=(
        "A 2023 Caterpillar 349 excavator was being moved on the insured's own lowboy "
        "trailer from its Mesa satellite yard to a job site in Buckeye. The driver saw "
        "smoke from the engine compartment at highway speed, pulled onto the shoulder and "
        "found flame at the turbocharger and exhaust manifold. A hand extinguisher had no "
        "effect. Casa Grande Fire Department attended. The fire consumed the engine "
        "compartment, the hydraulic system, the cab and the wiring harness."),
    affected_assets=(
        "Scheduled Item 001, a 2023 Caterpillar 349 hydraulic excavator, serial "
        "CAT0349FKMBX2117, constructive total loss; a tilt-rotate bucket and hydraulic "
        "thumb; Trimble GNSS machine control components; transport chains, binders and "
        "the trailer rub rail"),
    business_interruption="No — substitute machine hired from 18 November",
    structural_damage="No",
    environmental_exposure="Yes — hydraulic fluid and diesel on the highway shoulder",
    authorities="Casa Grande Fire Department; Pinal County Sheriff; ADOT",
    incident_reference="CGFD-2025-0114772",
    asset_heading="Equipment affected",
    asset_rows=(
        ("Scheduled Item 001", "2023 Caterpillar 349, 49 t, long reach boom"),
        ("Serial number", "CAT0349FKMBX2117"),
        ("Hour meter", "3,118 hours at last reading, 11 November 2025"),
        ("Scheduled limit", "USD 685,000, replacement cost basis"),
        ("Scheduled Item 020", "Trimble GNSS components mounted on the machine"),
    ),
    estimated_loss="715,950", repair_estimate="690,950",
    repair_estimate_label="Net indicated after deductible",
    report_slug="ironbark-equipment-assessors-report",
    report_title="Equipment assessor's report",
    report_firm="Copper State Plant Assessors",
    report_reference="CSPA-2025-1174",
    report_author="Rosalind Vukovic-Adeyemi, Plant Engineer",
    report_instructions=(
        ("Instructed by", "Desert Basin Specialty Insurance Company"),
        ("Instructed on", "17 November 2025"),
        ("Site attendance", "19 November 2025, Phoenix main yard"),
        ("Scope", "Cause, extent and settlement basis"),
    ),
    report_circumstances=(
        "The machine was recovered on the day of loss and has been stored tarped in a "
        "fenced and camera-monitored area at the insured's Phoenix main yard. It has not "
        "been repaired, dismantled, cleaned or disposed of, and the suspect hydraulic "
        "return line remains in situ in the burned compartment."),
    report_findings=(
        "The fire originated at the rear of the pump compartment. A hydraulic return line "
        "has failed at the hose-to-fitting interface approximately 300 mm from the "
        "turbocharger shield. The remaining hose shows cover cracking and a burst pattern "
        "consistent with release under pressure rather than with external fire damage "
        "propagating inward. Exhaust surface temperature at that location on this model "
        "exceeds the fluid's auto-ignition temperature in normal operation. The machine "
        "is a constructive total loss: the cab, harness, hydraulic circuit and engine "
        "ancillaries are destroyed and the cost of rebuild exceeds a replacement. A "
        "3,000-hour service including hydraulic hose inspection was completed by the "
        "dealer on 22 October 2025, three weeks before the loss."),
    report_quantum=(
        ("Excavator, scheduled limit, replacement cost", "USD 685,000"),
        ("Trimble GNSS machine control components", "USD 14,200"),
        ("Chains, binders and trailer rub rail", "USD 3,900"),
        ("Recovery and heavy haul", "USD 8,750"),
        ("Wreck removal and shoulder cleanup", "USD 4,100"),
    ),
    report_comment_heading="On breakdown and ensuing fire",
    report_comment=(
        "The policy excludes mechanical and hydraulic breakdown but expressly covers "
        "resulting damage from an ensuing fire. The line failure is the breakdown; the "
        "fire is the ensuing peril and is the whole of the loss. The dealer's inspection "
        "three weeks earlier is relevant to a recovery against that dealer, not to cover."),
    report_recommendation=(
        "Settle on the scheduled limit. The replacement quotation of USD 712,400 exceeds "
        "the schedule, so the schedule is the operative cap and no betterment arises."),
    schedule_slug="ironbark-equipment-loss-schedule",
    schedule_title="Equipment loss schedule",
    schedule_headers=("Item", "Description", "Serial", "Basis", "Total USD"),
    schedule_rows=(
        ("001", "Caterpillar 349 excavator, 2023", "CAT0349FKMBX2117", "Scheduled limit", "685000"),
        ("020", "Trimble GNSS machine control, part", "Multiple", "Replacement cost", "14200"),
        ("—", "Chains, binders, trailer rub rail", "n/a", "Replacement cost", "3900"),
        ("—", "Recovery and heavy haul", "n/a", "Invoice", "8750"),
        ("—", "Wreck removal and shoulder cleanup", "n/a", "Invoice", "4100"),
    ),
    schedule_total_label="Gross claim before deductible",
    email_subject="FNOL - Ironbark Constructors - excavator fire in transit, I-10 AZ - IM-2298-66401",
    email_to="equipmentclaims@desertbasinspecialty.example",
    email_cc="a.petrosyan-ward@ironbarkconstructors.example",
    email_date="Tue, 18 Nov 2025 11:26:09 -0700",
    email_message_id="srr-im-2025-2298-a@sonoranridgerisk.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify the total loss by fire of a scheduled "
        "excavator belonging to our client Ironbark Constructors, Inc. Our reference is "
        "SRR/IM/2025/2298."),
    email_facts=(
        ("Policy number", "IM-2298-66401"),
        ("Insured name", "Ironbark Constructors, Inc."),
        ("Scheduled item", "Item 001, Caterpillar 349, serial CAT0349FKMBX2117"),
        ("Date of loss", "14 November 2025"),
        ("Loss location", "Interstate 10 westbound, milepost 168, near Casa Grande, AZ"),
        ("Cause", "Fire - hydraulic line failure onto the exhaust"),
        ("Estimated loss", "USD 715,950"),
    ),
    email_narrative=(
        "The machine was on our client's own lowboy moving between their Mesa yard and a\n"
        "job in Buckeye. The driver saw smoke, pulled over, and found flame at the\n"
        "turbocharger. Casa Grande Fire attended. It is a constructive total loss. No\n"
        "injuries and no other vehicles involved.\n\n"
        "The lowboy trailer itself and the ADOT highway damage are motor vehicle exposures\n"
        "and are excluded from this floater under the licensed highway vehicle exclusion.\n"
        "We are presenting those to the commercial auto carrier separately.\n\n"
        "The machine has not been touched. It is tarped in the client's Phoenix yard and\n"
        "the suspect hydraulic line is still in place in the burned compartment."),
    email_closing=(
        "Please confirm a claim reference and an assessor. A substitute machine was hired\n"
        "from 18 November, so rental reimbursement is running."),
    expected="IM-2298-66401", confidence="Exact",
    why=("Policy number stated and confirmed at item level: the damaged machine is scheduled "
         "Item 001 with a matching serial number and limit, both yard addresses are on the "
         "policy, and the broker reference and sender domain agree."),
    exercises=(
        "**Identity down to the serial number.** The match is corroborated by an asset "
        "identifier that appears on the policy schedule, which is stronger than an address.",
        "**A loss location that is on no schedule.** The loss happened on a highway. "
        "`risk_location` cannot help, and the match must carry on the other axes — which is "
        "how a floater should behave, because the whole point is that the property moves.",
        "**Another policy expressly ruled out.** The trailer and highway damage are named as "
        "belonging to a motor policy that is not in the book.",
    ),
))

# ---------------------------------------------------------------------------
# 12 — Beacon, Aurora jobsite theft. NO policy number. Both floater sections.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="aurora-jobsite-theft",
    title="Aurora Gateway Logistics Park — jobsite theft of tools and installation copper",
    line_of_business="Inland marine — equipment and installation",
    broker="Front Range Commercial Insurance Group, Inc.",
    handler="Hollis Baumgartner-Reyes", handler_role="Account Executive",
    handler_email="h.baumgartner-reyes@frcig.example",
    handler_phone="+1 303 555 0198",
    broker_reference="FRC/IM/2025/7741",
    reported_on="15 September 2025",
    insured="Beacon Mechanical Services, Inc.",
    policy_number="",
    policy_type="Not stated on the notice",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Beacon Mechanical Services, Inc.",
    claimant_contact="Terrance Achebe-Lindgren, Project Manager",
    claimant_email="t.achebe-lindgren@beaconmech.example",
    date_of_loss="13 September 2025", time_of_loss="overnight, discovered 14 September 06:20",
    loss_location="19400 East 32nd Parkway, Aurora, CO 80011",
    cause="Theft — forced entry to a job trailer and a shared container",
    description=(
        "The padlock on the insured's job trailer was cut and a shared conex container "
        "was breached at a ground-up distribution building where the insured is the "
        "mechanical subcontractor. Tools were taken from the trailer and installation "
        "material from the container and the laydown: copper tube staged for the domestic "
        "water rough-in, press fittings and brass valves, pre-insulated copper, and four "
        "packaged condensing units. Two further condensing units were dropped and left "
        "with crushed fin coils, and about 30 feet of temporary power cord was cut and "
        "stripped."),
    affected_assets=(
        "Ridgid 1224 threading machine and Ridgid 918-I roll groover, both scheduled; two "
        "press tools; four cordless kits, band saws, rotary hammers and core drills; two "
        "thermal imagers and an air balancing hood; an engine drive welder; 1,400 ft of "
        "type L copper; press fittings and brass valves; 600 ft of pre-insulated copper; "
        "four Trane condensing units"),
    business_interruption="No — but the copper drives a three-week re-order",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Aurora Police Department",
    police_reference="APD-2025-0227418",
    incident_reference="BMS-AUR-2025-0044",
    asset_heading="Property taken, by section",
    asset_rows=(
        ("Trailer T-2 — tools", "Threading machine, roll groover, press tools, cordless kits"),
        ("Trailer T-2 — instruments", "Two Fluke thermal imagers, Shortridge balancing hood"),
        ("Laydown — plant", "Miller engine drive welder, chained to the trailer tongue"),
        ("Conex — installation", "1,400 ft type L copper, fittings, valves"),
        ("Laydown — installation", "Four Trane 5-ton condensing units, two more damaged"),
    ),
    estimated_loss="224,300", repair_estimate="224,300",
    repair_estimate_label="Replacement cost",
    report_slug="aurora-theft-investigation-report",
    report_title="Theft investigation report",
    report_firm="Mile High Loss Investigation",
    report_reference="MHLI-2025-3318",
    report_author="Ottoline Baptiste-Nakagawa, CFE",
    report_instructions=(
        ("Instructed by", "Front Range Mutual Casualty Insurance Company"),
        ("Instructed on", "15 September 2025"),
        ("Site attendance", "16 September 2025"),
        ("Scope", "Circumstances, security condition compliance and quantum"),
    ),
    report_circumstances=(
        "A ground-up 310,000 sq ft distribution building. The insured's scope is the "
        "rooftop package, gas piping, plumbing rough-in and office HVAC. The site is "
        "shared with several trades and the conex breached was shared with the electrical "
        "subcontractor."),
    report_findings=(
        "Entry was by bolt cutter to the trailer padlock and by levering the conex "
        "locking bar. The selection is discriminating: high-resale tools and copper were "
        "taken and low-value consumables were left, which indicates a targeted rather than "
        "opportunistic theft. Serial numbers for the threading machine, the roll groover "
        "and the welder were filed with the National Equipment Register on 14 September. "
        "The cut chain, the cut padlock and the sheared locking bar have been bagged and "
        "retained, and nothing has been repaired or cleared. On the security condition: "
        "the copper and the condensing units were on the open laydown rather than in a "
        "locked container or a secured interior room, which is what the condition "
        "requires for high-theft property."),
    report_quantum=(
        ("Tools from trailer T-2", "USD 71,400"),
        ("Engine drive welder", "USD 11,200"),
        ("Copper pipe, fittings, valves, pre-insulated", "USD 96,800"),
        ("Four Trane condensing units", "USD 27,600"),
        ("Two damaged condensing units", "USD 9,400"),
        ("Trailer, conex and temporary power", "USD 7,900"),
    ),
    report_comment_heading="On which section answers",
    report_comment=(
        "The tools, the welder and the trailer damage are contractors equipment and sit "
        "on Section 2. The copper, the fittings, the valves and the condensing units are "
        "material intended to become a permanent part of the building and sit on Section "
        "1, the installation floater — which is written excess over any builder's risk "
        "carried by the owner or the general contractor. The general contractor's position "
        "on its own builder's risk should be established before this is settled."),
    report_recommendation=(
        "Apply the copper theft deductible to the Section 1 heads and the equipment "
        "deductible to Section 2. Establish the general contractor's builder's risk "
        "position before payment."),
    schedule_slug="aurora-theft-schedule",
    schedule_title="Schedule of property taken",
    schedule_headers=("Section", "Item", "Quantity", "Total USD"),
    schedule_rows=(
        ("Equipment", "Ridgid 1224 threading machine with die set", "1", "18500"),
        ("Equipment", "Ridgid 918-I hydraulic roll groover", "1", "14200"),
        ("Equipment", "Ridgid RP 351 press tools with jaw sets", "2", "16800"),
        ("Equipment", "Cordless kits, band saws, hammers, core drills", "11", "12400"),
        ("Equipment", "Fluke thermal imagers and balancing hood", "3", "9500"),
        ("Equipment", "Miller engine drive welder", "1", "11200"),
        ("Installation", "Type L copper, 2 in, 2.5 in and 4 in", "1400", "61200"),
        ("Installation", "Press fittings and brass valves", "2", "18400"),
        ("Installation", "Pre-insulated copper", "600", "17200"),
        ("Installation", "Trane 5-ton condensing units, taken", "4", "27600"),
        ("Installation", "Trane condensing units, fin coil damage", "2", "9400"),
        ("Equipment", "Trailer door, hasp, conex bar, temporary power", "1", "7900"),
    ),
    schedule_total_label="Total taken and damaged",
    email_subject="New loss - Beacon Mechanical Services - jobsite theft, Aurora CO",
    email_to="imclaims@frontrangemutual.example",
    email_cc="t.achebe-lindgren@beaconmech.example",
    email_date="Mon, 15 Sep 2025 10:14:52 -0600",
    email_message_id="frc-im-2025-7741-a@frcig.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a jobsite theft on behalf of our "
        "client Beacon Mechanical Services, Inc. Our reference is FRC/IM/2025/7741.\n\n"
        "Their controller is away and they could not give us the policy number, so please "
        "locate on our reference. This is their combined equipment and installation "
        "floater, not the liability policy - it is first-party theft of their own property."),
    email_facts=(
        ("Broker reference", "FRC/IM/2025/7741"),
        ("Insured name", "Beacon Mechanical Services, Inc."),
        ("Insured trading as", "Beacon HVAC & Plumbing"),
        ("Date of loss", "13 September 2025"),
        ("Loss location", "19400 East 32nd Parkway, Aurora, CO 80011"),
        ("Cause", "Theft - forced entry to a job trailer and a shared container"),
        ("Estimated loss", "USD 224,300"),
    ),
    email_narrative=(
        "Someone cut the padlock on our client's job trailer over the weekend and levered\n"
        "the shared conex. They took the tools with resale value and left the rest, and\n"
        "they took the copper - which is the part that hurts, because it drives the next\n"
        "three weeks of their programme.\n\n"
        "Two of the scheduled items are gone: the Ridgid threading machine and the roll\n"
        "groover. Serials are filed with the National Equipment Register.\n\n"
        "The loss spans both sections. The tools and the welder are equipment; the copper,\n"
        "the fittings and the condensing units are installation material going into the\n"
        "building. The general contractor is asking whether their builder's risk picks up\n"
        "the copper - our reading is that our client's installation cover sits excess of it."),
    email_closing=(
        "Please confirm a claim reference. Our client needs your position on the copper\n"
        "sublimit by Thursday or they slip the riser."),
    expected="IM-7741-15530", confidence="Strong",
    why=("No policy number, but the broker reference `FRC/IM/2025/7741` is the one on this "
         "policy, two of the stolen items are scheduled equipment on it, and the loss spans "
         "both sections of a combined equipment and installation floater. The insured's "
         "liability policy cannot answer first-party theft of the insured's own property."),
    exercises=(
        "**The broker reference distinguishing two policies of one insured.** Insured name and "
        "both domains are identical across `GL-8804-27153` and `IM-7741-15530`; the reference "
        "and the line of business are the only separators.",
        "**Scheduled items as corroboration.** Two stolen tools appear on the equipment schedule.",
        "**Excess-of-builder's-risk.** The notice raises the general contractor's policy, which "
        "affects order of response, not identification.",
    ),
))

# ---------------------------------------------------------------------------
# 13 — Harborline, Delaware terminal freeze. NO number. SECONDARY location.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="harborline-delaware-freeze",
    title="Harborline Delaware terminal — frozen sprinkler line",
    line_of_business="Commercial property",
    broker="Talbot & Rennick Insurance Brokers, Inc.",
    handler="Diane Whitcomb-Reyes", handler_role="Account Executive",
    handler_email="d.whitcomb-reyes@talbotrennick.example",
    handler_phone="+1 410 555 0182",
    broker_reference="TR/PROP/2025/4471",
    reported_on="9 February 2026",
    insured="Harborline Cold Storage & Logistics, LLC",
    policy_number="",
    policy_type="Commercial Property",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Harborline Cold Storage & Logistics, LLC",
    claimant_contact="Nathaniel Osei-Barrow, VP Operations",
    claimant_email="n.osei-barrow@harborlinecold.example",
    date_of_loss="1 February 2026", time_of_loss="discovered 05:50 EST",
    loss_location="419 Delaware Terminal Road, New Castle, DE 19720",
    cause="Freeze — frozen sprinkler branch line",
    description=(
        "A 4-inch wet-pipe sprinkler branch line in the unheated dock canopy above doors "
        "14 to 22 froze and split during an extreme cold event. NWS Wilmington recorded a "
        "low of -4F with a 22 mph north-west wind, following three consecutive days below "
        "15F. Standing water covered approximately 31,000 sq ft of the cross-dock floor. "
        "Two 480V panels and a motor control centre took water at the base, six dock "
        "leveller pits were submerged, and 214 of 640 stored pallets had direct water "
        "contact."),
    affected_assets=(
        "31,000 sq ft of cross-dock floor; 340 linear feet by 8 feet of metal building "
        "insulation; two 480V distribution panels and one motor control centre; six "
        "hydraulic dock levellers; 214 water-affected pallets of which 137 bear "
        "third-party customer labels"),
    business_interruption="Yes — running out of doors 1 to 13 only, volume diverted to Baltimore",
    structural_damage="No",
    environmental_exposure="No",
    authorities="New Castle County AHJ notified of the sprinkler impairment",
    incident_reference="RTI-2026-0447",
    asset_heading="Location and property affected",
    asset_rows=(
        ("Location", "Delaware terminal — dry storage and cross-dock, 72,500 sq ft"),
        ("Note", "This is the insured's secondary premises, not the Baltimore cold store"),
        ("Floor area affected", "31,000 sq ft, standing water to 4 in at the trench drain"),
        ("Electrical", "Two 480V panels and one MCC, not re-energised"),
        ("Stock", "214 pallets water-affected, 137 customer-owned"),
    ),
    estimated_loss="1,240,000", repair_estimate="407,130",
    repair_estimate_label="Emergency mitigation to date",
    report_slug="harborline-delaware-mitigation-report",
    report_title="Emergency mitigation report",
    report_firm="RestorTech Industrial Services, LLC",
    report_reference="RTI-2026-0447",
    report_author="Solveig Marchetti-Adeyinka, IICRC CDS",
    report_instructions=(
        ("Instructed by", "Harborline Cold Storage & Logistics, LLC"),
        ("Instructed on", "1 February 2026, 07:35"),
        ("Site attendance", "1 to 8 February 2026"),
        ("Scope", "Emergency extraction, drying and contents handling"),
    ),
    report_circumstances=(
        "A single-storey metal frame dry storage and cross-dock building of about 72,500 "
        "sq ft, leased by the insured. The client refers to it internally as the Delaware "
        "terminal. It is not the Baltimore refrigerated facility and the two should not be "
        "confused on the file."),
    report_findings=(
        "Approximately 118,000 gallons were extracted between 1 and 3 February. Eighty-four "
        "air movers, twenty-two LGR dehumidifiers and four desiccant units were required; "
        "desiccant was necessary because of ambient temperature. Vinyl-faced blanket "
        "insulation was removed from the north and west walls to 8 feet across 340 linear "
        "feet as unsalvageable. Contents were segregated at pallet level with every label "
        "photographed, because the affected stock is a mixture of the client's own goods "
        "and customer-owned goods held under warehousing agreements. A contributing fact "
        "is recorded without comment on cover: the dock canopy unit heater serving the "
        "area where the line split was tagged out of service on 14 January with a failed "
        "ignition module, and the replacement part was on order."),
    report_quantum=(
        ("Extraction, 118,000 gallons, and ice removal", "USD 50,850"),
        ("Drying equipment and technician labour", "USD 191,680"),
        ("Containment, insulation removal and disposal", "USD 45,650"),
        ("Contents handling and third-party storage", "USD 53,100"),
        ("Electrical drying, dock pits, antimicrobial, documentation", "USD 65,850"),
    ),
    report_comment_heading="On the fire watch and the sprinkler impairment",
    report_comment=(
        "The split line was isolated by the client's fire protection contractor at 06:40 "
        "on 1 February and a continuous fire watch was established at 07:00, maintained "
        "until the system was restored to service at 16:20 on 4 February. The local "
        "authority was notified. RestorTech did not perform that work and offers no view "
        "on the protective safeguards warranty; the log is held by the client."),
    report_recommendation=(
        "Complete drying before reinstatement. Do not re-energise the panels or the MCC "
        "until the client's electrician has meggered and certified them."),
    schedule_slug="harborline-delaware-mitigation-schedule",
    schedule_title="Mitigation cost schedule",
    schedule_headers=("Head", "Description", "Quantity", "Total USD"),
    schedule_rows=(
        ("Response", "Emergency mobilisation, three crews, after hours", "1", "18400"),
        ("Extraction", "Truck mount, 118,000 gallons", "118000", "41250"),
        ("Extraction", "Ice removal, dock face and doors 14 to 22", "1", "9600"),
        ("Drying", "Air movers, 84 units over 8 days", "672", "26880"),
        ("Drying", "LGR dehumidifiers, 22 units over 8 days", "176", "31680"),
        ("Drying", "Desiccant units, 4 over 8 days", "32", "38400"),
        ("Labour", "Technicians, blended rate", "1184", "94720"),
        ("Building", "Containment and insulation removal", "340", "38900"),
        ("Contents", "Pallet handling, relocation and staging", "640", "38400"),
        ("Contents", "Third-party storage transport", "92", "14700"),
        ("MEP", "Electrical drying and dock leveller pits", "1", "28750"),
        ("Other", "Antimicrobial, documentation and disposal", "1", "25450"),
    ),
    schedule_total_label="Total mitigation to date",
    email_subject="New loss - Harborline - frozen sprinkler line, Delaware terminal New Castle DE",
    email_to="claims@meridianatlantic-ins.example",
    email_cc="n.osei-barrow@harborlinecold.example",
    email_date="Mon, 09 Feb 2026 08:55:30 -0500",
    email_message_id="tr-prop-2026-4471-b@talbotrennick.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a freeze and water damage loss for "
        "our client Harborline Cold Storage & Logistics, LLC. Our reference is "
        "TR/PROP/2025/4471.\n\n"
        "Please note this loss is at the client's **Delaware terminal**, not the Baltimore "
        "cold store. It is the scheduled dry storage and cross-dock location at New Castle. "
        "The building is leased and the owner is Delaware River Industrial Trust, LP, who "
        "are the loss payee for that premises."),
    email_facts=(
        ("Broker reference", "TR/PROP/2025/4471"),
        ("Insured name", "Harborline Cold Storage & Logistics, LLC"),
        ("Date of loss", "1 February 2026"),
        ("Loss location", "419 Delaware Terminal Road, New Castle, DE 19720"),
        ("Risk location", "Scheduled secondary premises - dry storage and cross-dock"),
        ("Cause", "Freeze - frozen sprinkler branch line"),
        ("Estimated loss", "USD 1,240,000"),
    ),
    email_narrative=(
        "A sprinkler branch line in the unheated dock canopy froze and split during the\n"
        "cold snap at the start of the month. It was found at 05:50 on Sunday and isolated\n"
        "by 06:40, but there was standing water across about 31,000 sq ft of the\n"
        "cross-dock floor by then.\n\n"
        "Two 480V panels and an MCC took water and are not re-energised. Six dock leveller\n"
        "pits were under. 214 of the 640 pallets on the floor had direct water contact and\n"
        "137 of those are customer-owned, so there is a bailee element again.\n\n"
        "A fire watch ran continuously from 07:00 on the 1st until the system was back in\n"
        "service on the 4th, and the county was notified."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Mitigation is already at USD\n"
        "407,130 and reconstruction has not started."),
    expected="CP-4471-88210", confidence="Strong",
    why=("No policy number. The loss address is scheduled Location 003 on the Harborline "
         "policy — the insured's secondary premises, not its headquarters — and the broker "
         "reference, the insured name, the loss payee for that premises and the sender domain "
         "all agree."),
    exercises=(
        "**Matching to a secondary scheduled location.** The insured's mailing address is in "
        "Baltimore and the loss is in Delaware. A matcher that compares the loss address only "
        "to `primary_location` misses it; it has to search the whole `policy_locations` schedule.",
        "**Naming the location that matched.** The candidate card should show Location 003 and "
        "*its* sum insured, not the Baltimore headline.",
        "**A second loss on a policy already claimed on.** Pack 1 is the same policy, a "
        "different premises and a different peril.",
    ),
))

# ---------------------------------------------------------------------------
# 14 — Windrow Grove, Building K fire. NO number. Fire report as the specialist doc.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="windrow-grove-building-k-fire",
    title="Windrow Grove Apartments — dryer fire in Building K",
    line_of_business="Commercial property",
    broker="Kettleridge Risk Partners, LLC",
    handler="Marcus Adeyemi-Croft", handler_role="Producer",
    handler_email="m.adeyemi-croft@kettleridgerisk.example",
    handler_phone="+1 918 555 0347",
    broker_reference="KRP/HAB/2025/7735",
    reported_on="5 December 2025",
    insured="Sundale Property Group, LLC",
    policy_number="",
    policy_type="Commercial Property - Habitational",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Windrow Grove Apartments, LP",
    claimant_contact="Dale Fontenot-Kimura, Community Manager",
    claimant_email="d.fontenot-kimura@sundalepg.example",
    date_of_loss="21 November 2025", time_of_loss="02:14 CST",
    loss_location="6120 East 91st Street, Tulsa, OK 74137 — Building K",
    cause="Fire — lint ignition at a clothes dryer",
    description=(
        "Fire started in the enclosed laundry alcove of Unit K-206 at or immediately "
        "behind the landlord-provided electric clothes dryer, from lint accumulated in "
        "the dryer cabinet and a crushed foil transition duct. It extended into the "
        "ceiling assembly and through the dryer vent penetration into a common attic void "
        "with no draftstopping at the unit demising lines, then ran east and west above "
        "the second and third floors. Forty per cent of the roof structure over the west "
        "half was opened for ventilation or consumed."),
    affected_assets=(
        "Units K-206 and K-306 total loss; K-106 heavy water damage with ceiling collapse; "
        "K-205, K-207, K-305, K-307 fire extension and overhaul; K-105 and K-107 water "
        "damage; the remaining 15 units smoke-affected and de-energised; 2,600 sq ft of "
        "charred trusses; radiant heat damage to Building L and two resident vehicles"),
    injuries="3",
    business_interruption="Yes — 24 units out, 47 residents displaced",
    structural_damage="Yes — 2,600 sq ft of roof trusses charred, 40% of the west roof opened",
    environmental_exposure="No",
    authorities="Tulsa Fire Department; Public Service Company of Oklahoma; American Red Cross",
    incident_reference="TFD-2025-0091447",
    asset_heading="Building and units affected",
    asset_rows=(
        ("Building", "Residential Building K, also Building 06 on the site plan"),
        ("Units", "24 dwelling units in three stacked tiers, built 2004, not sprinklered"),
        ("Total loss", "K-206 unit of origin and K-306 directly above"),
        ("Uninhabitable", "All 24 units pending electrical clearance"),
        ("Adjacent", "Building L east elevation, 400 sq ft siding, four windows"),
    ),
    estimated_loss="2,610,000", repair_estimate="2,260,000",
    repair_estimate_label="Structure and contents",
    report_slug="windrow-grove-fire-investigation-report",
    report_title="Fire investigation report",
    report_firm="Tulsa Fire Department, Fire Investigation Division",
    report_reference="TFD-2025-0091447",
    report_author="R. Halvorsen-Ekwueme, Fire Investigator, Badge 4471",
    report_instructions=(
        ("Report type", "NFIRS 111 — building fire"),
        ("Alarm", "21 November 2025, 02:14 CST"),
        ("Controlled", "03:41 CST"),
        ("Released", "Public records copy, 4 December 2025"),
    ),
    report_circumstances=(
        "A three-storey garden apartment building of wood frame with brick veneer and a "
        "composition shingle roof, built in 2004 and not sprinklered. Each unit has "
        "hard-wired interlinked smoke alarms; there is no common-area detection. On-site "
        "management identified the ownership entity as Windrow Grove Apartments LP and the "
        "manager as Sundale Residential Management, with a regional office in Tulsa."),
    report_findings=(
        "The area of origin is the enclosed laundry alcove of Unit K-206 and the point of "
        "origin is at or immediately behind the electric clothes dryer. The transition "
        "duct is a foil-type flexible connector, crushed behind the appliance, with heavy "
        "lint accumulation. Thermal patterns, the appliance's internal fire damage and the "
        "elimination of other ignition sources support ignition at the dryer. The cause is "
        "classified accidental. No indication of incendiary activity and no accelerant "
        "alert. The attic void is common across each stack and no draftstopping was "
        "observed at the unit demising walls in the area examined, which is what allowed "
        "the fire to run above the second and third floors."),
    report_quantum=(
        ("Structure loss estimate, NFIRS", "USD 1,850,000"),
        ("Contents loss estimate, NFIRS", "USD 410,000"),
        ("Pre-incident structure value", "USD 3,150,000"),
        ("Pre-incident contents value", "USD 620,000"),
    ),
    report_comment_heading="Evidence retained and interested parties",
    report_comment=(
        "The dryer, the flexible transition duct, the rigid wall-cavity duct section and "
        "the terminal block are held in secure evidence storage under numbers TFD-E-25-2211 "
        "to TFD-E-25-2214 and will be released to the property's insurer or its engineer on "
        "written request and a joint examination protocol. Management was advised in writing "
        "that the appliance manufacturer and the party responsible for installation may have "
        "an interest. The appliance was installed by the complex's maintenance department "
        "in 2019."),
    report_recommendation=(
        "Arrange a joint examination before any destructive testing. Referred to City of "
        "Tulsa Development Services on the question of attic draftstopping at construction."),
    schedule_slug="windrow-grove-fire-loss-schedule",
    schedule_title="Fire loss schedule",
    schedule_headers=("Unit or element", "Condition", "Basis", "Total USD"),
    schedule_rows=(
        ("K-206", "Total loss, unit of origin", "Reinstatement", "182000"),
        ("K-306", "Total loss", "Reinstatement", "182000"),
        ("K-106", "Ceiling collapse, heavy water", "Reinstatement", "96000"),
        ("K-205, K-207, K-305, K-307", "Fire extension and overhaul", "Reinstatement", "348000"),
        ("K-105, K-107", "Suppression water", "Reinstatement", "112000"),
        ("Remaining 15 units", "Smoke and de-energisation", "Reinstatement", "420000"),
        ("Roof structure", "2,600 sq ft trusses, decking, covering", "Reinstatement", "386000"),
        ("Exterior", "Brick spalling, soffit and fascia, 90 lin ft", "Reinstatement", "64000"),
        ("Building L", "Radiant heat, siding and four windows", "Reinstatement", "48000"),
        ("Contents and relocation", "Tenant relocation and landlord contents", "Policy heads", "422000"),
    ),
    schedule_total_label="Total indicated",
    email_subject="New loss - Windrow Grove Apartments, Tulsa OK - fire in Building K",
    email_to="newloss@cascadiamutual.example",
    email_cc="d.fontenot-kimura@sundalepg.example",
    email_date="Fri, 05 Dec 2025 11:41:07 -0600",
    email_message_id="krp-hab-2025-7735-b@kettleridgerisk.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a fire loss at our client's Windrow "
        "Grove community in Tulsa. Our reference is KRP/HAB/2025/7735. The fire department's "
        "public records copy has only just been released, which is why the formal notice "
        "follows the telephone report of 21 November."),
    email_facts=(
        ("Broker reference", "KRP/HAB/2025/7735"),
        ("Insured name", "Sundale Property Group, LLC"),
        ("Claimant", "Windrow Grove Apartments, LP"),
        ("Date of loss", "21 November 2025"),
        ("Loss location", "6120 East 91st Street, Tulsa, OK 74137 - Building K"),
        ("Cause", "Fire - lint ignition at a clothes dryer"),
        ("Estimated loss", "USD 2,610,000"),
    ),
    email_narrative=(
        "Fire started in the laundry alcove of a second-floor unit at about 02:14 and got\n"
        "into the attic through the dryer vent penetration. There is no draftstopping in\n"
        "the attic at the unit demising lines, so it ran the length of the stack.\n\n"
        "Two units are a total loss, all 24 in the building are uninhabitable pending\n"
        "electrical clearance, and 47 residents are displaced. Two residents were treated\n"
        "for smoke inhalation, one admitted overnight. No fatalities.\n\n"
        "The dryer is landlord-provided and was installed by the client's own maintenance\n"
        "department in 2019. The fire department is holding the appliance and the ducting\n"
        "as evidence and will release it on a joint examination protocol - you will want\n"
        "your engineer involved before anyone touches it."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Tenant relocation costs are\n"
        "running and our client would like your position on that head early."),
    expected="CP-7735-19042", confidence="Strong",
    why=("No policy number. The complex name and address are scheduled Location 001, the "
         "claimant is the additional named insured, the managing agent named in the fire report "
         "is the scheduled additional insured, the reported pre-incident structure value of USD "
         "3,150,000 is the per-building limit on the schedule, and the broker reference agrees."),
    exercises=(
        "**A specialist document that carries no policy reference at all.** A fire department "
        "report never states a policy number; everything identifying comes from the covering "
        "email and the loss notice.",
        "**Building letter against building number.** The report calls it Building K and "
        "cross-references Building 06 on the site plan, which is how the schedule names it.",
        "**Two losses, one policy, one year.** This and pack 2 are both on `CP-7735-19042`, "
        "different perils and different buildings — the duplicate-candidate check should not "
        "confuse them.",
    ),
))

# ---------------------------------------------------------------------------
# 15 — Fairmount, mezzanine collapse. Policy number stated. Second loss, same policy.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="fairmount-mezzanine-collapse",
    title="Fairmount Textile Mills — storage mezzanine collapse",
    line_of_business="Commercial property",
    broker="Ridgeway Kessler Brokerage Services",
    handler="Priya Raghunathan-Lowe", handler_role="Wholesale Broker",
    handler_email="p.raghunathan-lowe@ridgewaykessler.example",
    handler_phone="+1 864 555 0913",
    broker_reference="RK/MFG/2024/2210",
    reported_on="15 May 2025",
    insured="Fairmount Textile Mills Holdings, Inc.",
    policy_number="CP-2210-55870",
    policy_type="Manufacturing All Risk Property",
    policy_period="1 September 2024 to 1 September 2025",
    policy_limit="USD 85,000,000", policy_deductible="USD 250,000",
    claimant="Fairmount Technical Fabrics, Inc.",
    claimant_contact="Auberon Mkhize-Calloway, Director of Manufacturing Operations",
    claimant_email="a.mkhize-calloway@fairmounttextile.example",
    date_of_loss="14 May 2025", time_of_loss="15:40 EDT",
    loss_location="1180 Woodruff Industrial Road, Greenville, SC 29607 — Building 1D",
    cause="Collapse — bolted connection failure on a storage mezzanine",
    description=(
        "A steel storage mezzanine installed in 2018 over the north bay of Building 1D "
        "gave way during a routine fork truck let-down of fabric rolls. The collapse ran "
        "about 62 feet along the north wall over the full 28-foot depth of the mezzanine, "
        "bringing approximately 1,740 sq ft of deck down onto the racking and floor below. "
        "Nobody was on the mezzanine. The fork truck operator was at floor level about "
        "30 feet away and was not struck."),
    affected_assets=(
        "1,740 sq ft of collapsed mezzanine and a further 3,900 sq ft unloaded and shored; "
        "14 bays of pallet racking destroyed and 6 more to inspect; two ESFR branch lines "
        "and 11 heads; approximately 2,900 rolls of technical and coated fabric; north "
        "wall girts over 40 feet; two leased fork trucks"),
    business_interruption="Yes — the north bay is out of service, shipping run from the south bay and LOC 3",
    structural_damage="Yes — girts deformed, slab spalled, column baseplates to inspect",
    environmental_exposure="No",
    authorities="City of Greenville notified of the sprinkler impairment",
    incident_reference="FTM-2025-0514",
    asset_heading="Building and plant affected",
    asset_rows=(
        ("Building", "1D — Warehouse and Shipping, 88,000 sq ft, metal frame, 2007"),
        ("Mezzanine", "1,740 sq ft collapsed, 3,900 sq ft shored and withheld"),
        ("Racking", "Zone 3, 14 bays destroyed, 6 to inspect"),
        ("Fire protection", "ESFR, two branch lines and 11 heads sheared"),
        ("Stock", "Approximately 2,900 rolls, 610 total loss on triage"),
    ),
    estimated_loss="8,700,000", repair_estimate="2,833,000",
    repair_estimate_label="Reinstatement excluding stock and time element",
    report_slug="fairmount-mezzanine-engineers-report",
    report_title="Structural engineer's report",
    report_firm="Halberd Forensic Engineering",
    report_reference="HFE-2025-0515",
    report_author="P. Vanterpool-Aziz, PE, CFEI",
    report_instructions=(
        ("Instructed by", "Ridgeway Kessler Brokerage Services"),
        ("Instructed on", "14 May 2025"),
        ("Site attendance", "15 May 2025"),
        ("Scope", "Cause of the collapse and the condition of the remaining structure"),
    ),
    report_circumstances=(
        "The mezzanine was supplied and installed in 2018 by Foothills Material Handling "
        "Systems, Inc. under a design-build purchase order. It carries a posted capacity "
        "placard of 125 psf. Below it is the ESFR-protected pick-and-pack area and racking "
        "Zone 3."),
    report_findings=(
        "The failure initiated at a bolted beam-to-column connection at column line C-4. "
        "Several bolts were recovered from the debris in a sheared condition and others "
        "show elongated holes, indicating the connection was carrying load in bearing on "
        "an under-sized bolt group before it let go. Reconciling the warehouse management "
        "system's inventory record for the north bay against the collapsed area puts the "
        "loading between 180 and 230 psf against the posted 125 psf. Both conditions are "
        "present: the connection was inadequate for the design load, and the actual load "
        "materially exceeded the design load. The sealed drawings and connection "
        "calculations for the 2018 installation have been requested and are not yet "
        "produced."),
    report_quantum=(
        ("Mezzanine demolition, redesign and reconstruction", "USD 1,400,000"),
        ("Racking replacement and inspection", "USD 380,000"),
        ("ESFR sprinkler repair and system restoration", "USD 165,000"),
        ("Building repairs — girts, slab, baseplates, MEP", "USD 420,000"),
        ("Debris removal and shoring", "USD 350,000"),
        ("Two leased fork trucks", "USD 118,000"),
    ),
    report_comment_heading="On the defect exclusion and the absence of an overload exclusion",
    report_comment=(
        "The policy excludes loss arising from a defect or error in design or workmanship "
        "but writes back resulting physical damage from an insured peril. If the connection "
        "design or its installation was defective, the cost of making good the connection "
        "is one question and the resulting collapse damage to the racking, the stock and "
        "the building is another. Separately, this form carries no express overload "
        "exclusion for real property, unlike the contractors equipment forms in the same "
        "programme. Both points are recorded for the coverage file."),
    report_recommendation=(
        "Keep the remaining 3,900 sq ft unloaded and shored; this engineer will not "
        "release it. Preserve the sheared bolts, the connection plates and the C-4 column "
        "section, and notify Foothills Material Handling Systems before any testing."),
    schedule_slug="fairmount-mezzanine-loss-schedule",
    schedule_title="Loss schedule",
    schedule_headers=("Head", "Description", "Basis", "Total USD"),
    schedule_rows=(
        ("Structure", "Mezzanine demolition, redesign and rebuild", "Reinstatement", "1400000"),
        ("Racking", "14 bays replaced, 6 inspected", "Reinstatement", "380000"),
        ("Fire protection", "ESFR branch lines, 11 heads, restoration", "Reinstatement", "165000"),
        ("Building", "Girts, slab, baseplates, lighting, alarm, WMS", "Reinstatement", "420000"),
        ("Debris", "Removal and temporary shoring", "Invoice", "350000"),
        ("Plant", "Two leased fork trucks", "Replacement", "118000"),
        ("Stock", "610 rolls total loss", "Selling price", "2100000"),
        ("Stock", "1,180 rolls downgrade allowance", "Allowance", "700000"),
    ),
    schedule_total_label="Total property damage",
    email_subject="FNOL - Fairmount Textile Mills - mezzanine collapse, Building 1D - CP-2210-55870",
    email_to="firstnotice@alleghenycommercialrisk.example",
    email_cc="service@carolinabra.example",
    email_date="Thu, 15 May 2025 11:26:44 -0400",
    email_message_id="rk-mfg-2025-2210-b@ridgewaykessler.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a structural collapse at our client's "
        "Woodruff Road complex. Our reference is RK/MFG/2024/2210. This is reported within "
        "the 48-hour requirement for an occurrence reasonably expected to exceed half the "
        "deductible - it comfortably will."),
    email_facts=(
        ("Policy number", "CP-2210-55870"),
        ("Insured name", "Fairmount Textile Mills Holdings, Inc."),
        ("Claimant", "Fairmount Technical Fabrics, Inc."),
        ("Date of loss", "14 May 2025"),
        ("Loss location", "1180 Woodruff Industrial Road, Greenville, SC 29607"),
        ("Cause", "Collapse - bolted connection failure on a storage mezzanine"),
        ("Estimated loss", "USD 8,700,000"),
    ),
    email_narrative=(
        "A steel storage mezzanine in Building 1D gave way during a routine let-down of\n"
        "fabric rolls. About 1,740 sq ft of deck came down onto the racking and the\n"
        "pick-and-pack area below. Nobody was on it and nobody was struck.\n\n"
        "The collapse sheared two ESFR branch lines. The system was isolated at 15:52 and a\n"
        "fire watch has run continuously since 16:05; the city has been notified. We are\n"
        "giving you formal notice of the impairment now, within the 24 hours the warranty\n"
        "requires for an impairment exceeding eight hours.\n\n"
        "I should flag two coverage questions rather than let them arrive later: the defect\n"
        "exclusion and its write-back for resulting damage, and whether overloading beyond\n"
        "the posted placard is an excluded cause on this form. The engineer's report\n"
        "addresses both on the facts."),
    email_closing=(
        "Please confirm a claim reference and an adjuster today. We would also like your\n"
        "view on whether you or we should issue preservation correspondence to Foothills\n"
        "Material Handling Systems, who designed and installed the mezzanine in 2018."),
    expected="CP-2210-55870", confidence="Exact",
    why=("Policy number stated and corroborated by the insured, a different subsidiary as "
         "claimant, Building 1D on the location schedule, the broker reference and the "
         "wholesale broker's sender domain."),
    exercises=(
        "**A second, different subsidiary.** Pack 3 named Piedmont Dye & Finish; this names "
        "Fairmount Technical Fabrics. Both are additional named insureds on the same policy.",
        "**Two live coverage questions raised in the notice itself.** Neither is a matching "
        "signal; both belong in warnings.",
        "**Two claims on one policy in one term.** Together with pack 3 this exercises the "
        "duplicate-candidate path on genuinely distinct losses.",
    ),
))

# ---------------------------------------------------------------------------
# 16 — Kestrel Ridge, vehicle impact. AMBIGUOUS: transposed policy number.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="kestrel-ridge-vehicle-impact",
    title="Kestrel Ridge Retail Center — vehicle into the anchor storefront",
    line_of_business="Commercial property",
    broker="Harkness-Vaillancourt Insurance Agency",
    handler="Renata Sjoberg-Ellis", handler_role="Account Manager",
    handler_email="r.sjoberg-ellis@hvagency.example",
    handler_phone="+1 208 555 0466",
    broker_reference="HV/RET/2025/9083",
    reported_on="7 November 2025",
    insured="Kestrel Ridge Holdings, LP",
    policy_number="CP-9038-64115",
    policy_type="Commercial Property - Retail",
    policy_period="1 January 2025 to 1 January 2026",
    policy_limit="USD 22,000,000", policy_deductible="USD 10,000 vehicle impact",
    claimant="Kestrel Ridge Holdings, LP",
    claimant_contact="Grant Mikkelsen-Ruiz, Assistant Property Manager",
    claimant_email="g.mikkelsen-ruiz@aspenwallcre.example",
    date_of_loss="6 November 2025", time_of_loss="07:52 MST",
    loss_location="4840 North Eagle Ridge Boulevard, Boise, ID 83713 — Building 02",
    cause="Impact by vehicle — pickup through the anchor storefront",
    description=(
        "A 2016 Ford F-250 travelling through the parking field accelerated across four "
        "parking rows and struck the storefront of Building 02, the anchor space. The "
        "vehicle penetrated the glazing and the aluminium storefront framing and came to "
        "rest about eleven feet inside the sales floor. The driver, aged 78, was extracted "
        "by Boise Fire; the police report indicates a suspected medical event and records "
        "no evidence of impairment. A sprinkler head was struck and discharged before the "
        "fire department isolated the system."),
    affected_assets=(
        "34 linear feet of aluminium storefront framing, nine glazing panels and two "
        "automatic sliding door assemblies; a bearing storefront mullion and 26 feet of "
        "entry canopy, now shored; two bollards; entry vestibule finishes; one sprinkler "
        "head and fire alarm devices; sign band; two parking lot light standards"),
    injuries="2",
    business_interruption="Yes — the anchor tenant has been closed since the incident",
    structural_damage="Yes — a bearing mullion lost, entry canopy temporarily shored",
    environmental_exposure="No",
    authorities="Boise Police Department; Boise Fire Department",
    police_reference="2025-071144",
    incident_reference="ACR-2025-1106",
    asset_heading="Building and elements affected",
    asset_rows=(
        ("Building", "Building 02, anchor space Suite 200, 31,200 sq ft"),
        ("Tenant in occupation", "Wilder & Pine Home Goods, lease to 2029"),
        ("Storefront", "34 lin ft framing, nine glazing panels, two sliding doors"),
        ("Structural", "One bearing mullion, 26 ft entry canopy shored"),
        ("Fire protection", "One sprinkler head discharged, 900 sq ft water"),
    ),
    estimated_loss="408,500", repair_estimate="372,000",
    repair_estimate_label="Landlord reinstatement",
    report_slug="kestrel-ridge-structural-assessment",
    report_title="Structural assessment",
    report_firm="Sawtooth Structural Engineering",
    report_reference="SSE-2025-1107",
    report_author="Marielle Okonjo-Sandberg, PE, SE",
    report_instructions=(
        ("Instructed by", "Aspenwall Commercial Realty, LLC"),
        ("Instructed on", "6 November 2025"),
        ("Site attendance", "6 November 2025, 10:30"),
        ("Scope", "Whether the entry canopy remains supported and safe for public use"),
    ),
    report_circumstances=(
        "Building 02 is a single-storey masonry non-combustible anchor unit built in 2008 "
        "with an aluminium storefront system and a projecting entry canopy over the "
        "pedestrian approach. The canopy is carried at its outer edge by the storefront "
        "mullions, one of which was destroyed by the impact."),
    report_findings=(
        "The mullion at grid line 3 has been removed by the impact over its full height. "
        "It is a bearing element for the canopy above and not, as the storefront supplier's "
        "shop drawings suggest, a purely infill member. With it gone the canopy is "
        "cantilevered from a connection detailed for a propped condition and is not safe "
        "for public use. Temporary shoring was designed and installed on the day of loss. "
        "Deflection measured at the canopy leading edge before shoring was 21 mm against "
        "a serviceability limit of 8 mm."),
    report_quantum=(
        ("Structural repair — mullion, canopy connection, engineering", "USD 118,000"),
        ("Storefront framing, glazing and automatic doors", "USD 96,000"),
        ("Interior vestibule finishes and lighting", "USD 62,000"),
        ("Bollards, striping, wheel stops, light standards, landscaping", "USD 34,000"),
        ("Sprinkler head, fire alarm devices, restoration", "USD 19,000"),
        ("Water damage repair to the sales floor", "USD 27,000"),
        ("Sign band and substrate", "USD 16,000"),
    ),
    report_comment_heading="On the shoring authorisation",
    report_comment=(
        "Emergency shoring cost USD 21,400 against a policy pre-authorisation of USD "
        "15,000. This engineer would not accept an unshored cantilevered canopy over a "
        "public entrance overnight, and the alternative was closing the pedestrian "
        "approach entirely. Retrospective approval is sought on that basis."),
    report_recommendation=(
        "Maintain the shoring until the replacement mullion is installed and the canopy "
        "connection is reinstated to the propped condition it was detailed for."),
    schedule_slug="kestrel-ridge-vehicle-impact-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Element", "Description", "Quantity", "Total USD"),
    schedule_rows=(
        ("Storefront", "Aluminium framing and glazing panels", "34", "68000"),
        ("Storefront", "Automatic sliding door assemblies", "2", "28000"),
        ("Structural", "Bearing mullion and canopy connection repair", "1", "96000"),
        ("Structural", "Engineering, inspection and certification", "1", "22000"),
        ("Site", "Bollards, striping, wheel stops, light standards", "1", "34000"),
        ("Interior", "Vestibule floor, ceiling, lighting, walls", "1", "62000"),
        ("Fire", "Sprinkler head, pull station, two horn/strobes", "1", "19000"),
        ("Interior", "Water damage to sales floor, 900 sq ft", "900", "27000"),
        ("Signage", "Sign band and fascia substrate", "60", "16000"),
    ),
    schedule_total_label="Total landlord reinstatement",
    email_subject="New loss - Kestrel Ridge Retail Center, Boise ID - vehicle into the anchor storefront",
    email_to="claims@northshorespecialty.example",
    email_cc="g.mikkelsen-ruiz@aspenwallcre.example",
    email_date="Fri, 07 Nov 2025 15:47:19 -0700",
    email_message_id="hv-ret-2025-9083-b@hvagency.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify an impact by vehicle at our client's "
        "Kestrel Ridge Retail Center. Our reference is HV/RET/2025/9083.\n\n"
        "A caution on the policy number quoted below. It has been taken from the property "
        "manager's certificate binder and it does not resolve on our own system either. "
        "Please search on the property address and our reference - we think a digit is out "
        "of order and we are checking the schedule."),
    email_facts=(
        ("Policy number as recorded in the binder", "CP-9038-64115"),
        ("Broker reference", "HV/RET/2025/9083"),
        ("Insured name", "Kestrel Ridge Holdings, LP"),
        ("Managing agent", "Aspenwall Commercial Realty, LLC"),
        ("Date of loss", "6 November 2025"),
        ("Loss location", "4840 North Eagle Ridge Boulevard, Boise, ID 83713"),
        ("Cause", "Impact by vehicle - pickup through the anchor storefront"),
        ("Estimated loss", "USD 408,500"),
    ),
    email_narrative=(
        "A pickup accelerated across four parking rows on Thursday morning and went\n"
        "through the anchor storefront, coming to rest about eleven feet inside the sales\n"
        "floor. The driver is 78 and the police report suggests a medical event; there is\n"
        "no suggestion of impairment and no citation as at the report date.\n\n"
        "The impact took out a storefront mullion that our engineer says is a bearing\n"
        "element for the entry canopy. The canopy is shored and the entrance is re-routed.\n"
        "A tenant employee was cut by flying glass and treated and released.\n\n"
        "The anchor tenant is closed and is compiling their own claim; their improvements\n"
        "were installed at their expense under the lease so we have pointed them at their\n"
        "own carrier. The driver's insurer is Ridgeline Mutual and we assume you will pursue."),
    email_closing=(
        "Please confirm a claim reference and an adjuster, and confirm that the reduced\n"
        "vehicle impact deductible applies rather than the all other perils figure. We also\n"
        "need retrospective approval for shoring at USD 21,400 against the USD 15,000\n"
        "allowance."),
    expected="CP-9083-64115", confidence="Possible",
    why=("The notice states `CP-9038-64115`, which **does not exist** — it is a digit "
         "transposition of `CP-9083-64115`, and the broker flags the doubt in the email. Exact "
         "lookup returns nothing. The OCR-folded and edit-distance rungs of the `policy_number` "
         "comparator should reach it, and if they do not, the broker reference, the address "
         "matching scheduled Location 002, the insured, the managing agent and the sender "
         "domain all resolve it."),
    exercises=(
        "**A policy number that is wrong by one transposition.** This is what the "
        "`policy_number` comparator's edit-distance rung exists for. A matcher that only does "
        "exact lookup returns NO_MATCH on a policy that is plainly in the book.",
        "**The notice admits the doubt.** A good answer uses that rather than trusting the "
        "stated string.",
        "**Matching to Building 02 rather than the centre.** The address given is the anchor's "
        "own street number, which is scheduled Location 002, not the primary location.",
    ),
))

# ---------------------------------------------------------------------------
# 17 — Rivergate, copper theft. AMBIGUOUS: no number, BR vs GL, same insured.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="rivergate-copper-theft",
    title="Rivergate Commons Phase II — copper theft from the works",
    line_of_business="Construction — builder's risk",
    broker="Ashcombe Vail Commercial Insurance Services",
    handler="Gerald Nwosu-Fitzgerald", handler_role="Construction Claims Handler",
    handler_email="g.nwosu-fitzgerald@ashcombevail.example",
    handler_phone="+1 704 555 0731",
    broker_reference="AV/BR/2025/3358",
    reported_on="19 January 2026",
    insured="Rivergate Development Partners, LLC",
    policy_number="",
    policy_type="Not stated — the contractor holds a builder's risk and a liability policy",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Vanterra Construction Group, LLC",
    claimant_contact="Callum Baptiste-Ngo, Project Superintendent",
    claimant_email="c.baptiste-ngo@vanterracg.example",
    date_of_loss="17 January 2026", time_of_loss="overnight, discovered 18 January 07:40",
    loss_location="2650 Rivergate Parkway, Charlotte, NC 28273",
    cause="Theft — copper stripped from the partially completed works",
    description=(
        "Perimeter fence fabric was cut on the Rivergate Parkway frontage over a weekend "
        "and two site cameras on the north-west corner were disabled. Copper branch "
        "circuit wire was cut out of conduit box to box across Levels 2 and 3, and "
        "unpulled reels, copper water tube, plumbing trim and eight panelboards were "
        "taken. Extensive collateral damage was done reaching concealed runs: conduit "
        "crushed and pulled, about 140 boxes damaged and drywall cut open in some sixty "
        "locations."),
    affected_assets=(
        "3,100 ft of copper branch circuit wire; 22 reels of unpulled THHN; 900 ft of "
        "type L copper tube; 46 plumbing fixtures and trim sets; eight panelboards; "
        "conduit and 140 boxes on Levels 2 and 3; drywall in 60 locations; three doors "
        "and frames; perimeter fence and two cameras"),
    business_interruption="No",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Charlotte-Mecklenburg Police Department",
    police_reference="CMPD-2026-0021884",
    incident_reference="RGC2-IR-2026-004",
    asset_heading="Project and works affected",
    asset_rows=(
        ("Project", "Rivergate Commons Phase II, 214 units over a two-level podium"),
        ("Project status", "Level 5 framing, MEP rough-in on Levels 2 and 3, no CO issued"),
        ("Stolen — permanent works", "Copper wire, tube, trim, eight panelboards"),
        ("Damaged — permanent works", "Conduit, 140 boxes, drywall, three doors"),
        ("Temporary works", "Perimeter fence and two site cameras"),
    ),
    estimated_loss="394,400", repair_estimate="394,400",
    repair_estimate_label="Replacement and rework",
    report_slug="rivergate-site-security-report",
    report_title="Site security and loss report",
    report_firm="Queen City Site Security",
    report_reference="QCSS-2026-0118",
    report_author="Delphine Amankwah-Ritter, Operations Supervisor",
    report_instructions=(
        ("Prepared for", "Vanterra Construction Group, LLC"),
        ("Prepared on", "19 January 2026"),
        ("Discovery", "18 January 2026, 07:40, weekend patrol"),
        ("Scope", "Discovery, site protection arrangements and preserved evidence"),
    ),
    report_circumstances=(
        "The site was secured and vacated on Friday 16 January at 16:00. Two gates, six "
        "foot chain link, locked outside working hours. Eleven cameras with a recording "
        "DVR, not centrally monitored. A nightly drive-through patrol is contracted to "
        "this company under a written agreement."),
    report_findings=(
        "Fence fabric was cut on the Rivergate Parkway frontage rather than at either "
        "gate. Two cameras covering the north-west corner were disabled by cutting their "
        "cables; the remaining nine recorded throughout and the footage has been exported "
        "and provided to Charlotte-Mecklenburg Police. The patrol log for 16 to 18 January "
        "is attached to this report. High-value material was partly secured: the "
        "panelboards and the plumbing trim were in a lockable Level 2 storage room and a "
        "Level 3 closet, but the wire reels were in the open on Level 2 and the copper "
        "tube was banded on the Level 3 deck. Nothing has been repaired or cleared; cut "
        "wire ends, damaged boxes, crushed conduit and the cut fence fabric are all in place."),
    report_quantum=(
        ("Copper branch circuit wire, 3,100 ft", "USD 74,000"),
        ("Unpulled THHN reels, 22 off", "USD 58,000"),
        ("Copper water tube, 900 ft", "USD 41,000"),
        ("Plumbing fixtures and trim, 46 sets", "USD 33,000"),
        ("Panelboards, eight off", "USD 47,000"),
        ("Conduit, boxes, drywall, doors and rework", "USD 133,400"),
    ),
    report_comment_heading="On the site protection requirements",
    report_comment=(
        "The project's builder's risk policy requires fencing with lockable gates, site "
        "lighting, and either monitored cameras or a logged nightly patrol. Both a "
        "recording camera system and a contracted logged patrol were in place, so the "
        "requirement appears satisfied on its face. The patrol log and the camera "
        "retention certificate are provided so that can be verified rather than assumed."),
    report_recommendation=(
        "Move all remaining copper and panelboards into a locked container inside the "
        "fence line, add a second overnight patrol and post a static guard until the "
        "MEP rough-in is closed up."),
    schedule_slug="rivergate-theft-schedule",
    schedule_title="Schedule of theft and damage",
    schedule_headers=("Category", "Item", "Quantity", "Total USD"),
    schedule_rows=(
        ("Stolen", "Copper branch circuit wire cut from conduit", "3100", "74000"),
        ("Stolen", "THHN copper wire reels, unpulled", "22", "58000"),
        ("Stolen", "Type L copper water tube", "900", "41000"),
        ("Stolen", "Plumbing fixtures and trim sets", "46", "33000"),
        ("Stolen", "Electrical panelboards", "8", "47000"),
        ("Damage", "Conduit and boxes, Levels 2 and 3", "140", "88000"),
        ("Damage", "Drywall opened to reach concealed runs", "60", "36000"),
        ("Damage", "Doors and frames, closets and store", "3", "9400"),
        ("Damage", "Perimeter fence fabric and gate chain", "1", "4200"),
        ("Damage", "Site cameras", "2", "3800"),
    ),
    schedule_total_label="Total theft and damage",
    email_subject="New loss - Rivergate Commons Phase II, Charlotte NC - copper theft from site",
    email_to="constructionclaims@pinnacleinland.example",
    email_cc="c.baptiste-ngo@vanterracg.example",
    email_date="Mon, 19 Jan 2026 09:31:55 -0500",
    email_message_id="av-br-2026-3358-b@ashcombevail.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a theft from the Rivergate Commons "
        "Phase II works. Our reference is AV/BR/2025/3358.\n\n"
        "The superintendent submitted this through your portal on Sunday evening and left "
        "the policy number blank. He told us on the callback that the job has a builder's "
        "risk and that his own company has a liability policy through us as well, and he "
        "did not know which one a theft goes on. It is the builder's risk - see below."),
    email_facts=(
        ("Broker reference", "AV/BR/2025/3358"),
        ("Project name", "Rivergate Commons Phase II"),
        ("Contract number", "AIA-A102-2025-RGC2"),
        ("Insured name", "Rivergate Development Partners, LLC"),
        ("Contractor", "Vanterra Construction Group, LLC"),
        ("Date of loss", "17 January 2026"),
        ("Loss location", "2650 Rivergate Parkway, Charlotte, NC 28273"),
        ("Estimated loss", "USD 394,400"),
    ),
    email_narrative=(
        "They cut the fence on the Parkway frontage over the weekend, killed two cameras\n"
        "and went box to box on two floors taking copper out of the conduit. They also\n"
        "took unpulled reels, copper tube, plumbing trim and eight panelboards.\n\n"
        "Everything taken is material staged for installation into the building, and the\n"
        "collateral damage is to the partially completed works themselves. That is\n"
        "first-party loss to insured project property. There is no third-party injury and\n"
        "no damage to anyone else's property, so the contractor's liability policy does not\n"
        "respond - and in any event its other insurance condition makes it excess over any\n"
        "builder's risk covering the same loss.\n\n"
        "The electrical subcontractor is missing two cordless kits from their own gang box.\n"
        "That is their property and their cover, and it is not part of this claim."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. We would also like your position\n"
        "early on the copper theft sublimit, which we expect to be the binding constraint."),
    expected="BR-3358-20471", confidence="Possible",
    why=("No policy number, and the reporter's own company is a named insured on the builder's "
         "risk *and* the named insured on a liability policy placed through the same broker — "
         "with this very project scheduled as designated project P-1 on the liability policy. "
         "The discriminator is loss type: every item is first-party damage to or theft of "
         "insured project property, and there is no third-party element at all."),
    exercises=(
        "**One project on two policies.** Rivergate Commons Phase II appears as the project on "
        "`BR-3358-20471` and as designated project P-1 on `GL-5529-41836`, both through broker "
        "Ashcombe Vail. `project_name` and `risk_location` fire for both candidates.",
        "**The broker reference as the tiebreak.** `AV/BR/…` versus `AV/GL/…` is the cleanest "
        "signal separating them, which is exactly why the reference carries 2.5.",
        "**Property that belongs to neither insured.** The subcontractor's tool kits are named "
        "and excluded from the claim in the notice.",
    ),
))

# ---------------------------------------------------------------------------
# 18 — Northfield, site vandalism. NO number. Police report as specialist doc.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="northfield-site-vandalism",
    title="Northfield Senior Living — overnight vandalism and theft on site",
    line_of_business="Construction — builder's risk",
    broker="Kettleridge Risk Partners, LLC",
    handler="Marcus Adeyemi-Croft", handler_role="Producer",
    handler_email="m.adeyemi-croft@kettleridgerisk.example",
    handler_phone="+1 918 555 0347",
    broker_reference="KRP/CAR/2025/6612",
    reported_on="13 January 2026",
    insured="Sundale Property Group, LLC",
    policy_number="",
    policy_type="Builder's Risk - Completed Value",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Halcyon Builders, LLC",
    claimant_contact="Owen Kastellec-Brandt, Project Executive",
    claimant_email="o.kastellec-brandt@halcyonbuilders.example",
    date_of_loss="3 January 2026", time_of_loss="between 22:00 and 05:30",
    loss_location="1725 Northfield Commons Drive, Madison, WI 53704",
    cause="Malicious damage and theft",
    description=(
        "Unknown persons cut chain link fence fabric adjacent to the north-east gate of an "
        "active construction site and caused extensive damage across two buildings under "
        "construction and the existing structure under renovation. The offence was part "
        "theft and part vandalism: copper piping, wire, panelboard bus and four HVAC "
        "condensing units were removed, and significant damage was done to items of no "
        "resale value including drywall, doors, sprinkler heads and spray-painted walls."),
    affected_assets=(
        "Building A: 40 sheets of installed drywall, six door assemblies, elevator "
        "hoistway door frames, 200 ft of copper tube, three panelboards, two sprinkler "
        "heads, extensive graffiti. Building B: 300 ft of copper wire, ten condensing "
        "units of which four removed. Existing structure: entry glazing, temporary panel, "
        "60 ft of copper. Site: two job trailers, a conex, four light towers, 120 ft of "
        "temporary power cord, two cameras"),
    business_interruption="No",
    structural_damage="No",
    environmental_exposure="No",
    authorities="City of Madison Police Department, Detective Bureau assigned",
    police_reference="MPD-2026-0014477",
    incident_reference="HAL-NSL-2026-0004",
    asset_heading="Buildings affected",
    asset_rows=(
        ("Building A", "Four storeys over podium, at drywall stage — worst affected"),
        ("Building B", "Two storeys, at rough-in — copper and condensing units"),
        ("Building C", "Commons, at framing — no damage observed"),
        ("Existing structure", "Former clinic under renovation to a therapy wing"),
        ("Site", "Trailers, conex, light towers, temporary power, cameras"),
    ),
    estimated_loss="380,000", repair_estimate="380,000",
    repair_estimate_label="Replacement and rework",
    report_slug="northfield-police-incident-report",
    report_title="Police incident report",
    report_firm="City of Madison Police Department, North District",
    report_reference="MPD-2026-0014477",
    report_author="Officer M. Sundqvist-Abara, Badge 3318",
    report_instructions=(
        ("Report type", "Burglary, criminal damage to property, theft"),
        ("Offence window", "3 January 2026 22:00 to 4 January 2026 05:30"),
        ("Reported", "4 January 2026, 07:12"),
        ("Certified copy issued", "12 January 2026, at the request of the property representative"),
    ),
    report_circumstances=(
        "An active construction site with no street number posted at the time of the "
        "offence. The site was identified by the reporting party as the Northfield Senior "
        "Living construction project. Three buildings are under construction and a fourth "
        "existing single-storey structure is undergoing renovation on the same parcel. "
        "The reporting party identified the owner as Sundale Property Group of Tulsa and a "
        "second entity as Northfield Senior Living Partners, and stated that the site is "
        "fully fenced and that a nightly security patrol is contracted."),
    report_findings=(
        "A section of chain link fence fabric approximately eight feet wide was cut "
        "vertically and folded back adjacent to the north-east gate. Tyre impressions in "
        "frozen ground inside the fence line were photographed and cast where possible. "
        "Damage was documented across Buildings A and B and the existing structure. Two of "
        "six site cameras were spray-painted over; the reporting party confirmed on 5 "
        "January that footage from the remaining four had been exported. The security "
        "patrol log records a drive-through at 21:50 on 3 January and the next at 05:30 on "
        "4 January, at which point the damage was discovered. Latent prints were lifted "
        "from the pried electrical panel doors and the conex locking bar, and a pair of "
        "bolt cutters was recovered in tall grass outside the cut fence section."),
    report_quantum=(
        ("Reporting party's preliminary estimate, range", "USD 340,000 to 420,000"),
        ("Building A — drywall, doors, graffiti, hoistway frames", "USD 148,000"),
        ("Building A — copper, panelboards, sprinkler heads", "USD 96,000"),
        ("Building B — copper wire and condensing units", "USD 84,000"),
        ("Existing structure and site works", "USD 52,000"),
    ),
    report_comment_heading="Valuation and evidence",
    report_comment=(
        "The figures above are the reporting party's estimate and are recorded as such; no "
        "independent valuation has been performed by this department. Latent prints, the "
        "tyre casts and the recovered bolt cutters have been submitted for processing and "
        "the case remains open with the Detective Bureau."),
    report_recommendation=(
        "The reporting party was advised of the case number for insurance purposes and "
        "provided with a copy of this report on request."),
    schedule_slug="northfield-vandalism-schedule",
    schedule_title="Schedule of damage and theft",
    schedule_headers=("Building", "Item", "Quantity", "Total USD"),
    schedule_rows=(
        ("A", "Installed drywall punched, kicked or cut through", "40", "22000"),
        ("A", "Interior door assemblies destroyed", "6", "18000"),
        ("A", "Graffiti removal and refinishing, ground floor", "1", "34000"),
        ("A", "Elevator hoistway door frames, floors 1 and 2", "2", "74000"),
        ("A", "Copper tube removed from the mechanical room", "200", "18000"),
        ("A", "Panelboards pried, bus and breakers removed", "3", "68000"),
        ("A", "Sprinkler heads broken off", "2", "10000"),
        ("B", "Copper wire cut from conduit at outlet boxes", "300", "22000"),
        ("B", "HVAC condensing units removed", "4", "38000"),
        ("B", "HVAC condensing units, coils and line sets cut out", "6", "24000"),
        ("B", "Exterior door assemblies damaged", "2", "6000"),
        ("Existing", "Entry glazing, temporary panel, copper, graffiti", "1", "22000"),
        ("Site", "Trailers, conex, light towers, temporary power, cameras", "1", "24000"),
    ),
    schedule_total_label="Total damage and theft",
    email_subject="New loss - Northfield Senior Living, Madison WI - overnight vandalism and theft on site",
    email_to="buildersrisk.claims@cascadiamutual.example",
    email_cc="o.kastellec-brandt@halcyonbuilders.example",
    email_date="Tue, 13 Jan 2026 10:08:22 -0600",
    email_message_id="krp-car-2026-6612-b@kettleridgerisk.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify malicious damage and theft on the "
        "Northfield Senior Living site. Our reference is KRP/CAR/2025/6612. The certified "
        "police report has only just been released, which is why the formal notice follows "
        "the telephone report of 4 January."),
    email_facts=(
        ("Broker reference", "KRP/CAR/2025/6612"),
        ("Project name", "Northfield Senior Living"),
        ("Contract number", "AIA-A133-2024-NSL"),
        ("Insured name", "Sundale Property Group, LLC"),
        ("General contractor", "Halcyon Builders, LLC"),
        ("Date of loss", "3 January 2026"),
        ("Loss location", "1725 Northfield Commons Drive, Madison, WI 53704"),
        ("Estimated loss", "USD 380,000"),
    ),
    email_narrative=(
        "They cut the fence by the north-east gate overnight and went through two of the\n"
        "three buildings under construction and the existing clinic building that is being\n"
        "converted to the therapy wing.\n\n"
        "Part of it is straightforward copper theft - pipe, wire, panelboard bus, four\n"
        "condensing units taken and six more with their coils cut out. The rest is\n"
        "vandalism for its own sake: drywall punched through, doors destroyed, sprinkler\n"
        "heads knocked off, graffiti across the ground floor.\n\n"
        "Madison PD attended, lifted prints and recovered a pair of bolt cutters outside\n"
        "the cut section. Note the existing structure is damaged too, which engages the\n"
        "existing structure section rather than the works section."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. The patrol log shows a gap\n"
        "between 21:50 and 05:30 which you will want to look at against the site security\n"
        "requirement, and we would rather raise it than have it found."),
    expected="BR-6612-77309", confidence="Strong",
    why=("No policy number. The project name and contract number, the site address, both owner "
         "entities, the general contractor and the broker reference all match. The building "
         "configuration described in the police report — three under construction plus an "
         "existing single-storey clinic being converted — matches the policy's project "
         "description, and only this policy carries an existing structure section."),
    exercises=(
        "**A specialist document that identifies the insured only loosely.** The police report "
        "says 'Sundale Property Group of Tulsa' and never states an entity suffix or a policy.",
        "**Existing structure damage as a corroborating signal.** The renovation element is "
        "unique to this policy in the book.",
        "**A warranty gap disclosed by the reporter.** The patrol interval is raised in the "
        "email; it is a warning, not a rank.",
    ),
))

# ---------------------------------------------------------------------------
# 19 — Cypress Landing, haboob and microburst. NO number. Second loss, same policy.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="cypress-landing-haboob",
    title="Cypress Landing Data Hall B — haboob and wet microburst",
    line_of_business="Construction — builder's risk",
    broker="Sonoran Ridge Risk Advisors, LLC",
    handler="Desmond Achterberg", handler_role="Producer",
    handler_email="d.achterberg@sonoranridgerisk.example",
    handler_phone="+1 602 555 0510",
    broker_reference="SRR/BR/2025/1147",
    reported_on="10 July 2026",
    insured="Meridian Grid Holdings, LLC",
    policy_number="",
    policy_type="Builder's Risk / Installation",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Ironbark Constructors, Inc.",
    claimant_contact="Hector Zaldivar-Boone, Executive Vice President",
    claimant_email="h.zaldivar-boone@ironbarkconstructors.example",
    date_of_loss="7 July 2026", time_of_loss="18:34 MST",
    loss_location="14900 South Cypress Landing Way, Mesa, AZ 85212",
    cause="Windstorm — haboob followed by a wet microburst",
    description=(
        "A haboob followed by a wet microburst crossed the site on the evening of 7 July. "
        "The site anemometer logged a peak gust of 78 mph at 18:47 and the nearby airport "
        "recorded 0.94 inches of rain in forty minutes. Two bays of partially braced "
        "erected steel racked out of plumb, about 4,400 sq ft of installed wall panel was "
        "torn off and 9,200 sq ft of roof membrane lifted and tore. With the envelope open "
        "above and to the west, two medium-voltage switchgear line-ups staged on their "
        "pads inside the north bay were exposed to blowing dust and then driving rain for "
        "roughly ninety minutes."),
    affected_assets=(
        "Two bays of erected structural steel racked out of plumb, three purlins and two "
        "bracing members bent; 4,400 sq ft of insulated metal panel; 9,200 sq ft of TPO "
        "membrane with 6,000 sq ft of seam separation; two MV switchgear line-ups; two "
        "generator enclosures; 180 ft of chilled water pipe and its rack; staged cable "
        "tray, busway and raised floor under a collapsed canopy; temporary works"),
    business_interruption="No — delay in start-up not purchased on this programme",
    structural_damage="Yes — two bays of steel racked, shored and tagged out",
    environmental_exposure="No",
    authorities="None",
    incident_reference="IBC-WX-2026-0022",
    asset_heading="Works and equipment affected",
    asset_rows=(
        ("Project", "Cypress Landing Data Hall B, campus building 2 of 4"),
        ("Structure", "North bay at 70% erection, two bays racked 2.8 in and 3.4 in"),
        ("Envelope", "4,400 sq ft IMP torn off, 9,200 sq ft TPO lifted and torn"),
        ("MV-SWGR-1", "Dust ingress at three cubicle joints, moisture in two cubicles"),
        ("MV-SWGR-2", "End enclosure deformed by a struck panel, standing water inside"),
    ),
    estimated_loss="5,900,000", repair_estimate="2,905,000",
    repair_estimate_label="Works reinstatement excluding switchgear",
    report_slug="cypress-landing-electrical-assessment",
    report_title="Electrical equipment assessment",
    report_firm="Saguaro Power Systems Consulting",
    report_reference="SPSC-2026-0709",
    report_author="Ottilie Brandvold-Nkemelu, PE",
    report_instructions=(
        ("Instructed by", "Desert Basin Specialty Insurance Company"),
        ("Instructed on", "9 July 2026"),
        ("Site attendance", "10 July 2026"),
        ("Scope", "Condition of the two MV switchgear line-ups and the settlement basis"),
    ),
    report_circumstances=(
        "Two of the four medium-voltage switchgear line-ups had been set on their "
        "housekeeping pads inside the north bay. Both were sealed with strip heaters "
        "energised and desiccant installed, which is what the policy's site protection "
        "warranty requires for electrical equipment delivered to site. Neither had entered "
        "testing, energisation or commissioning; both were in storage on the pad."),
    report_findings=(
        "MV-SWGR-1 shows dust ingress at three cubicle joints and free moisture in two "
        "cubicles. The strip heaters remained energised throughout, which is confirmed "
        "from the temporary power log, and insulation resistance readings taken on 10 July "
        "are within specification. This line-up is recoverable by a field clean, dry, test "
        "and re-certification. MV-SWGR-2 is materially worse: a displaced wall panel struck "
        "the west end, deforming the end cubicle enclosure and breaching the seal. There is "
        "standing water in two cubicles and visible dust deposition on the bus insulators. "
        "Enclosure deformation of this order cannot be corrected in the field without "
        "compromising the arc-resistant rating. Neither line-up has been energised, opened "
        "for cleaning or moved, in accordance with the policy condition."),
    report_quantum=(
        ("MV-SWGR-1 field clean, dry, test and re-certify", "USD 340,000"),
        ("MV-SWGR-2 return to factory and re-manufacture", "USD 2,900,000"),
        ("Structural steel re-plumb, re-bolt and member replacement", "USD 680,000"),
        ("IMP wall panel replacement and assessment", "USD 540,000"),
        ("Roof membrane, cover board, insulation and fasteners", "USD 820,000"),
        ("Chilled water pipe, rack and staged materials", "USD 455,000"),
    ),
    report_comment_heading="On which deductible applies",
    report_comment=(
        "The policy carries a separate and materially higher deductible for electrical "
        "equipment damaged during testing, energisation or commissioning. Neither line-up "
        "had entered any of those states; both were in storage on the pad and were damaged "
        "by a windstorm that removed the envelope above them. In this consultant's opinion "
        "the windstorm deductible applies to the switchgear as it does to the rest of the "
        "works. The insured raised this proactively and this report agrees with them."),
    report_recommendation=(
        "Do not attempt a field repair on MV-SWGR-2. Keep both line-ups under weather "
        "protection with strip heaters energised and hourly humidity logging until removed."),
    schedule_slug="cypress-landing-storm-damage-schedule",
    schedule_title="Storm damage schedule",
    schedule_headers=("Element", "Description", "Quantity", "Total USD"),
    schedule_rows=(
        ("Structure", "Steel re-plumb, re-bolt, member replacement", "2", "680000"),
        ("Envelope", "IMP wall panel replacement", "4400", "540000"),
        ("Envelope", "TPO membrane, cover board, insulation, fasteners", "9200", "820000"),
        ("Interior", "Water damage, slab and raised floor pedestals", "1", "210000"),
        ("Mechanical", "Chilled water spools and pipe rack", "180", "165000"),
        ("Materials", "Staged tray, busway and raised floor under canopy", "1", "290000"),
        ("Temporary", "Fence, canopy, light towers, trailer skirting", "1", "140000"),
        ("Mitigation", "Weather protection, dehumidification, shoring", "1", "260000"),
        ("Electrical", "MV-SWGR-1 field clean, dry, test, re-certify", "1", "340000"),
        ("Electrical", "MV-SWGR-2 factory re-manufacture", "1", "2900000"),
    ),
    schedule_total_label="Total works damage",
    email_subject="New loss - Cypress Landing Data Hall B, Mesa AZ - haboob and microburst damage",
    email_to="report@desertbasinspecialty.example",
    email_cc="a.petrosyan-ward@ironbarkconstructors.example",
    email_date="Fri, 10 Jul 2026 14:22:08 -0700",
    email_message_id="srr-br-2026-1147-b@sonoranridgerisk.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify windstorm damage to the works at "
        "Cypress Landing Data Hall B. Our reference is SRR/BR/2025/1147."),
    email_facts=(
        ("Broker reference", "SRR/BR/2025/1147"),
        ("Project name", "Cypress Landing Data Hall B"),
        ("Contract number", "CONSENSUSDOCS-410-2025-CLB"),
        ("Insured name", "Meridian Grid Holdings, LLC"),
        ("Contractor", "Ironbark Constructors, Inc."),
        ("Date of loss", "7 July 2026"),
        ("Loss location", "14900 South Cypress Landing Way, Mesa, AZ 85212"),
        ("Estimated loss", "USD 5,900,000"),
    ),
    email_narrative=(
        "A haboob and then a wet microburst crossed the site on Tuesday evening. The site\n"
        "anemometer logged 78 mph. Two bays of steel racked, 4,400 sq ft of wall panel came\n"
        "off and 9,200 sq ft of roof membrane lifted.\n\n"
        "The serious element is that with the envelope open, two medium-voltage switchgear\n"
        "line-ups sitting on their pads took blowing dust and then driving rain for about\n"
        "ninety minutes. One is probably recoverable in the field; the other was struck by\n"
        "a flying panel and looks like a factory re-manufacture with a 34 to 40 week lead.\n\n"
        "Neither has been energised, opened or moved. Site evacuated at 18:26 and nobody\n"
        "was hurt - 47 people were on site. This programme has no delay in start-up cover,\n"
        "so any schedule consequence is a contractual matter between our client and the\n"
        "contractor and is not tendered."),
    email_closing=(
        "Please confirm a claim reference and appoint an equipment expert quickly - the\n"
        "switchgear is the whole exposure and it is sitting under a temporary cover."),
    expected="BR-1147-30926", confidence="Strong",
    why=("No policy number. The project name and contract number, the site address, both named "
         "insureds, the broker reference and the sender domain all match, and the loss is "
         "first-party damage to the works and to owner-supplied equipment scheduled on this policy."),
    exercises=(
        "**A second loss on the policy that pack 7 mis-referenced.** Here the same project is "
        "notified with no policy number at all, and must still reach `BR-1147-30926`.",
        "**Equipment damage that is not equipment-floater damage.** The switchgear is "
        "owner-supplied permanent works, scheduled on the builder's risk, not on the "
        "contractor's floater.",
        "**Deductible selection as a warning.** Which deductible applies turns on whether the "
        "equipment had entered commissioning; it changes the net, not the rank.",
    ),
))

# ---------------------------------------------------------------------------
# 20 — Tidewater, rooftop equipment. NO number. Third-party tenant property.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="tidewater-rooftop-equipment",
    title="Hampton Roads Logistics Center — crane load into a tenant's rooftop plant",
    line_of_business="Liability — commercial general liability",
    broker="Coastline Brantley Insurance Advisors",
    handler="Sylvia Okonjo-Pratt", handler_role="Producer",
    handler_email="s.okonjo-pratt@coastlinebrantley.example",
    handler_phone="+1 757 555 0271",
    broker_reference="CB/ROOF/2024/3376",
    reported_on="27 August 2025",
    insured="Tidewater Roofing & Exteriors, LLC",
    policy_number="",
    policy_type="Commercial General Liability - Occurrence",
    policy_period="Renewed November 2024 — certificates with the insured",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Chandler Vale Cold Chain Solutions",
    claimant_contact="Ignatius Bellweather-Cho, Project Manager (insured)",
    claimant_email="i.bellweather-cho@tidewaterroofing.example",
    date_of_loss="26 August 2025", time_of_loss="11:15 EDT",
    loss_location="1900 Bainbridge Logistics Way, Chesapeake, VA 23320",
    cause="Impact — crane load swung into a tenant's rooftop condensing units",
    description=(
        "A bundle of insulation board and TPO membrane rolls was being flown to Roof "
        "Section 7 by a 90-ton hydraulic crane set up in the truck court. On the way over "
        "the load contacted the guy wires and then the housing of a tenant's rooftop "
        "condensing unit bank. Nothing fell; the load swung into the equipment and dragged "
        "across it. The tenant's process refrigeration serving a 22,000 sq ft "
        "temperature-controlled fulfilment area was taken out."),
    affected_assets=(
        "Four rooftop condensing units belonging to the tenant — two with crushed coils "
        "and bent fan shrouds, one with a fractured refrigerant line and full charge lost, "
        "one displaced off its curb; approximately 140 lb of R-448A vented; 30 ft of "
        "rooftop refrigerant piping and insulation; two condenser disconnects and whips; "
        "20 ft of aluminium rooftop walkway"),
    business_interruption="Yes — tenant's temperature-controlled area on temporary cooling, product at risk",
    structural_damage="No",
    environmental_exposure="Yes — approximately 140 lb of R-448A refrigerant vented",
    authorities="None",
    incident_reference="TWR-HRLC-2025-0826",
    potential_litigation="Yes — the property manager has served notice on the insured",
    asset_heading="Third-party property affected",
    asset_rows=(
        ("Owner of the property", "Chandler Vale Cold Chain Solutions, tenant of Suite 400"),
        ("Building owner", "Hampton Roads Logistics Center Owner, LLC — certificate holder"),
        ("Condensing units", "Four, process refrigeration, not the building's units"),
        ("Refrigerant", "Approximately 140 lb R-448A vented"),
        ("Consequence", "22,000 sq ft temperature-controlled area lost cooling"),
    ),
    estimated_loss="182,000", repair_estimate="142,000",
    repair_estimate_label="Third-party equipment and reinstatement",
    report_slug="hampton-roads-lifting-incident-report",
    report_title="Lifting incident report",
    report_firm="Chesapeake Rigging & Crane",
    report_reference="CRC-2025-0826",
    report_author="Anselm Kirchhoff-Adeyemi, Lift Director",
    report_instructions=(
        ("Prepared for", "Tidewater Roofing & Exteriors, LLC and its insurers"),
        ("Prepared on", "27 August 2025"),
        ("Crane", "90 ton hydraulic, operator and signal person supplied by this company"),
        ("Scope", "Sequence of the lift and the point of contact"),
    ),
    report_circumstances=(
        "The building is a 640,000 sq ft logistics facility, fully occupied and operating "
        "with three tenants running 24/7 distribution. The insured is approximately 60% "
        "through a phased TPO re-roof for the building owner. Roof Section 7 had not been "
        "torn off; the lift was staging material for it."),
    report_findings=(
        "The pick was within the load chart at 38% of rated capacity and a written lift "
        "plan was in force. The load path passed within approximately 4 metres of the "
        "Suite 400 tenant's rooftop condensing bank. On the traverse the load contacted "
        "the guy wires on the eastern unit and then the unit housings, and dragged across "
        "the bank before the operator arrested the swing. Nothing was dropped. The "
        "contributing factor is the load path, not the machine or the rigging: the "
        "tenant's equipment was inside the swing radius and the lift plan did not exclude "
        "it. The crane has been taken out of service for inspection and the operator has "
        "provided a written statement."),
    report_quantum=(
        ("Four condensing units, two likely beyond repair", "USD 95,000"),
        ("Refrigerant recovery, recharge and leak test", "USD 18,000"),
        ("Rooftop piping, insulation, disconnects and whips", "USD 22,000"),
        ("Aluminium rooftop walkway", "USD 7,000"),
        ("Temporary cooling — spot coolers and rental chiller", "USD 40,000"),
    ),
    report_comment_heading="On the re-sequencing",
    report_comment=(
        "All crane operations on the project were stopped pending review and hoisting has "
        "been re-sequenced to the north side, away from tenant equipment. That should have "
        "been the sequence from the outset given the occupancy."),
    report_recommendation=(
        "Do not resume hoisting over any occupied tenant's rooftop plant. Re-issue the "
        "lift plan with an exclusion zone drawn around all rooftop equipment."),
    schedule_slug="hampton-roads-tenant-damage-schedule",
    schedule_title="Tenant damage schedule",
    schedule_headers=("Item", "Description", "Quantity", "Total USD"),
    schedule_rows=(
        ("Condensing units", "Crushed coils and bent fan shrouds", "2", "38000"),
        ("Condensing unit", "Fractured refrigerant line, charge lost", "1", "31000"),
        ("Condensing unit", "Housing displaced off curb", "1", "26000"),
        ("Refrigerant", "R-448A recovery, recharge and leak test", "140", "18000"),
        ("Piping", "Rooftop refrigerant piping and insulation", "30", "14000"),
        ("Electrical", "Condenser disconnects and whips", "2", "8000"),
        ("Access", "Aluminium rooftop walkway", "20", "7000"),
        ("Mitigation", "Spot coolers and rental chiller, running", "1", "40000"),
    ),
    schedule_total_label="Total third-party damage",
    email_subject="New loss - Tidewater Roofing - crane load into tenant rooftop plant, Chesapeake VA",
    email_to="claims@tidewaterexchange.example",
    email_cc="b.tsigaridas-cole@tidewaterroofing.example",
    email_date="Wed, 27 Aug 2025 08:40:31 -0400",
    email_message_id="cb-roof-2025-3376-b@coastlinebrantley.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify damage to a third party's rooftop "
        "plant on behalf of our client Tidewater Roofing & Exteriors, LLC. Our reference is "
        "CB/ROOF/2024/3376. The client's operations manager is away with the certificate "
        "file, so please locate on our reference - it is the commercial liability policy we "
        "placed for them that renewed last November."),
    email_facts=(
        ("Broker reference", "CB/ROOF/2024/3376"),
        ("Insured name", "Tidewater Roofing & Exteriors, LLC"),
        ("Insured licence", "Virginia Class A Contractor 2705-118442"),
        ("Date of loss", "26 August 2025"),
        ("Loss location", "1900 Bainbridge Logistics Way, Chesapeake, VA 23320"),
        ("Cause", "Crane load swung into a tenant's rooftop condensing units"),
        ("Estimated loss", "USD 182,000"),
    ),
    email_narrative=(
        "A bundle of insulation and membrane being flown to Roof Section 7 caught the guy\n"
        "wires and then the housings on a tenant's rooftop condensing bank and dragged\n"
        "across it. Nothing was dropped and nobody was hurt.\n\n"
        "The equipment belongs to the tenant in Suite 400, not to the building owner and\n"
        "not to our client. It is their process refrigeration for a 22,000 sq ft\n"
        "temperature-controlled fulfilment area and it is down. They have spot coolers and\n"
        "a rental chiller in and are saying they have product at risk. The property manager\n"
        "has put our client on notice for the equipment, the refrigerant, the temporary\n"
        "cooling and any product and business interruption.\n\n"
        "Nothing on the tenant's equipment has been touched, straightened or repaired.\n"
        "Our client paid USD 3,800 for the first two days of spot coolers directly, which\n"
        "we flag as a possible voluntary payment point."),
    email_closing=(
        "Please confirm a claim reference and an adjuster. Our client would also like your\n"
        "view on the crane company's position - their hire contract has our client\n"
        "indemnifying them, which we are not happy about."),
    expected="GL-3376-90284", confidence="Strong",
    why=("No policy number. The insured's name and Virginia contractor licence, the broker "
         "reference `CB/ROOF/2024/3376`, the sender domain and the building owner — a scheduled "
         "certificate holder on this policy — all agree, and the loss is third-party property "
         "damage arising from the insured's operations."),
    exercises=(
        "**Damaged property belonging to a party who is not an insured and not a certificate "
        "holder.** The tenant is neither; the building owner is. Identification must not depend "
        "on the claimant being on the policy.",
        "**A second loss on a policy already claimed on.** Pack 10 is the same policy, a "
        "different project and a different peril.",
        "**A recovery question raised in the notice.** The crane hire indemnity affects "
        "subrogation, not which policy answers.",
    ),
))

# ---------------------------------------------------------------------------
# 21 — Larkspur Landscaping. NO_MATCH. Fleet collision, insured not in the book.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="larkspur-fleet-collision",
    title="Larkspur Landscaping — fleet collision with third-party property damage",
    line_of_business="Motor — commercial fleet",
    broker="Greenbel Risk Services, LLC",
    handler="Wendeline Kirchhoff-Baptiste", handler_role="Producer",
    handler_email="w.kirchhoff-baptiste@greenbelrisk.example",
    handler_phone="+1 301 555 0416",
    broker_reference="GRS/AUTO/2026/0114",
    reported_on="16 March 2026",
    insured="Larkspur Landscaping & Grounds Management, LLC",
    policy_number="",
    policy_type="Commercial Automobile",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Ballenger Creek Auto & Tyre",
    claimant_contact="Marisol Achebe-Trent, Operations Director (insured)",
    claimant_email="m.achebe-trent@larkspurlandscaping.example",
    date_of_loss="14 March 2026", time_of_loss="07:48 EDT",
    loss_location="Ballenger Creek Pike at Elmer Derr Road, Frederick, MD 21703",
    cause="Collision — avoiding action, trailer jackknife and departure from the roadway",
    description=(
        "The insured's F-550 dump body was towing an equipment trailer carrying a compact "
        "track loader and a walk-behind trencher to a residential job. A passenger vehicle "
        "pulled out without stopping, the driver braked hard, the trailer jackknifed and "
        "the combination left the roadway. The truck sheared a utility pole and came to "
        "rest against the corner of a commercial building. The trailer overturned and the "
        "track loader was thrown clear."),
    affected_assets=(
        "2021 Ford F-550 dump body, likely total loss; a 20 ft equipment trailer; a Bobcat "
        "T770 compact track loader; a walk-behind trencher; a third party's building "
        "corner, roll-up door and storefront; a utility pole, transformer and conductor"),
    injuries="1",
    business_interruption="Yes — the third-party auto and tyre business was closed four days",
    structural_damage="Yes — third party's building corner masonry and roll-up door",
    environmental_exposure="Yes — approximately 40 gallons of diesel and hydraulic fluid released",
    authorities="Maryland State Police; Maryland Department of the Environment; Potomac Edison",
    police_reference="MSP-2026-0341",
    incident_reference="LLG-2026-0314",
    potential_litigation="Possible — unidentified third-party vehicle, citation pending",
    asset_heading="Vehicles, plant and third-party property",
    asset_rows=(
        ("Unit 7", "2021 Ford F-550 dump body, likely total loss"),
        ("Trailer", "20 ft equipment trailer, overturned"),
        ("Plant", "Bobcat T770 compact track loader, uneconomic to repair"),
        ("Plant", "Walk-behind trencher"),
        ("Third party", "Ballenger Creek Auto & Tyre building; Potomac Edison pole 44-1182"),
    ),
    estimated_loss="320,000", repair_estimate="153,400",
    repair_estimate_label="Own damage",
    report_slug="larkspur-motor-assessors-report",
    report_title="Motor assessor's report",
    report_firm="Monocacy Vehicle Assessing",
    report_reference="MVA-2026-0318",
    report_author="Casimir Oyelaran-Fenwick, AMIMI",
    report_instructions=(
        ("Instructed by", "Greenbel Risk Services, LLC"),
        ("Instructed on", "16 March 2026"),
        ("Inspection", "17 March 2026, Mid-Atlantic Truck Centre and Frederick Towing"),
        ("Scope", "Own damage assessment and settlement basis"),
    ),
    report_circumstances=(
        "A commercial landscape maintenance and snow management contractor operating "
        "fourteen trucks, nine trailers and associated plant across three Maryland "
        "counties. The combination was in transit to a residential HOA contract."),
    report_findings=(
        "The F-550 has sustained frontal and offside impact damage with chassis rail "
        "deformation aft of the front axle. Repair cost is assessed at USD 71,400 against "
        "a pre-accident value of USD 62,000, so the vehicle is a constructive total loss. "
        "The trailer is repairable at USD 9,800. The Bobcat T770 has cab, boom arm and "
        "undercarriage damage; the dealer assesses it as uneconomic to repair at USD "
        "68,000 against a replacement of USD 74,500. The trencher is repairable at USD "
        "4,200. The primary cause is attributed by the attending troopers to an "
        "unidentified vehicle that failed to stop."),
    report_quantum=(
        ("2021 Ford F-550, constructive total loss", "USD 62,000"),
        ("20 ft equipment trailer, repair", "USD 9,800"),
        ("Bobcat T770 compact track loader", "USD 68,000"),
        ("Walk-behind trencher, repair", "USD 4,200"),
        ("Towing, recovery and storage", "USD 8,900"),
    ),
    report_comment_heading="On the plant",
    report_comment=(
        "The track loader and the trencher are contractors plant carried on the trailer at "
        "the time of loss. Whether they respond under the motor policy or under a separate "
        "plant floater depends on the placement, which this assessor has not seen."),
    report_recommendation=(
        "Settle the F-550 on a total loss basis. Obtain the insured's plant cover position "
        "before the Bobcat is disposed of."),
    schedule_slug="larkspur-damage-schedule",
    schedule_title="Damage schedule",
    schedule_headers=("Head", "Item", "Basis", "Total USD"),
    schedule_rows=(
        ("Own damage", "2021 Ford F-550 dump body", "Total loss, PAV", "62000"),
        ("Own damage", "20 ft equipment trailer", "Repair", "9800"),
        ("Own plant", "Bobcat T770 compact track loader", "Uneconomic to repair", "68000"),
        ("Own plant", "Walk-behind trencher", "Repair", "4200"),
        ("Own damage", "Towing, recovery and storage", "Invoice", "8900"),
        ("Third party", "Ballenger Creek Auto & Tyre building", "Contractor estimate", "84000"),
        ("Third party", "Potomac Edison pole, transformer, conductor", "Utility estimate", "45000"),
        ("Third party", "Environmental containment and soil removal", "Invoice", "26700"),
    ),
    schedule_total_label="Total indicated",
    email_subject="FNOL - Larkspur Landscaping & Grounds Management - fleet collision, Frederick MD",
    email_to="newloss@intake-claims.example",
    email_cc="m.achebe-trent@larkspurlandscaping.example",
    email_date="Mon, 16 Mar 2026 13:22:47 -0400",
    email_message_id="grs-auto-2026-0114-a@greenbelrisk.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify a fleet collision on behalf of our "
        "client Larkspur Landscaping & Grounds Management, LLC. Our reference is "
        "GRS/AUTO/2026/0114."),
    email_facts=(
        ("Broker reference", "GRS/AUTO/2026/0114"),
        ("Insured name", "Larkspur Landscaping & Grounds Management, LLC"),
        ("Date of loss", "14 March 2026"),
        ("Loss location", "Ballenger Creek Pike at Elmer Derr Road, Frederick, MD 21703"),
        ("Cause", "Collision - avoiding action, trailer jackknife"),
        ("Estimated loss", "USD 320,000"),
    ),
    email_narrative=(
        "A passenger vehicle pulled out in front of our client's F-550 and trailer on\n"
        "Saturday morning. The driver braked, the trailer jackknifed and the combination\n"
        "left the road, sheared a utility pole and struck a commercial building.\n\n"
        "Our client's driver has a fractured sternum and rib fractures and was admitted for\n"
        "two nights - that will be a workers compensation matter on a separate file. The\n"
        "other vehicle did not make contact and left before troopers arrived.\n\n"
        "There is third-party building damage, a utility recovery claim from Potomac\n"
        "Edison, and about 40 gallons of diesel and hydraulic fluid on the ground with MDE\n"
        "notified. We are looking to the motor policy, and to whatever plant cover our\n"
        "client holds for the Bobcat and the trencher."),
    email_closing=(
        "Please confirm which of our client's placements you are handling and assign an\n"
        "adjuster and an equipment appraiser. The plant is accruing storage."),
    expected="NO_MATCH", confidence="No Match",
    why=("Larkspur Landscaping & Grounds Management, LLC appears nowhere in the book as a named "
         "insured, joint name, principal, contractor or loss payee. Greenbel Risk Services is "
         "not a broker on any policy and `greenbelrisk.example` is not a broker domain in the "
         "book. The loss location is not on any schedule, and the lines sought — commercial "
         "motor, workers compensation and standalone environmental — are not written at all."),
    exercises=(
        "**A plausible near-miss on line of business.** A Bobcat track loader is exactly the "
        "class of plant scheduled on two inland marine policies in the book, and the loss is in "
        "Maryland where a commercial property policy sits. Neither is a match.",
        "**No identifying value hits the pool.** With no policy number, no known broker "
        "reference, no known domain and no scheduled location, the candidate query should fall "
        "back to country plus line of business plus in-force — and the gate should still reject "
        "every one of them.",
        "**Rejecting is the correct answer**, and should be reported with a reason rather than "
        "as an empty result.",
    ),
))

# ---------------------------------------------------------------------------
# 22 — Meadowcrest. NO_MATCH. A fabricated policy number in the book's format.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="meadowcrest-frozen-sprinkler",
    title="Meadowcrest at Zionsville — frozen sprinkler line in an assisted living community",
    line_of_business="Commercial property",
    broker="Pemberton Wexler Associates, Inc.",
    handler="Anselm Duchamp-Rivera", handler_role="Producer",
    handler_email="a.duchamp-rivera@pembertonwexler.example",
    handler_phone="+1 317 555 0904",
    broker_reference="PWA/PROP/2025/0022",
    reported_on="29 January 2026",
    insured="Meadowcrest Assisted Living Communities, Inc.",
    policy_number="CP-6650-11223",
    policy_type="Commercial Property",
    policy_period="1 October 2025 to 1 October 2026",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Meadowcrest at Zionsville Operating Co., LLC",
    claimant_contact="Ottilie Rasmussen-Baptiste, Executive Director",
    claimant_email="o.rasmussen-baptiste@meadowcrestal.example",
    date_of_loss="26 January 2026", time_of_loss="03:50 EST, discovered 04:15",
    loss_location="1180 North Ford Road, Zionsville, IN 46077",
    cause="Freeze — frozen sprinkler branch line in the east wing attic",
    description=(
        "A wet-pipe sprinkler branch line serving the east wing attic froze and split "
        "during an extreme cold event, with NWS Indianapolis recording a low of -11F and "
        "wind chills near -30F after four consecutive days below 10F. Water discharged for "
        "approximately twenty-five minutes before the riser was closed by facilities "
        "staff. Fifty resident rooms across two floors, the east corridor and the main "
        "kitchen and servery were affected, and fifty residents were relocated."),
    affected_assets=(
        "11,000 sq ft of attic and ceiling assemblies; 50 resident rooms with ceiling "
        "collapse in nine; 180 linear feet of corridor ceiling grid; the main kitchen and "
        "servery including two reach-in refrigerators and the dish machine; the nurse call "
        "head end; elevator No. 2 pit; one 208V distribution panel"),
    business_interruption="Yes — the east wing is out of service and 50 residents relocated",
    structural_damage="No",
    environmental_exposure="No",
    authorities="Zionsville Fire Department; Indiana State Department of Health",
    incident_reference="ZFD-2026-000188",
    asset_heading="Community and areas affected",
    asset_rows=(
        ("Community", "Meadowcrest at Zionsville, 88 licensed beds, built 2013"),
        ("Construction", "Two storeys, wood frame with brick veneer, NFPA 13R sprinklered"),
        ("East wing", "Rooms 118 to 142 and 218 to 242, 50 rooms"),
        ("Kitchen and servery", "Serving line, two reach-ins, dish machine, dry goods"),
        ("Systems", "Nurse call head end degraded, elevator No. 2 out of service"),
    ),
    estimated_loss="2,900,000", repair_estimate="1,840,000",
    repair_estimate_label="Reinstatement estimate",
    report_slug="meadowcrest-restoration-report",
    report_title="Restoration contractor's report",
    report_firm="Crossroads Restoration Services",
    report_reference="CRS-2026-0126",
    report_author="Perpetua Vandenberg-Okoye, IICRC CDS",
    report_instructions=(
        ("Instructed by", "Meadowcrest Assisted Living Communities, Inc."),
        ("Instructed on", "26 January 2026, 06:20"),
        ("Site attendance", "26 January to 8 February 2026"),
        ("Scope", "Emergency mitigation, drying and controlled demolition"),
    ),
    report_circumstances=(
        "A two-storey assisted living community of 88 licensed beds, fully sprinklered to "
        "NFPA 13R. The split occurred in the east wing attic, which is served by two unit "
        "heaters. The facilities director reports both were operational and that the split "
        "occurred at a section of line running close to a soffit vent."),
    report_findings=(
        "Water tracked from the attic through the ceiling assemblies into fifty resident "
        "rooms on both floors and along the east corridor, and through the kitchen ceiling "
        "onto the serving line. Ceiling collapse occurred in nine rooms. Moisture readings "
        "on arrival ranged from 34% to 46% WME. Controlled demolition of saturated ceiling "
        "assemblies began the same morning. A continuous fire watch was maintained from "
        "04:20 on 26 January until the system was restored to service at 11:40 on 28 "
        "January, and the Indiana State Department of Health was notified at 08:00 on 26 "
        "January under the licensure requirements."),
    report_quantum=(
        ("Emergency mitigation, extraction and drying", "USD 214,000"),
        ("Resident rooms, 50, ceilings and finishes", "USD 940,000"),
        ("East corridor and common areas", "USD 186,000"),
        ("Kitchen, servery and equipment", "USD 268,000"),
        ("Nurse call, elevator pit and electrical", "USD 232,000"),
    ),
    report_comment_heading="On resident relocation",
    report_comment=(
        "Twenty-two residents were relocated within the community and twenty-eight to two "
        "sister communities and a contracted skilled nursing partner. Relocation, transfer "
        "and agency staffing costs are accruing and are not included in the figures above."),
    report_recommendation=(
        "Complete drying before reinstatement. Do not return the east wing to service until "
        "the nurse call system is fully restored and certified."),
    schedule_slug="meadowcrest-reinstatement-schedule",
    schedule_title="Reinstatement schedule",
    schedule_headers=("Area", "Description", "Quantity", "Total USD"),
    schedule_rows=(
        ("East wing", "Attic and ceiling assemblies", "11000", "348000"),
        ("Resident rooms", "Ceilings, walls, flooring, nine with collapse", "50", "592000"),
        ("East corridor", "Ceiling grid, handrails, wall protection, flooring", "180", "186000"),
        ("Kitchen", "Serving line, ceiling, finishes", "1", "148000"),
        ("Kitchen", "Two reach-in refrigerators and dish machine", "3", "120000"),
        ("Systems", "Nurse call head end and two floor panels", "1", "96000"),
        ("Systems", "Elevator No. 2 pit and 208V panel", "2", "136000"),
        ("All", "Emergency mitigation, drying, controlled demolition", "1", "214000"),
    ),
    schedule_total_label="Total reinstatement",
    email_subject="FNOL - Meadowcrest at Zionsville - frozen sprinkler line, Zionsville IN - CP-6650-11223",
    email_to="claims@intake-claims.example",
    email_cc="o.rasmussen-baptiste@meadowcrestal.example",
    email_date="Thu, 29 Jan 2026 09:36:12 -0500",
    email_message_id="pwa-prop-2026-0022-a@pembertonwexler.example",
    email_opening=(
        "Good morning,\n\nWe are instructed to notify a freeze and water damage loss on "
        "behalf of our client Meadowcrest Assisted Living Communities, Inc. Our reference "
        "is PWA/PROP/2025/0022.\n\n"
        "A caveat on the policy number below. It is taken from the insured's own "
        "certificate of insurance. The account was remarketed at the 1 October 2025 renewal "
        "and the certificate in their file may pre-date the change, so please confirm "
        "against your own records before the file is set up."),
    email_facts=(
        ("Policy number as recorded on the certificate", "CP-6650-11223"),
        ("Broker reference", "PWA/PROP/2025/0022"),
        ("Insured name", "Meadowcrest Assisted Living Communities, Inc."),
        ("Date of loss", "26 January 2026"),
        ("Loss location", "1180 North Ford Road, Zionsville, IN 46077"),
        ("Cause", "Freeze - frozen sprinkler branch line"),
        ("Estimated loss", "USD 2,900,000"),
    ),
    email_narrative=(
        "A sprinkler branch line in the east wing attic froze and split at about 03:50 in\n"
        "the extreme cold at the end of last week. The alarm activated and the monitoring\n"
        "company dispatched the fire department; facilities staff closed the riser at 04:15.\n\n"
        "Fifty resident rooms across both floors are affected, nine with ceiling collapse,\n"
        "along with the east corridor and the main kitchen. Fifty residents have been\n"
        "relocated - twenty-two within the community and twenty-eight to sister\n"
        "communities and a skilled nursing partner. The state department of health was\n"
        "notified the same morning under the licensure rules.\n\n"
        "A fire watch ran continuously until the system was restored on the 28th."),
    email_closing=(
        "Please confirm a claim reference and an adjuster urgently - we have residents "
        "displaced and agency staffing costs running."),
    expected="NO_MATCH", confidence="No Match",
    why=("The stated number `CP-6650-11223` does not exist in the book and is not a near variant "
         "of anything in it. The insured, both additional named insureds, the broker, the broker "
         "reference, the sender domain, the loss location and the state all fail to match. The "
         "notice itself discloses that the number came from a possibly stale certificate."),
    exercises=(
        "**A policy number that looks exactly right and is not.** It shares the `CP-` prefix and "
        "the four-plus-five digit shape of the four property policies in the book. Containment "
        "and edit-distance rungs must not manufacture a match out of it.",
        "**Distractors on three axes at once.** Senior living occupancy resembles the Northfield "
        "project; the frozen sprinkler cause is identical to pack 13; resident relocation "
        "resembles the Windrow Grove tenant relocation head. None of them is this policy.",
        "**No Indiana risk exists anywhere in the book**, which is the cleanest discriminator.",
    ),
))

# ---------------------------------------------------------------------------
# 23 — Bayview Terrace COA. NO_MATCH. Habitational water loss, wrong state.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="bayview-terrace-riser",
    title="Bayview Terrace Condominiums — domestic hot water riser failure",
    line_of_business="Commercial property — condominium association",
    broker="Harborcrest Community Management, LLC",
    handler="Thaddeus Oyelowo-Brand", handler_role="Community Manager",
    handler_email="t.oyelowo-brand@harborcrestmgmt.example",
    handler_phone="+1 360 555 0244",
    broker_reference="HCM/BT/2026/0502",
    reported_on="5 May 2026",
    insured="Bayview Terrace Condominium Association, Inc.",
    policy_number="",
    policy_type="Not stated — the association's insurance file is incomplete",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="Bayview Terrace Condominium Association, Inc.",
    claimant_contact="Cordelia Nakamura-Whitfield, Board President",
    claimant_email="c.nakamura-whitfield@bayviewterracecoa.example",
    date_of_loss="2 May 2026", time_of_loss="between 02:00 and 05:30, discovered 05:35",
    loss_location="1200 Bayview Terrace Drive, Bremerton, WA 98312",
    cause="Escape of water — pitting corrosion failure of a copper hot water riser",
    description=(
        "The 2-inch copper domestic hot water riser in the Building 2 central chase failed "
        "at a pinhole that opened into a longitudinal split between the second and third "
        "floors. Water discharged into the chase and then into units on all three floors "
        "of the west stack and into the ground floor storage rooms. The attending plumber "
        "attributes the failure to internal pitting corrosion consistent with the age of "
        "the pipe and the association's water chemistry."),
    affected_assets=(
        "Units 2-301, 2-201 and 2-101 uninhabitable; six further units with moderate or "
        "minor damage; twelve ground floor storage lockers with owner contents affected in "
        "nine; the central chase, corridor flooring on three floors and the stairwell "
        "landing; a Building 2 electrical sub-panel"),
    business_interruption="No — three households displaced to hotels",
    structural_damage="No",
    environmental_exposure="No",
    authorities="None",
    incident_reference="BTC-2026-0502",
    asset_heading="Buildings and units affected",
    asset_rows=(
        ("Community", "Bayview Terrace Condominiums, 96 units in four buildings, built 1998"),
        ("Building affected", "Building 2 only, 24 units, west stack"),
        ("Uninhabitable", "2-301, 2-201 and 2-101"),
        ("Moderate", "2-302, 2-202, 2-102 at the shared demising walls"),
        ("Common elements", "Central chase, corridors, stairwell landing, sub-panel"),
    ),
    estimated_loss="697,400", repair_estimate="549,000",
    repair_estimate_label="Association reinstatement",
    report_slug="bayview-terrace-plumbers-report",
    report_title="Plumbing failure report",
    report_firm="Tideline Mechanical",
    report_reference="TM-2026-0502",
    report_author="Ferdinand Achebe-Lindqvist, Master Plumber",
    report_instructions=(
        ("Instructed by", "Harborcrest Community Management, LLC"),
        ("Instructed on", "2 May 2026, 06:40"),
        ("Site attendance", "2 May 2026"),
        ("Scope", "Cause of the riser failure and the condition of the remaining risers"),
    ),
    report_circumstances=(
        "Four residential buildings of wood frame construction, three storeys, built in "
        "1998, on a single parcel. Each building has a central chase carrying the domestic "
        "hot and cold water risers. The failure is in Building 2 between the second and "
        "third floors."),
    report_findings=(
        "The failed section is 2-inch type M copper. The failure initiated as a pinhole on "
        "the invert and opened into a longitudinal split approximately 90 mm long. Wall "
        "thickness at the failure measures 0.31 mm against a nominal 0.71 mm. The interior "
        "surface shows classic hemispherical pitting with tubercles, consistent with "
        "pitting corrosion driven by water chemistry over a long period rather than with a "
        "discrete event. On opening the chase further, comparable wall thinning was found "
        "at two other locations in the same riser. The failed section has been cut out, "
        "bagged, labelled and retained in the maintenance room."),
    report_quantum=(
        ("Riser repair and chase reinstatement", "USD 46,000"),
        ("Building repairs, nine units and common elements", "USD 385,000"),
        ("Cabinetry and flooring", "USD 118,000"),
        ("Emergency mitigation and drying", "USD 108,000"),
        ("Storage locker contents, owner property", "USD 34,000"),
    ),
    report_comment_heading="On the remaining risers",
    report_comment=(
        "The wall thinning found at two further locations in the Building 2 riser is very "
        "likely present in the other three buildings, which were built at the same time "
        "with the same material and are on the same water supply. A full riser replacement "
        "programme should be planned. That is a capital matter for the association and is "
        "not part of this claim."),
    report_recommendation=(
        "Replace the whole Building 2 hot water riser rather than patching the failed "
        "section. Commission a water chemistry assessment before specifying replacement "
        "material."),
    schedule_slug="bayview-terrace-damage-schedule",
    schedule_title="Damage schedule",
    schedule_headers=("Unit or element", "Condition", "Basis", "Total USD"),
    schedule_rows=(
        ("2-301", "Uninhabitable, ceiling, walls, flooring, cabinets", "Reinstatement", "96000"),
        ("2-201", "Uninhabitable, ceiling collapse, flooring, cabinets", "Reinstatement", "112000"),
        ("2-101", "Uninhabitable, 2 in standing water throughout", "Reinstatement", "134000"),
        ("2-302, 2-202, 2-102", "Moderate, demising walls and flooring", "Reinstatement", "78000"),
        ("2-303, 2-203, 2-103", "Minor, chase wall staining", "Reinstatement", "18000"),
        ("Storage lockers", "Twelve flooded, owner contents in nine", "Owner property", "34000"),
        ("Common elements", "Chase, corridors, stairwell, sub-panel", "Reinstatement", "77000"),
        ("Riser", "Repair and chase reinstatement", "Reinstatement", "46000"),
        ("Mitigation", "Extraction, drying, containment", "Invoice", "108000"),
    ),
    schedule_total_label="Total association claim",
    email_subject="Water loss - Bayview Terrace Condominium Association - Building 2 riser failure",
    email_to="claims@intake-claims.example",
    email_cc="c.nakamura-whitfield@bayviewterracecoa.example",
    email_date="Tue, 05 May 2026 10:04:33 -0700",
    email_message_id="hcm-bt-2026-0502-a@harborcrestmgmt.example",
    email_opening=(
        "Good morning,\n\nI am the community manager for the Bayview Terrace Condominium "
        "Association and I am reporting a significant water loss in one of their buildings. "
        "Our reference is HCM/BT/2026/0502.\n\n"
        "I am sending this to the general intake address because our management company is "
        "between account managers and I could not reach anyone at the agency this morning. "
        "The association's insurance file is not in good order and I do not have the policy "
        "number or the schedule. Please tell me exactly what to look for and I will get it."),
    email_facts=(
        ("Broker reference", "HCM/BT/2026/0502"),
        ("Insured name", "Bayview Terrace Condominium Association, Inc."),
        ("Managing agent", "Harborcrest Community Management, LLC"),
        ("Date of loss", "2 May 2026"),
        ("Loss location", "1200 Bayview Terrace Drive, Bremerton, WA 98312"),
        ("Cause", "Escape of water - copper hot water riser failure"),
        ("Estimated loss", "USD 697,400"),
    ),
    email_narrative=(
        "The domestic hot water riser in the Building 2 chase failed overnight on Friday.\n"
        "A resident on the second floor heard running water at about 05:35 and found it\n"
        "coming through her ceiling light fitting.\n\n"
        "Nine units are affected, three of them uninhabitable, and the ground floor storage\n"
        "lockers flooded. Three households are in hotels and the association has been\n"
        "paying for that pending your direction.\n\n"
        "The plumber's view is pitting corrosion from the inside, consistent with the age\n"
        "of the pipe. He found similar wall thinning at two other points when he opened the\n"
        "chase. I should say plainly that our attorney has already warned the board that\n"
        "wear and tear may be raised, and I would rather know now."),
    email_closing=(
        "Please confirm a claim reference and an adjuster this week. We also need direction\n"
        "on the betterments and improvements line and on who carries the displacement costs."),
    expected="NO_MATCH", confidence="No Match",
    why=("Bayview Terrace Condominium Association is not a named insured, joint name, managing "
         "agent, mortgagee or loss payee anywhere in the book. Harborcrest Community Management "
         "is not a broker in the book and `harborcrestmgmt.example` is not a broker domain. "
         "Bremerton, Washington is on no schedule and no policy in the book covers a Washington "
         "risk or a condominium association form."),
    exercises=(
        "**The closest habitational near-miss in the set.** Multifamily water loss with resident "
        "displacement, reported by a property manager with no policy number — structurally the "
        "same shape as packs 4 and 16, which both match. Entity and state are the discriminators.",
        "**A reporter who is neither broker nor insured.** The sender is the managing agent, so "
        "`broker_domain` should not fire at all rather than firing wrongly.",
        "**A cause that invites a coverage argument** — pitting corrosion against wear and tear "
        "— which is irrelevant to identification and must stay out of the score.",
    ),
))

# ---------------------------------------------------------------------------
# 24 — Cobalt Ridge. NO_MATCH. Trench collapse, contractor not in the book.
# ---------------------------------------------------------------------------
SPECS.append(pack(
    slug="cobalt-ridge-trench-collapse",
    title="Overland Park sewer rehabilitation — trench collapse and serious injury",
    line_of_business="Construction — casualty",
    broker="Ozark Meridian Insurance Partners",
    handler="Henrietta Balogun-Sawicki", handler_role="Account Executive",
    handler_email="h.balogun-sawicki@ozarkmeridian.example",
    handler_phone="+1 816 555 0722",
    broker_reference="OMI/CAS/2026/0033",
    reported_on="21 May 2026",
    insured="Cobalt Ridge Utility Contractors, LLC",
    policy_number="",
    policy_type="Not stated — multiple placements potentially engaged",
    policy_period="Not stated on the notice",
    policy_limit="Not stated on the notice", policy_deductible="Not stated on the notice",
    claimant="City of Overland Park",
    claimant_contact="Solomon Vanterpool-Achike, HSE Director (insured)",
    claimant_email="s.vanterpool-achike@cobaltridgeutility.example",
    date_of_loss="19 May 2026", time_of_loss="09:34 CDT",
    loss_location="2200 block of West Carbondale Avenue, Overland Park, KS 66214",
    cause="Excavation collapse — wall failure beyond the end of the trench box",
    description=(
        "A crew of five was working in an open cut excavation approximately fourteen feet "
        "deep replacing a section of vitrified clay sanitary sewer. A trench box was in use "
        "for the working section. The north wall failed at a point about six feet beyond "
        "the end of the box, where the excavation had been extended to expose an unmarked "
        "service lateral. Two employees were partially buried to chest height and were "
        "extricated by the crew and by technical rescue."),
    affected_assets=(
        "Approximately 40 feet of public sidewalk and 30 feet of the eastbound kerb lane "
        "collapsed; a 4-inch gas service to an adjacent retail centre lost its support; the "
        "existing sewer main fractured with an estimated 9,000 gallons discharged; an "
        "excavator tipped into the void; a rented compressor and a contractor vehicle lost"),
    injuries="3",
    business_interruption="Yes — nine retail tenants without gas for 31 hours, two restaurants closed",
    structural_damage="Yes — public roadway and sidewalk collapse, City stop work order issued",
    environmental_exposure="Yes — approximately 9,000 gallons of sewage into the storm system",
    authorities="OSHA Region VII; Kansas Department of Health and Environment; Overland Park Fire",
    incident_reference="CRU-2026-IN-0033",
    potential_litigation="Yes — counsel retained, OSHA citation exposure",
    asset_heading="Works, third-party property and persons",
    asset_rows=(
        ("Project", "City of Overland Park sanitary sewer rehabilitation"),
        ("Contract", "OP-2026-SS-14, Cobalt Ridge as prime contractor to the City"),
        ("Employees", "Two admitted with crush and compression injuries, one treated and released"),
        ("Third party", "40 ft sidewalk, 30 ft kerb lane, 4 in gas service"),
        ("Own plant", "Excavator tipped into the void, rented compressor, one vehicle"),
    ),
    estimated_loss="1,150,000", repair_estimate="584,000",
    repair_estimate_label="Own plant and project rework",
    report_slug="cobalt-ridge-geotechnical-report",
    report_title="Geotechnical investigation report",
    report_firm="Flint Hills Geotechnical",
    report_reference="FHG-2026-0520",
    report_author="Anneliese Kowalczyk-Obi, PE, GE",
    report_instructions=(
        ("Instructed by", "Cobalt Ridge Utility Contractors, LLC"),
        ("Instructed on", "19 May 2026"),
        ("Site attendance", "19 and 20 May 2026"),
        ("Scope", "Cause of the wall failure and the safe condition of the excavation"),
    ),
    report_circumstances=(
        "An open cut excavation approximately fourteen feet deep and eight feet wide in a "
        "public right of way, replacing a 12-inch vitrified clay sanitary sewer under a "
        "City contract. A trench box protected the working section. The failure occurred "
        "in an extension beyond the box made to expose an unmarked service lateral."),
    report_findings=(
        "The soil profile at the failure is a stiff lean clay to about seven feet over a "
        "medium dense silty sand, with a perched water table at nine feet six inches. The "
        "sand stratum classifies as Type C. The extension beyond the trench box was neither "
        "shored, sloped nor benched, and at fourteen feet in Type C material an unsupported "
        "vertical face is not stable under any condition. The competent person's daily "
        "inspection record for 19 May classifies the excavation as Type B, which the "
        "profile does not support. Sampling before backfill has been taken and photographed."),
    report_quantum=(
        ("Project rework, re-excavation and shoring redesign", "USD 310,000"),
        ("Excavator recovery and repair", "USD 190,000"),
        ("Rented compressor", "USD 38,000"),
        ("Contractor vehicle", "USD 46,000"),
        ("City roadway and sidewalk, range", "USD 240,000 to 400,000"),
    ),
    report_comment_heading="On the soil classification",
    report_comment=(
        "The classification recorded by the competent person is the central factual issue. "
        "A Type B classification permits a 1:1 slope; a Type C classification at this depth "
        "requires either a full-depth protective system or a 1.5:1 slope, neither of which "
        "was present in the extension. This is recorded as a finding of fact for the file."),
    report_recommendation=(
        "Do not re-open the excavation without a full-depth protective system designed for "
        "Type C material by a registered professional engineer."),
    schedule_slug="cobalt-ridge-loss-schedule",
    schedule_title="Loss schedule",
    schedule_headers=("Head", "Item", "Basis", "Total USD"),
    schedule_rows=(
        ("Own plant", "Excavator recovery and repair", "Assessor", "190000"),
        ("Own plant", "Rented compressor", "Rental agreement", "38000"),
        ("Own vehicle", "Contractor vehicle", "Assessor", "46000"),
        ("Project", "Rework, re-excavation, shoring redesign", "Estimate", "310000"),
        ("Third party", "City roadway and sidewalk", "City estimate", "320000"),
        ("Third party", "Kansas Gas Service response and re-support", "Utility estimate", "60000"),
        ("Environmental", "Sewage containment, recovery, KDHE reporting", "Invoice", "85000"),
        ("Third party", "Retail tenant business interruption, provisional", "Provisional", "101000"),
    ),
    schedule_total_label="Total indicated",
    email_subject="Incident notification - Cobalt Ridge Utility Contractors - trench collapse, Overland Park KS",
    email_to="claims@intake-claims.example",
    email_cc="s.vanterpool-achike@cobaltridgeutility.example",
    email_date="Thu, 21 May 2026 16:11:29 -0500",
    email_message_id="omi-cas-2026-0033-a@ozarkmeridian.example",
    email_opening=(
        "Good afternoon,\n\nWe are instructed to notify a serious incident on behalf of our "
        "client Cobalt Ridge Utility Contractors, LLC. Our reference is OMI/CAS/2026/0033. "
        "This is issued under their HSE procedure within 24 hours of a serious incident and "
        "we are placing all potentially relevant carriers on notice."),
    email_facts=(
        ("Broker reference", "OMI/CAS/2026/0033"),
        ("Insured name", "Cobalt Ridge Utility Contractors, LLC"),
        ("Contract number", "OP-2026-SS-14"),
        ("Date of loss", "19 May 2026"),
        ("Loss location", "2200 block of West Carbondale Avenue, Overland Park, KS 66214"),
        ("Cause", "Excavation collapse beyond the end of the trench box"),
        ("Estimated loss", "USD 1,150,000"),
    ),
    email_narrative=(
        "The north wall of a fourteen-foot excavation failed at a point beyond the trench\n"
        "box where the crew had extended it to expose an unmarked lateral. Two employees\n"
        "were buried to chest height and were extricated within 22 and 41 minutes. Both\n"
        "are admitted; one has a pelvic fracture and a crush injury to the lower leg. A\n"
        "third was cut during the rescue and released. No fatalities.\n\n"
        "The collapse undermined about 40 feet of public sidewalk and a lane of West\n"
        "Carbondale Avenue, took the support out from under a gas service to the adjacent\n"
        "retail centre, and fractured the existing sewer - about 9,000 gallons went into\n"
        "the storm system. OSHA Region VII and KDHE are both notified.\n\n"
        "There is no builder's risk and no owner-controlled programme on this contract. It\n"
        "is a City contract with our client as prime, so their own placements are the ones\n"
        "engaged: liability, excess, workers compensation, contractors equipment, motor and\n"
        "possibly contractors pollution."),
    email_closing=(
        "Please confirm which carriers you are handling and provide claim references. Our\n"
        "client's counsel is Marchetti, Oduya & Steinbrenner and they have asked to be\n"
        "copied on the acknowledgement."),
    expected="NO_MATCH", confidence="No Match",
    why=("Cobalt Ridge Utility Contractors, LLC appears nowhere in the book. Ozark Meridian "
         "Insurance Partners is not a broker on any policy and `ozarkmeridian.example` is not a "
         "broker domain. Contract `OP-2026-SS-14` is not a contract number or project reference "
         "in the book, the site is on no schedule, and the notice states there is no builder's "
         "risk or owner-controlled programme on the contract."),
    exercises=(
        "**The strongest construction near-miss in the set.** A contractor incident with "
        "third-party property damage, employee injuries, equipment damage and a contract number "
        "— structurally identical to packs 5, 8 and 19, all of which match.",
        "**A contract number that will hit the SQL pool and must still be rejected.** "
        "`OP-2026-SS-14` is long enough to be searched against all four reference columns; "
        "nothing in the book contains it.",
        "**Two policies in the book carry earth-movement exclusions triggered by the insured's "
        "own trenching** — precisely this fact pattern, for a different insured in a different "
        "state. Peril similarity is not identity.",
    ),
))
