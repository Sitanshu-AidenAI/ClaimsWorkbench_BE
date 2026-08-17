"""The demo document set, held to what the demo claims about it.

`app/db/demo.py` exists to show the pipeline working on realistic files. That is
only a demonstration if the files really carry the facts, really read per page,
and really resolve to a rectangle on the page a value is printed on — and none of
that is guaranteed by the generator running without error. A `_pair()` call that
silently drew off the bottom of the page would produce a valid PDF, a passing
build, and a demo that could not cite anything.

No database and no model provider: these are properties of the bytes on disk and
of the readers, so they run in the ordinary unit suite where a regression in a
reader shows up against known content.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import pytest

from app.services.documents.text import extract_text
from app.services.intelligence.chunking import chunk_document, regions_from_pages
from app.services.intelligence.highlight import resolve_pdf_rects

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo-data" / "document-intelligence"

LOSS_NOTICE = DEMO_DIR / "harbourline-loss-notice.pdf"
POLICY_SCHEDULE = DEMO_DIR / "harbourline-policy-schedule.pdf"
SURVEY_REPORT = DEMO_DIR / "harbourline-survey-report.pdf"
DAMAGE_SCHEDULE = DEMO_DIR / "harbourline-damage-schedule.csv"
BROKER_EMAIL = DEMO_DIR / "broker-notification.eml"

#: The facts the scenario is built on. Every document that mentions one must
#: agree with every other, because a review screen showing a policy number from
#: the schedule that differs from the notice is showing a bug in this fixture
#: rather than a disagreement worth an officer's time.
POLICY_NUMBER = "MAR-2026-77413"
INSURED = "Ravensgate Marine Logistics Limited"
ESTIMATED_LOSS = "GBP 486,500"
REPAIR_ESTIMATE = "GBP 312,480"


def _pages(path: Path, content_type: str = "application/pdf") -> list[str]:
    result = extract_text(content_type, path.read_bytes())
    assert result.status.value == "extracted", f"{path.name} could not be read"
    return [page.text for page in result.pages]


class TestTheFilesAreThere:
    def test_every_document_the_demo_attaches_exists(self) -> None:
        """A missing file makes the demo fail at the point it looks most real."""
        for path in (LOSS_NOTICE, POLICY_SCHEDULE, SURVEY_REPORT, DAMAGE_SCHEDULE, BROKER_EMAIL):
            assert path.is_file(), f"{path.name} is missing — run `make demo-documents`"
            assert path.stat().st_size > 0


class TestTheyReadPerPage:
    """Page attribution is what a citation is made of.

    A reader that returned the right text as one undivided block would pass every
    extraction test in the suite and make every page number in the product wrong.
    """

    def test_the_loss_notice_reads_as_two_pages(self) -> None:
        pages = _pages(LOSS_NOTICE)
        assert len(pages) == 2

    def test_the_survey_report_reads_as_three_pages(self) -> None:
        pages = _pages(SURVEY_REPORT)
        assert len(pages) == 3

    def test_the_policy_and_the_loss_are_on_different_pages_of_the_notice(self) -> None:
        """The property the demo is built to show.

        Page 1 answers "under what policy" and page 2 answers "what happened".
        A field-level citation that opens the right one is doing something a
        whole-document answer cannot.
        """
        first, second = _pages(LOSS_NOTICE)

        # Page 1 is the policy and the people.
        assert POLICY_NUMBER in first
        assert INSURED in first
        assert "Policy type" in first
        assert "Date of loss" not in first
        assert ESTIMATED_LOSS not in first

        # Page 2 is the loss and the money. It repeats the policy number in its
        # running header, the way a real continuation page does — which is
        # exactly why a citation has to name a page rather than a document.
        assert "Date of loss" in second
        assert ESTIMATED_LOSS in second
        assert "Policy type" not in second

    def test_the_surveyors_figure_is_on_the_last_page_only(self) -> None:
        """The case for citing rather than searching.

        The estimated loss appears in the covering email *and* on page 3 of the
        survey. A citation naming page 3 is demonstrably the passage that was
        read, not the first place the string occurs in the case file.
        """
        pages = _pages(SURVEY_REPORT)
        assert ESTIMATED_LOSS in pages[2]
        assert ESTIMATED_LOSS not in pages[0]
        assert ESTIMATED_LOSS not in pages[1]


class TestTheDocumentsAgree:
    def test_the_policy_number_is_the_same_everywhere_it_appears(self) -> None:
        notice = "\n".join(_pages(LOSS_NOTICE))
        schedule = "\n".join(_pages(POLICY_SCHEDULE))
        survey = "\n".join(_pages(SURVEY_REPORT))
        email = extract_text("message/rfc822", BROKER_EMAIL.read_bytes()).text

        for name, text in (
            ("notice", notice),
            ("schedule", schedule),
            ("survey", survey),
            ("email", email),
        ):
            assert POLICY_NUMBER in text, f"the {name} does not carry the policy number"

    def test_the_schedule_totals_the_repair_estimate_the_other_documents_state(self) -> None:
        """The figure a reviewer would check by hand.

        A schedule whose lines do not add up to the figure quoted in the survey
        is the single most likely thing to make a demo look wrong to an insurer
        in the room.
        """
        rows = list(csv.reader(io.StringIO(DAMAGE_SCHEDULE.read_text())))
        line_items = rows[1:-1]
        stated_total = rows[-1][5]

        assert sum(int(row[5]) for row in line_items) == int(stated_total)
        assert f"GBP {int(stated_total):,}" == REPAIR_ESTIMATE

    def test_the_email_states_the_figures_without_being_their_source(self) -> None:
        """The email quotes the survey. It is not where the figures came from."""
        email = extract_text("message/rfc822", BROKER_EMAIL.read_bytes()).text
        assert ESTIMATED_LOSS in email
        assert REPAIR_ESTIMATE in email


class TestEveryPack:
    """The same properties, held across all six notification packs.

    The five packs built by `scripts/build_demo_packs.py` share one generator, so
    a layout change breaks all of them at once — which is the argument for
    checking them all rather than trusting the one that gets opened most. Driven
    off the generator's own scenario list, so a pack added there is covered here
    without anybody remembering to add it.
    """

    @staticmethod
    def _scenarios() -> list[Any]:
        # Imported lazily and by path: `scripts/` is not a package, and this is
        # the only test that needs it.
        import sys

        scripts = str(Path(__file__).resolve().parents[2] / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from build_demo_packs import SCENARIOS

        return list(SCENARIOS)

    def test_there_are_five_generated_packs_besides_the_marine_one(self) -> None:
        assert len(self._scenarios()) == 5

    def test_every_pack_has_its_email_and_four_attachments(self) -> None:
        for scenario in self._scenarios():
            directory = scenario.directory
            assert (directory / "broker-notification.eml").is_file(), scenario.slug
            assert (directory / "README.md").is_file(), scenario.slug
            for name in (
                scenario.notice_file,
                scenario.policy_file,
                scenario.report_file,
                scenario.schedule_file,
            ):
                assert (directory / name).is_file(), f"{scenario.slug}/{name}"

    def test_every_pack_reads_with_the_page_counts_it_promises(self) -> None:
        for scenario in self._scenarios():
            for name, expected in (
                (scenario.notice_file, 2),
                (scenario.policy_file, 1),
                (scenario.report_file, 3),
            ):
                pages = _pages(scenario.directory / name)
                assert len(pages) == expected, f"{scenario.slug}/{name} read as {len(pages)} pages"

    def test_every_pack_splits_the_policy_from_the_loss(self) -> None:
        """The property a per-field citation exists to demonstrate."""
        for scenario in self._scenarios():
            first, second = _pages(scenario.directory / scenario.notice_file)

            assert scenario.policy_number in first, scenario.slug
            assert "Policy type" in first, scenario.slug
            assert scenario.estimated_loss not in first, scenario.slug

            assert "Date of loss" in second, scenario.slug
            assert scenario.estimated_loss in second, scenario.slug
            assert "Policy type" not in second, scenario.slug

    def test_every_pack_states_the_quantum_on_the_last_page_of_its_report(self) -> None:
        """And nowhere earlier in that document, so a citation to it is specific."""
        for scenario in self._scenarios():
            pages = _pages(scenario.directory / scenario.report_file)
            assert scenario.estimated_loss in pages[2], scenario.slug
            assert scenario.estimated_loss not in pages[0], scenario.slug
            assert scenario.estimated_loss not in pages[1], scenario.slug

    def test_every_packs_documents_agree_on_the_policy_number(self) -> None:
        for scenario in self._scenarios():
            directory = scenario.directory
            texts = {
                "notice": "\n".join(_pages(directory / scenario.notice_file)),
                "policy": "\n".join(_pages(directory / scenario.policy_file)),
                "report": "\n".join(_pages(directory / scenario.report_file)),
                "email": extract_text(
                    "message/rfc822", (directory / "broker-notification.eml").read_bytes()
                ).text,
            }
            for where, text in texts.items():
                assert scenario.policy_number in text, f"{scenario.slug}: {where}"

    def test_every_packs_policy_document_uses_its_own_vocabulary(self) -> None:
        """What the dataset's aliases are for.

        The schedule says "Assured" or "Policyholder" where the notice says
        "Insured name". A pack whose documents all used the schema's own wording
        would make retrieval look better than it is.
        """
        for scenario in self._scenarios():
            policy = "\n".join(_pages(scenario.directory / scenario.policy_file))
            assert scenario.policy_insured_label in policy, scenario.slug
            assert "Insured name" not in policy, scenario.slug

    def test_every_packs_schedule_adds_up(self) -> None:
        for scenario in self._scenarios():
            rows = list(
                csv.reader(io.StringIO((scenario.directory / scenario.schedule_file).read_text()))
            )
            line_items, stated = rows[1:-1], rows[-1][-1]
            assert sum(int(row[-1]) for row in line_items) == int(stated), scenario.slug
            assert int(stated) == scenario.schedule_total, scenario.slug

    def test_every_packs_quantum_resolves_to_a_rectangle(self) -> None:
        """The last link in the chain, on every pack rather than the favourite one."""
        for scenario in self._scenarios():
            quote = f"Estimated loss: {scenario.estimated_loss}"
            rects, note = resolve_pdf_rects(
                (scenario.directory / scenario.report_file).read_bytes(),
                page_index=2,
                text=quote,
            )
            assert rects, f"{scenario.slug}: {quote!r} did not resolve ({note})"
            for rect in rects:
                assert rect.page_number == 3
                assert 0 <= rect.x0 < rect.x1 <= rect.page_width
                assert 0 <= rect.top < rect.bottom <= rect.page_height

    def test_no_pack_names_a_registrable_email_domain(self) -> None:
        """Fake data has to stay fake.

        `.example` is reserved by RFC 2606 and can never be registered, so a
        demo address can never reach a real inbox. A `.com` slipping into a
        fixture is a message sent to a stranger the first time somebody clicks it.
        """
        for scenario in self._scenarios():
            for path in scenario.directory.iterdir():
                if path.suffix.lower() not in {".eml", ".md", ".csv"}:
                    continue
                for match in re.findall(r"[\w.+-]+@[\w.-]+", path.read_text()):
                    assert match.endswith(".example"), f"{scenario.slug}/{path.name}: {match}"


class TestTheyCanBeCited:
    def test_the_notice_cuts_into_passages_that_stay_on_one_page(self) -> None:
        result = extract_text("application/pdf", LOSS_NOTICE.read_bytes())
        chunks, truncated = chunk_document(
            result.text,
            regions=regions_from_pages(result.page_offsets),
            target_chars=1200,
            overlap_chars=120,
            max_chunks=200,
        )

        assert chunks, "the notice produced no passages, so nothing about it is citable"
        assert not truncated
        # Every passage names the page it came from. A `None` here is a citation
        # that can be shown but not opened.
        assert all(chunk.page_number is not None for chunk in chunks)

    @pytest.mark.parametrize(
        ("page_index", "quote"),
        [
            (2, "Estimated loss: GBP 486,500"),
            (2, "Repair estimate: GBP 312,480"),
            (1, "Cause of loss: Heavy weather"),
        ],
    )
    def test_a_quote_resolves_to_a_rectangle_on_the_page_it_is_printed_on(
        self, page_index: int, quote: str
    ) -> None:
        """The last link in the chain, against real PDF word geometry.

        This is the assertion that fails if the generator's layout drifts, if the
        word matcher regresses, or if `pypdfium2` changes what it reports — all
        of which end the same way on screen: a page with no mark on it.
        """
        rects, note = resolve_pdf_rects(
            SURVEY_REPORT.read_bytes(), page_index=page_index, text=quote
        )

        assert rects, f"{quote!r} could not be located on page {page_index + 1}: {note}"
        assert note is None

        for rect in rects:
            assert rect.page_number == page_index + 1
            # Inside the page, and the right way up. A rectangle with `top`
            # greater than `bottom` draws as a zero-height line.
            assert 0 <= rect.x0 < rect.x1 <= rect.page_width
            assert 0 <= rect.top < rect.bottom <= rect.page_height

    def test_a_quote_that_is_not_on_the_page_is_reported_rather_than_guessed(self) -> None:
        """A near miss must not be drawn round the nearest thing to it."""
        rects, note = resolve_pdf_rects(
            SURVEY_REPORT.read_bytes(),
            page_index=0,
            text="Estimated loss: GBP 999,999",
        )

        assert list(rects) == []
        assert note is not None
