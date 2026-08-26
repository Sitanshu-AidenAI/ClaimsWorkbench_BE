"""Which documents get cited for a value, and which do not.

The rule under test throughout: a citation is written where the document's own
text contains the value, and nowhere else. Everything else here is a corollary of
that — the ordering a reviewer steps through, the cap, and the values distinctive
enough for a match to mean anything.

Model objects rather than fakes, unpersisted. The builder reads six attributes off
each and a database adds nothing to the properties being checked.
"""

from __future__ import annotations

import uuid

from app.domain.enums import DocumentSource
from app.models.extraction import ExtractedValue
from app.models.fnol import FNOLDocument, FNOLDocumentChunk
from app.services.extraction.corroborate import (
    ROLE_CORROBORATING,
    ROLE_PRIMARY,
    CitationBuilder,
    prepare,
)

NOTICE_TEXT = (
    "FIRST NOTIFICATION OF LOSS\n"
    "Policy number: CP-4471-88210\n"
    "Insured name: Harborline Cold Storage\n"
    "Date of loss: 10 January 2026\n"
    "Estimated loss: USD 3,700,000\n"
    "Injuries: 0\n"
)
REPORT_TEXT = (
    "REFRIGERATION ENGINEER'S REPORT\n"
    "Policy CP-4471-88210 — weld failure on the 4-inch liquid line.\n"
    "Incident date 2026-01-10, attended the following morning.\n"
)
SCHEDULE_TEXT = "line,description,amount\n1,Frozen product,3700000\n"
BODY_TEXT = "We are instructed to notify a claim under policy CP-4471-88210.\n"


def document(
    *,
    filename: str,
    text: str,
    source: str = DocumentSource.EMAIL_ATTACHMENT,
    pages: list[list[int]] | None = None,
    content_type: str = "application/pdf",
) -> FNOLDocument:
    row = FNOLDocument(
        filename=filename,
        content_type=content_type,
        source=source,
        extracted_text=text,
        page_offsets=pages,
    )
    row.id = uuid.uuid4()
    return row


def chunk(*, document_id: uuid.UUID, content: str, start: int = 0, page: int | None = 1):
    row = FNOLDocumentChunk(
        fnol_document_id=document_id,
        content=content,
        char_start=start,
        char_end=start + len(content),
        page_number=page,
        section_label="POLICY",
    )
    row.id = uuid.uuid4()
    return row


def value(
    *,
    text: str | None,
    quote: str | None = None,
    data_type: str = "string",
    typed: object = None,
    field_key: str = "policy.policy_number",
) -> ExtractedValue:
    row = ExtractedValue(
        field_key=field_key,
        label="Policy number",
        data_type=data_type,
        value_text=text,
        value_json=typed,
        quote=quote,
    )
    row.id = uuid.uuid4()
    return row


class TestPrepare:
    def test_attachments_are_ranked_before_the_notification_body(self) -> None:
        body = document(
            filename="notification-body.txt",
            text=BODY_TEXT,
            source=DocumentSource.NOTIFICATION_BODY,
        )
        report = document(filename="report.pdf", text=REPORT_TEXT)

        ordered = prepare([body, report])

        assert [item.filename for item in ordered] == ["report.pdf", "notification-body.txt"]

    def test_the_order_is_stable_across_calls(self) -> None:
        one = document(filename="b.pdf", text=REPORT_TEXT)
        two = document(filename="a.pdf", text=NOTICE_TEXT)

        assert [item.filename for item in prepare([one, two])] == ["a.pdf", "b.pdf"]
        assert [item.filename for item in prepare([two, one])] == ["a.pdf", "b.pdf"]

    def test_a_document_with_no_text_is_dropped(self) -> None:
        # An unreadable scan with no OCR. There is nothing to search, and a
        # citation with no offsets would point at nothing.
        assert prepare([document(filename="scan.pdf", text="")]) == []


class TestPrimaryCitation:
    def test_the_cited_passage_becomes_the_first_citation(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT, start=0)

        drafts = CitationBuilder().build(
            value(text="CP-4471-88210", quote="Policy number: CP-4471-88210"),
            cited=[passage],
            documents=prepare([notice]),
        )

        assert drafts[0].role == ROLE_PRIMARY
        assert drafts[0].rank == 0
        assert drafts[0].document_id == notice.id
        assert drafts[0].chunk_id == passage.id
        assert drafts[0].strategy == "chunk-grounded"
        # Narrowed to the model's quote rather than the whole passage.
        assert drafts[0].quote == "Policy number: CP-4471-88210"

    def test_the_page_comes_from_the_offsets_rather_than_the_passage(self) -> None:
        # The passage says page 1; the document's own page spans put the offset on
        # page 2, and the spans are the later, better answer.
        split = len(NOTICE_TEXT) // 2
        notice = document(
            filename="notice.pdf",
            text=NOTICE_TEXT,
            pages=[[0, split], [split, len(NOTICE_TEXT)]],
        )
        tail = NOTICE_TEXT[split:]
        passage = chunk(document_id=notice.id, content=tail, start=split, page=1)

        drafts = CitationBuilder().build(
            value(text="3,700,000", quote="Estimated loss: USD 3,700,000"),
            cited=[passage],
            documents=prepare([notice]),
        )

        assert drafts[0].page_number == 2

    def test_two_passages_of_one_document_are_one_citation(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        first = chunk(document_id=notice.id, content=NOTICE_TEXT[:40], start=0)
        second = chunk(document_id=notice.id, content=NOTICE_TEXT[40:], start=40)

        drafts = CitationBuilder().build(
            value(text="CP-4471-88210", quote="Policy number: CP-4471-88210"),
            cited=[first, second],
            documents=prepare([notice]),
        )

        assert [draft.document_id for draft in drafts] == [notice.id]

    def test_a_value_with_no_text_is_cited_nowhere(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)

        assert CitationBuilder().build(value(text=None), documents=prepare([notice])) == []


class TestCorroboration:
    def test_every_document_stating_the_value_is_cited(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)
        body = document(
            filename="notification-body.txt",
            text=BODY_TEXT,
            source=DocumentSource.NOTIFICATION_BODY,
            content_type="text/plain",
        )
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(text="CP-4471-88210", quote="Policy number: CP-4471-88210"),
            cited=[passage],
            documents=prepare([notice, report, body]),
        )

        assert [draft.role for draft in drafts] == [
            ROLE_PRIMARY,
            ROLE_CORROBORATING,
            ROLE_CORROBORATING,
        ]
        # Ranks are contiguous and in stepper order, attachment before body.
        assert [draft.rank for draft in drafts] == [0, 1, 2]
        assert drafts[1].document_id == report.id
        assert drafts[2].document_id == body.id

    def test_a_corroborating_citation_quotes_the_document_it_cites(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(text="CP-4471-88210", quote="Policy number: CP-4471-88210"),
            cited=[passage],
            documents=prepare([notice, report]),
        )

        found = drafts[1]
        assert found.chunk_id is None
        assert found.strategy == "document-search"
        assert found.quote == "CP-4471-88210"
        # The offsets are into the report, not the notice.
        assert REPORT_TEXT[found.char_start : found.char_end] == "CP-4471-88210"

    def test_a_date_written_another_way_is_still_found(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(
                text="10 January 2026",
                quote="Date of loss: 10 January 2026",
                data_type="date",
                typed="2026-01-10",
                field_key="loss.date_of_loss",
            ),
            cited=[passage],
            documents=prepare([notice, report]),
        )

        assert len(drafts) == 2
        assert drafts[1].quote == "2026-01-10"

    def test_an_amount_written_without_separators_is_still_found(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        schedule = document(filename="schedule.csv", text=SCHEDULE_TEXT, content_type="text/csv")
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(
                text="USD 3,700,000",
                quote="Estimated loss: USD 3,700,000",
                data_type="money",
                typed=370000000,
                field_key="financial.estimated_loss",
            ),
            cited=[passage],
            documents=prepare([notice, schedule]),
        )

        assert [draft.document_id for draft in drafts] == [notice.id, schedule.id]
        assert drafts[1].quote == "3700000"

    def test_a_document_that_does_not_state_the_value_is_not_cited(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(text="Harborline Cold Storage", quote="Insured name: Harborline Cold Storage"),
            cited=[passage],
            documents=prepare([notice, report]),
        )

        # The report never names the insured, so it is not offered as a source.
        assert [draft.document_id for draft in drafts] == [notice.id]

    def test_an_indistinctive_value_is_not_searched_for(self) -> None:
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)
        passage = chunk(document_id=notice.id, content=NOTICE_TEXT)

        drafts = CitationBuilder().build(
            value(
                text="0",
                quote="Injuries: 0",
                data_type="integer",
                typed=0,
                field_key="loss.injuries",
            ),
            cited=[passage],
            documents=prepare([notice, report]),
        )

        # "0" appears in the report's "4-inch" line and its date. Citing it there
        # would be a corroboration that proves nothing.
        assert len(drafts) == 1
        assert drafts[0].role == ROLE_PRIMARY

    def test_a_value_the_model_could_not_cite_still_gets_its_corroborations(self) -> None:
        # The model named a passage it was never shown, so there is no primary. The
        # documents that state the value are still worth offering — "we cannot say
        # which passage this was read from, but these two files state it".
        notice = document(filename="notice.pdf", text=NOTICE_TEXT)
        report = document(filename="report.pdf", text=REPORT_TEXT)

        drafts = CitationBuilder().build(
            value(text="CP-4471-88210"),
            cited=[],
            documents=prepare([notice, report]),
        )

        assert [draft.role for draft in drafts] == [ROLE_CORROBORATING, ROLE_CORROBORATING]
        assert [draft.rank for draft in drafts] == [0, 1]

    def test_the_cap_is_respected(self) -> None:
        documents = [document(filename=f"copy-{index}.pdf", text=NOTICE_TEXT) for index in range(8)]
        passage = chunk(document_id=documents[0].id, content=NOTICE_TEXT)

        drafts = CitationBuilder(max_citations=3).build(
            value(text="CP-4471-88210", quote="Policy number: CP-4471-88210"),
            cited=[passage],
            documents=prepare(documents),
        )

        assert len(drafts) == 3
        assert [draft.rank for draft in drafts] == [0, 1, 2]

    def test_no_documents_means_no_corroborations(self) -> None:
        assert CitationBuilder().build(value(text="CP-4471-88210")) == []
