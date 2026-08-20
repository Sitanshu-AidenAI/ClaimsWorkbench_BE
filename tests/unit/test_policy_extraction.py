"""Reading a policy wording's declarations page.

Pure functions over strings, so these cases are the specification of what a wording is
allowed to look like. Every one of them is a shape a real declarations page takes, and
several are shapes that a first pass over this module got wrong — the comments say
which, because a regression here is silent: a wrong insured name does not raise, it
scores a mismatch against the right policy.

The strong assertions are on the *refusals*. `None` rather than a guess is the rule
this module holds to, and a test that only pins the happy path would let a reader that
returns the producer's phone number as the broker's name pass.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from app.domain.enums import LineOfBusiness
from app.domain.policy_extraction import (
    PolicyFacts,
    read_line_of_business,
    read_policy_facts,
    read_policy_period,
)

GROUND_TRUTH = pathlib.Path(__file__).resolve().parents[2] / "policy" / "_ground_truth"

DECLARATIONS = """
MERIDIAN ATLANTIC INSURANCE COMPANY
A Stock Insurance Company - Home Office: 1400 Chesapeake Boulevard, Baltimore, MD 21202
NAIC No. 41783

COMMERCIAL PROPERTY COVERAGE PART - DECLARATIONS
Renewal of Policy No. CP-4471-88209

POLICY NUMBER: CP-4471-88210
POLICY PERIOD: From 03/01/2025 to 03/01/2026 at 12:01 A.M. Standard Time

NAMED INSURED:      Harborline Cold Storage & Logistics, LLC
MAILING ADDRESS:    2870 Patapsco Industrial Parkway, Suite 210
                    Baltimore, MD 21226

PRODUCER:           Talbot & Rennick Insurance Brokers, Inc.
                    Producer Code 09-33412
                    1155 Light Street, 6th Floor, Baltimore, MD 21230
"""


class TestPolicyNumber:
    def test_the_labelled_number_is_read(self) -> None:
        assert read_policy_facts(DECLARATIONS).policy_number == "CP-4471-88210"

    def test_a_renewal_reference_is_not_read_as_the_policy_number(self) -> None:
        """`Renewal of Policy No. CP-4471-88209` sits two lines above the real number.

        It is one digit from it, so a reader that takes the first reference in the
        document silently binds every claim on this policy to last year's term.
        """
        facts = read_policy_facts(DECLARATIONS)

        assert facts.policy_number == "CP-4471-88210"
        assert facts.policy_number != "CP-4471-88209"

    def test_punctuation_is_preserved_and_spacing_is_not(self) -> None:
        """Stored as the document rendered it.

        Comparison strips punctuation at the point of comparison — that is
        `normalise_reference`'s job — so stripping it here would lose the form an
        officer reads on screen.
        """
        facts = read_policy_facts("POLICY NUMBER:  CP - 4471 - 88210\n")

        assert facts.policy_number == "CP-4471-88210"

    def test_the_filename_is_a_last_resort_and_says_so(self) -> None:
        facts = read_policy_facts(
            "A WORDING WITH NO DECLARATIONS PAGE\n",
            filename="POL-CP-4471-88210_Harborline_Commercial-Property.pdf",
        )

        assert facts.policy_number == "CP-4471-88210"
        assert facts.labels["policy_number"] == "filename"

    def test_a_number_in_the_document_beats_the_filename(self) -> None:
        facts = read_policy_facts(DECLARATIONS, filename="POL-XX-0000-00000_Wrong.pdf")

        assert facts.policy_number == "CP-4471-88210"
        assert facts.labels["policy_number"] == "policy number"

    def test_no_number_anywhere_is_none_rather_than_a_guess(self) -> None:
        assert read_policy_facts("Some prose with no reference in it.").policy_number is None


class TestInsured:
    def test_the_named_insured_is_read_with_its_corporate_form(self) -> None:
        """`Inc.` and `LLC` are part of the name.

        A first pass stripped the trailing full stop along with dot leaders and stored
        `Beacon Mechanical Services, Inc` — a name that no longer matches the book.
        """
        facts = read_policy_facts(DECLARATIONS)

        assert facts.insured_name == "Harborline Cold Storage & Logistics, LLC"

    def test_a_scheduled_list_of_insureds_reads_the_first_one(self) -> None:
        """A construction wording numbers its insureds and aligns their roles with dots.

        The marker has to be stripped before the split, or the split takes `1.` as the
        whole name and the reader reports no insured at all.
        """
        text = """
BUILDERS RISK POLICY
DECLARATIONS

POLICY NUMBER:  BR-3358-20471
NAMED INSUREDS (as their respective interests may appear):
    1.  Rivergate Development Partners, LLC ................ Project Owner / Developer
    2.  Vanterra Construction Group, LLC .................... General Contractor
"""
        facts = read_policy_facts(text)

        assert facts.insured_name == "Rivergate Development Partners, LLC"

    def test_a_mailing_address_label_is_not_read_as_the_insured(self) -> None:
        text = "NAMED INSURED:\nMAILING ADDRESS: 12 Dock Road, Leeds LS9 8AX\n"

        assert read_policy_facts(text).insured_name is None

    def test_additional_named_insureds_are_collected(self) -> None:
        """The party reporting a loss is very often not the first named insured.

        Comparing only against the first name scores a mismatch against the right
        policy, which is the error this list exists to prevent.
        """
        text = """
POLICY NUMBER:  IM-2298-66401
NAMED INSURED:  Ironbark Constructors, Inc.
ADDITIONAL NAMED INSUREDS:
                Ironbark Industrial Services, LLC
                Ironbark Equipment Leasing, LLC
MAILING ADDRESS: 7720 West Buckeye Road
"""
        facts = read_policy_facts(text)

        assert facts.insured_name == "Ironbark Constructors, Inc."
        assert facts.additional_insureds == [
            "Ironbark Industrial Services, LLC",
            "Ironbark Equipment Leasing, LLC",
        ]

    def test_a_scheduled_block_contributes_its_joint_names(self) -> None:
        """A construction policy does not write "additional named insureds".

        It numbers a block, and every entry is an insured. Reading only the first is the
        most damaging omission this module could make on a construction risk: the party
        reporting the loss is very often the contractor rather than the employer, so the
        insured signal scores a *mismatch* against the right policy and the confidence
        band is then capped by rule.
        """
        text = """
POLICY NUMBER:  BR-1147-30926
NAMED INSUREDS:
    Meridian Grid Holdings, LLC ......................... Project Owner
    Ironbark Constructors, Inc. ......................... General Contractor
    Ironbark Industrial Services, LLC ................... Affiliate
    All subcontractors of every tier, as their interests may appear

MAILING ADDRESS: 7720 West Buckeye Road, Phoenix, AZ 85043
"""
        facts = read_policy_facts(text)

        assert facts.insured_name == "Meridian Grid Holdings, LLC"
        assert facts.additional_insureds == [
            "Ironbark Constructors, Inc.",
            "Ironbark Industrial Services, LLC",
        ]

    def test_a_class_of_insureds_is_not_stored_as_a_party(self) -> None:
        """ "All subcontractors of every tier" matches nothing and dilutes every comparison."""
        text = (
            "POLICY NUMBER:  BR-3358-20471\n"
            "NAMED INSUREDS (as their respective interests may appear):\n"
            "    1.  Rivergate Development Partners, LLC ..... Project Owner\n"
            "    2.  Vanterra Construction Group, LLC ........ General Contractor\n"
            "    3.  Subcontractors of every tier, but only for their interest in materials and\n"
            "        temporary works forming part of the insured project, and only while at site.\n"
        )
        facts = read_policy_facts(text)

        assert facts.additional_insureds == ["Vanterra Construction Group, LLC"]

    def test_an_inline_insured_keeps_every_block_entry_as_a_joint_name(self) -> None:
        """Whether the block's first entry is the named insured depends on the label line.

        Getting it wrong silently deletes one real joint name per policy — which is why
        it is read from the label rather than assumed either way.
        """
        text = (
            "POLICY NUMBER:  CP-2210-55870\n"
            "NAMED INSURED:  Fairmount Textile Mills Holdings, Inc.\n"
            "                and all subsidiary companies,\n"
            "                including but not limited to:\n"
            "                    Fairmount Weaving Company, LLC\n"
            "                    Piedmont Dye & Finish, LLC\n"
        )
        facts = read_policy_facts(text)

        assert facts.insured_name == "Fairmount Textile Mills Holdings, Inc."
        assert "Fairmount Weaving Company, LLC" in facts.additional_insureds
        assert "Piedmont Dye & Finish, LLC" in facts.additional_insureds
        assert not any("including" in name for name in facts.additional_insureds)

    def test_a_role_annotation_does_not_disqualify_a_joint_name(self) -> None:
        text = (
            "POLICY NUMBER:  CP-7735-19042\n"
            "NAMED INSURED:  Sundale Property Group, LLC\n"
            "                Windrow Grove Apartments, LP\n"
            "                Sundale Residential Management, LLC (Managing Agent)\n"
        )
        facts = read_policy_facts(text)

        assert facts.additional_insureds == [
            "Windrow Grove Apartments, LP",
            "Sundale Residential Management, LLC (Managing Agent)",
        ]

    def test_an_explicit_additional_insured_label_wins_over_the_block(self) -> None:
        text = (
            "POLICY NUMBER:  IM-2298-66401\n"
            "NAMED INSURED:  Ironbark Constructors, Inc.\n"
            "ADDITIONAL NAMED INSUREDS:\n"
            "                Ironbark Equipment Leasing, LLC\n"
        )
        facts = read_policy_facts(text)

        assert facts.additional_insureds == ["Ironbark Equipment Leasing, LLC"]

    def test_a_producer_contact_line_does_not_become_the_broker_name(self) -> None:
        facts = read_policy_facts(DECLARATIONS)

        assert facts.broker_name == "Talbot & Rennick Insurance Brokers, Inc."
        assert "555" not in (facts.broker_name or "")


class TestPeriod:
    def test_the_period_line_is_read_month_first(self) -> None:
        start, end = read_policy_period("POLICY PERIOD: From 03/01/2025 to 03/01/2026 at 12:01")

        assert start == date(2025, 3, 1)
        assert end == date(2026, 3, 1)

    def test_an_issue_date_before_the_period_is_not_taken_as_the_term(self) -> None:
        """A declarations page carries an issue date, a retroactive date and a term.

        Taking "the first two dates in the document" reads whichever pair the layout
        happened to put first, which on a surplus-lines page is the wrong one.
        """
        text = """
ISSUED:         08/19/2024
POLICY PERIOD:  From 09/01/2024 to 09/01/2025, 12:01 A.M.
EXPIRING POLICY: CP-2210-55869
"""
        assert read_policy_period(text) == (date(2024, 9, 1), date(2025, 9, 1))

    @pytest.mark.parametrize(
        "line",
        [
            "POLICY PERIOD: From 2025-03-01 to 2026-03-01",
            "POLICY PERIOD: From 1 March 2025 to 1 March 2026",
            "POLICY PERIOD: From March 1, 2025 to March 1, 2026",
        ],
    )
    def test_the_written_date_conventions_are_all_read(self, line: str) -> None:
        assert read_policy_period(line) == (date(2025, 3, 1), date(2026, 3, 1))

    def test_a_reversed_period_is_ordered(self) -> None:
        """Whatever the page printed, the earlier date is the inception.

        A model constraint refuses `expiry < effective`, so a reader that passed the
        pair through unordered would fail the insert rather than the comparison.
        """
        start, end = read_policy_period("POLICY PERIOD: From 03/01/2026 to 03/01/2025")

        assert (start, end) == (date(2025, 3, 1), date(2026, 3, 1))

    def test_no_dates_is_a_pair_of_nones(self) -> None:
        assert read_policy_period("No period stated anywhere.") == (None, None)


class TestLineOfBusiness:
    def test_the_coverage_heading_decides_the_line(self) -> None:
        assert read_line_of_business(DECLARATIONS) == LineOfBusiness.PROPERTY

    def test_an_exclusion_mentioning_construction_does_not_reclassify_a_property_policy(
        self,
    ) -> None:
        """The heading says what a policy *is*; the body mentions what it is not.

        A property wording's builder's-risk exclusion contains the phrase "course of
        construction", and reading the whole page at once let that exclusion decide the
        line — which filed a manufacturing property policy as construction and then
        filtered it out of every property notice's candidate set.
        """
        text = (
            "MANUFACTURING PROPERTY POLICY - DECLARATIONS\n"
            "POLICY NUMBER: CP-2210-55870\n" + "filler line\n" * 60 + "This policy does not "
            "cover property in the course of construction.\n"
        )

        assert read_line_of_business(text, policy_number="CP-2210-55870") == (
            LineOfBusiness.PROPERTY
        )

    def test_a_companion_floater_naming_its_cgl_stays_inland_marine(self) -> None:
        """Position settles it, not a fixed order of lines.

        An equipment floater's declarations name the CGL it sits beside. Ranking by a
        fixed priority made whichever line came first in the table win.
        """
        text = (
            "COMBINED CONTRACTORS EQUIPMENT AND INSTALLATION FLOATER - INLAND MARINE\n"
            "Companion to Commercial General Liability policy GL-8804-27153\n"
        )

        assert read_line_of_business(text) == LineOfBusiness.MARINE

    def test_the_prefix_is_a_fallback_when_no_phrase_is_found(self) -> None:
        assert read_line_of_business("Nothing descriptive here.", policy_number="GL-1-2") == (
            LineOfBusiness.LIABILITY
        )

    def test_an_unrecognisable_wording_is_none_not_unknown(self) -> None:
        """`None` means "do not filter on this"; `UNKNOWN` is a value to filter *on*."""
        assert read_line_of_business("Nothing descriptive here.") is None


class TestLocations:
    def test_a_naic_number_is_not_read_as_a_postcode(self) -> None:
        """A bare five digits is the worst pattern this module could carry.

        A declarations page is full of five-digit numbers that are not postcodes — the
        NAIC number, a producer code, half a policy number — and each would become a
        location token some notice eventually collides with.
        """
        facts = read_policy_facts(DECLARATIONS)

        assert "41783" not in facts.postcodes  # NAIC
        assert "88209" not in facts.postcodes  # last year's policy number
        assert "21226" in facts.postcodes  # the mailing address

    def test_an_equipment_serial_is_not_read_as_a_uk_postcode(self) -> None:
        facts = read_policy_facts(
            "POLICY NUMBER: IM-2298-66401\nEQUIPMENT: Komatsu PC290LC excavator\n"
        )

        assert facts.postcodes == []

    def test_address_lines_are_collected_for_overlap_scoring(self) -> None:
        facts = read_policy_facts(DECLARATIONS)

        assert any("Patapsco" in line for line in facts.locations)

    def test_the_carriers_own_office_is_not_a_scheduled_premises(self) -> None:
        """The masthead address is the insurer's, and it was reaching the schedule.

        Not a cosmetic problem. `risk_location` is an *identifying* signal in
        `policy_matching`, so a location that agrees is enough to carry a wording past
        the rejection gate — and every wording in the corpus carried its carrier's head
        office as its first premises. A loss at the insurer's own front door scored a
        `strong` match against two policies, each explaining itself with the sentence
        "the loss postcode is a premises scheduled on this wording".
        """
        facts = read_policy_facts(DECLARATIONS)

        assert "21202" not in facts.postcodes
        assert not any("Chesapeake" in line for line in facts.locations)

    def test_the_producers_address_is_not_a_scheduled_premises(self) -> None:
        """The same rule one block down: a broker's office is not a risk location."""
        facts = read_policy_facts(DECLARATIONS)

        assert "21230" not in facts.postcodes
        assert not any("Light Street" in line for line in facts.locations)

    def test_the_insureds_mailing_address_leads_the_schedule(self) -> None:
        """What the exclusion is *for*: the first premises is now the insured's.

        The card shows the first entry. Before the carrier's lines were excluded it
        showed the insurer's head office, which is both wrong and the kind of wrong
        nobody reports — it looks like an address because it is one.
        """
        facts = read_policy_facts(DECLARATIONS)

        assert facts.postcodes[0] == "21226"
        assert "Patapsco" in facts.locations[0]

    def test_a_page_with_no_policy_number_line_keeps_every_address(self) -> None:
        """The masthead is located by the policy-number line. With none, exclude nothing.

        A page this reader cannot navigate keeps the behaviour it had rather than having
        its opening lines discarded on a guess about a layout nobody has seen — the same
        direction to fail in that the rest of this module takes.
        """
        facts = read_policy_facts(
            "SCHEDULE OF PREMISES\n"
            "1 Loading Dock Road, Baltimore, MD 21226\n"
            "2 Cold Store Lane, New Castle, DE 19720\n"
        )

        assert facts.postcodes == ["21226", "19720"]


class TestWholeCorpus:
    """Every wording in `policy/_ground_truth` reads its own identity.

    The corpus is the point: twelve wordings from eight different fictional carriers,
    each with its own layout. A reader tuned to one page shape passes the unit cases
    above and fails here, which is exactly the failure worth catching before an
    administrator loads a book.
    """

    @pytest.mark.skipif(
        not GROUND_TRUTH.exists(), reason="the synthetic policy corpus is not checked out"
    )
    def test_every_wording_yields_its_number_insured_line_and_term(self) -> None:
        sources = sorted(GROUND_TRUTH.glob("POL-*.txt"))
        assert len(sources) >= 12

        for path in sources:
            facts = read_policy_facts(path.read_text(), filename=path.name.replace(".txt", ".pdf"))
            expected_number = path.name.removeprefix("POL-").split("_")[0]

            assert facts.policy_number == expected_number, path.name
            assert facts.insured_name, path.name
            assert facts.line_of_business is not None, path.name
            assert facts.effective_date is not None, path.name
            assert facts.expiry_date is not None, path.name
            assert facts.expiry_date > facts.effective_date, path.name
            assert facts.is_empty is False, path.name

            # No joint name may be a clause continuation or a class of insureds. This
            # is the assertion that catches a wrapped qualifying clause being stored as
            # a party, which dilutes every insured comparison the policy takes part in.
            for name in facts.additional_insureds:
                assert name[:1].isupper(), (path.name, name)
                assert "subcontractor" not in name.lower(), (path.name, name)
                assert len(name) < 90, (path.name, name)

    @pytest.mark.skipif(
        not GROUND_TRUTH.exists(), reason="the synthetic policy corpus is not checked out"
    )
    def test_the_lines_read_match_the_corpus_mix(self) -> None:
        """Four property, three construction, three liability, two inland marine.

        Pinned as a distribution rather than per file, because the useful assertion is
        that the reader does not collapse a whole line into another — which is what a
        phrase table with the wrong precedence does.
        """
        counts: dict[str, int] = {}
        for path in sorted(GROUND_TRUTH.glob("POL-*.txt")):
            facts = read_policy_facts(path.read_text(), filename=path.name.replace(".txt", ".pdf"))
            key = facts.line_of_business.value if facts.line_of_business else "none"
            counts[key] = counts.get(key, 0) + 1

        assert counts == {"property": 4, "construction": 3, "liability": 3, "marine": 2}

    @pytest.mark.skipif(
        not GROUND_TRUTH.exists(), reason="the synthetic policy corpus is not checked out"
    )
    def test_no_wording_schedules_a_party_address_as_a_premises(self) -> None:
        """The corpus assertion behind the exclusion, and the reason it is needed.

        Every one of the twelve carried its carrier's office as its first premises, and
        two of them carried the same one — the two Front Range policies both listed
        1801 Broadway, Denver. So the defect was not one page's odd layout: it was the
        universal shape of a declarations page, which leads with the carrier's own
        identity block. Pinned across the corpus because a per-file test would have
        passed on eleven of them and still left the rule unstated.
        """
        markers = ("home office", "underwriting office", "producer", "naic", "reciprocal insurer")
        for path in sorted(GROUND_TRUTH.glob("POL-*.txt")):
            facts = read_policy_facts(path.read_text(), filename=path.name.replace(".txt", ".pdf"))

            # Still a real schedule: excluding the parties must not empty it. One is
            # the floor rather than two because a single-site insured is a real thing —
            # the roofing contractor in this corpus has one yard, and asserting two
            # would be pinning the corpus rather than the rule.
            assert facts.locations, path.name
            assert facts.postcodes, path.name

            for line in facts.locations:
                lowered = line.lower()
                for marker in markers:
                    assert marker not in lowered, (path.name, line)


class TestMetadataShape:
    def test_the_metadata_bag_carries_everything_not_promoted(self) -> None:
        facts = read_policy_facts(DECLARATIONS)
        bag = facts.as_metadata

        assert set(bag) == {"additional_insureds", "postcodes", "locations", "limits", "labels"}
        assert bag["labels"]["policy_number"] == "policy number"

    def test_an_empty_document_is_reported_as_empty(self) -> None:
        assert PolicyFacts().is_empty is True
        assert read_policy_facts("").is_empty is True
