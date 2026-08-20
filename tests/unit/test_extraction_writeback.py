"""The notification body as a document, and the claim-record write-back.

Two seams, tested together because they are the two places the generic
extraction layer touches FNOL. Both are the kind of code that is easy to get
subtly wrong and hard to notice: a body document created twice, a correction
overwritten, a currency applied after the amount it was meant to qualify.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.enums import DocumentExtractionStatus, DocumentSource, FieldSource
from app.models.extraction import ExtractedValue
from app.services.fnol.adapter import (
    CURRENCY_FIELD_KEY,
    FNOL_WRITEBACK,
    PARTIES_FIELD_KEY,
    FNOLWriteBackAdapter,
)
from app.services.fnol.body import (
    BODY_DOCUMENT_FILENAME,
    BODY_DOCUMENT_SOURCE,
    NotificationBodyDocumentService,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeCase:
    def __init__(self, body: str | None = "A broker writes about water damage.") -> None:
        self.id = uuid.uuid4()
        self.reference = "FNOL-2026-000123"
        self.source_body = body
        self.currency = "GBP"
        #: The notice's own date, which every temporal reading is resolved
        #: against. A case without one cannot answer "what does 'Friday' mean".
        self.received_at = datetime(2026, 3, 16, 13, 22, tzinfo=UTC)
        self.extraction_confidence: float | None = None
        # Attributes the write-back table addresses.
        self.policy_number: str | None = None
        self.insured_name: str | None = None
        self.date_of_loss: Any = None
        self.injuries: int | None = None
        self.estimated_loss_minor: int | None = None
        self.business_interruption: bool = False
        self.reporter_email: str | None = None


class FakeField:
    def __init__(self, *, human_modified: bool = False, confidence: float | None = None) -> None:
        self.human_modified = human_modified
        self.confidence = confidence


class FakeFNOLRepository:
    """Enough of `FNOLRepository` for the two collaborators under test."""

    def __init__(self, documents: list[Any] | None = None) -> None:
        self.documents = documents or []
        self.fields: dict[str, FakeField] = {}
        self.field_calls: list[dict[str, Any]] = []
        self.parties: list[dict[str, Any]] = []
        self.flushed = 0

    async def flush(self) -> None:
        self.flushed += 1

    async def list_documents(self, case_id: uuid.UUID) -> list[Any]:
        del case_id
        return self.documents

    def add_document(self, document: Any) -> Any:
        self.documents.append(document)
        return document

    async def upsert_field(self, case_id: uuid.UUID, **kwargs: Any) -> FakeField:
        del case_id
        self.field_calls.append(kwargs)
        existing = self.fields.get(kwargs["field_path"])
        if existing is None:
            existing = FakeField(confidence=kwargs.get("confidence"))
            self.fields[kwargs["field_path"]] = existing
        elif not existing.human_modified:
            existing.confidence = kwargs.get("confidence")
        return existing

    async def upsert_party(self, case_id: uuid.UUID, **kwargs: Any) -> Any:
        del case_id
        self.parties.append(kwargs)
        return kwargs


class FakeDocumentService:
    def __init__(self, *, fail: bool = False) -> None:
        self.stored: dict[str, bytes] = {}
        self._fail = fail

    async def store(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        if self._fail:
            raise RuntimeError("the bucket is unreachable")
        self.stored[key] = content


def make_value(
    key: str,
    value: str | None,
    *,
    data_type: str = "string",
    confidence: float | None = 0.9,
    human: bool = False,
    typed: Any = None,
) -> ExtractedValue:
    row = ExtractedValue(
        fnol_case_id=uuid.uuid4(),
        schema_id=uuid.uuid4(),
        field_key=key,
        label=key.split(".")[-1].replace("_", " ").title(),
        group_label="Fields",
        data_type=data_type,
        value_text=value,
        value_json=typed,
        confidence=confidence,
        human_modified=human,
        source=FieldSource.HUMAN if human else FieldSource.AI,
    )
    row.id = uuid.uuid4()
    return row


# ---------------------------------------------------------------------------
# The notification body as a document
# ---------------------------------------------------------------------------


class TestNotificationBodyDocument:
    async def test_the_body_becomes_a_document_with_page_offsets(self) -> None:
        """Page offsets are what make a body value *locatable*, not just citable.

        Without a page span the evidence endpoint can resolve a chunk but not a
        page, and the viewer has nothing to scroll to.
        """
        repository = FakeFNOLRepository()
        documents = FakeDocumentService()
        service = NotificationBodyDocumentService(repository, documents)  # type: ignore[arg-type]
        case = FakeCase()

        document = await service.ensure(case)  # type: ignore[arg-type]

        assert document is not None
        assert document.source == BODY_DOCUMENT_SOURCE
        assert document.filename == BODY_DOCUMENT_FILENAME
        assert document.content_type == "text/plain"
        assert document.extraction_status == DocumentExtractionStatus.EXTRACTED
        assert document.extracted_text == case.source_body
        assert document.page_offsets == [[0, len(case.source_body or "")]]
        assert document.text_extractor == "notification_body"
        assert len(documents.stored) == 1

    async def test_an_unchanged_body_is_not_written_again(self) -> None:
        repository = FakeFNOLRepository()
        documents = FakeDocumentService()
        service = NotificationBodyDocumentService(repository, documents)  # type: ignore[arg-type]
        case = FakeCase()

        first = await service.ensure(case)  # type: ignore[arg-type]
        second = await service.ensure(case)  # type: ignore[arg-type]

        assert first is second
        assert len(documents.stored) == 1
        assert len(repository.documents) == 1

    async def test_a_changed_body_replaces_the_document_and_invalidates_its_index(self) -> None:
        """A rewritten body has to be re-chunked, or its passages quote text that has gone."""
        repository = FakeFNOLRepository()
        documents = FakeDocumentService()
        service = NotificationBodyDocumentService(repository, documents)  # type: ignore[arg-type]
        case = FakeCase()

        original = await service.ensure(case)  # type: ignore[arg-type]
        assert original is not None
        original.index_fingerprint = "stale"
        original.extraction_signature = "stale"
        original.index_attempts = 2

        case.source_body = "A corrected account of what happened."
        updated = await service.ensure(case)  # type: ignore[arg-type]

        assert updated is original
        assert updated.extracted_text == case.source_body
        assert updated.index_fingerprint is None
        assert updated.extraction_signature is None
        assert updated.index_attempts == 0
        assert len(repository.documents) == 1

    async def test_a_case_with_no_body_gets_no_document(self) -> None:
        service = NotificationBodyDocumentService(
            FakeFNOLRepository(),  # type: ignore[arg-type]
            FakeDocumentService(),  # type: ignore[arg-type]
        )
        assert await service.ensure(FakeCase(body="   ")) is None  # type: ignore[arg-type]
        assert await service.ensure(FakeCase(body=None)) is None  # type: ignore[arg-type]

    async def test_storage_being_down_degrades_rather_than_failing_the_notice(self) -> None:
        repository = FakeFNOLRepository()
        service = NotificationBodyDocumentService(
            repository,  # type: ignore[arg-type]
            FakeDocumentService(fail=True),  # type: ignore[arg-type]
        )
        assert await service.ensure(FakeCase()) is None  # type: ignore[arg-type]
        assert repository.documents == []

    async def test_the_body_is_matched_by_source_not_by_filename(self) -> None:
        """Or a release that renamed the file would create a second body document."""

        class OldBody:
            id = uuid.uuid4()
            filename = "an-older-release-called-it-this.txt"
            source = DocumentSource.NOTIFICATION_BODY
            checksum_sha256 = "different"

        repository = FakeFNOLRepository(documents=[OldBody()])
        service = NotificationBodyDocumentService(
            repository,  # type: ignore[arg-type]
            FakeDocumentService(),  # type: ignore[arg-type]
        )
        await service.ensure(FakeCase())  # type: ignore[arg-type]
        assert len(repository.documents) == 1
        assert repository.documents[0].filename == BODY_DOCUMENT_FILENAME


# ---------------------------------------------------------------------------
# The claim-record write-back
# ---------------------------------------------------------------------------


class TestWriteBack:
    async def test_a_mapped_value_reaches_the_case_column_and_the_field_row(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [make_value("policy.policy_number", "CP-2026-4471")],
        )

        assert case.policy_number == "CP-2026-4471"
        assert repository.field_calls[0]["field_path"] == "policy.policy_number"
        assert repository.field_calls[0]["section"] == "policy"

    async def test_a_value_is_parsed_into_the_column_s_own_type(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [
                make_value("loss.injuries", "3", data_type="integer"),
                make_value("financial.estimated_loss", "GBP 128,000", data_type="money"),
                make_value("loss.business_interruption", "yes", data_type="boolean"),
                make_value("notification.reporter_email", "A Broker <a@b.test>"),
            ],
        )

        assert case.injuries == 3
        assert case.estimated_loss_minor == 12_800_000
        assert case.business_interruption is True
        assert case.reporter_email == "a@b.test"

    async def test_a_field_with_no_column_still_gets_a_provenance_row(self) -> None:
        """A dataset question with no home on the claim record is not an error.

        It is extracted, stored, cited and shown. Refusing to record it would be
        the module quietly deciding which questions a desk may ask.
        """
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]

        await adapter.apply(
            FakeCase(),  # type: ignore[arg-type]
            [make_value("documents.supporting", "Photographs and a repair quotation")],
        )
        assert repository.field_calls[0]["field_path"] == "documents.supporting"

    async def test_a_field_the_table_does_not_know_is_skipped_silently(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]

        await adapter.apply(
            FakeCase(),  # type: ignore[arg-type]
            [make_value("vessel.imo_number", "IMO 9074729")],
        )
        assert repository.field_calls == []

    async def test_a_model_answer_arriving_after_a_correction_does_not_apply(self) -> None:
        """The field row is the authority on whether a person has spoken."""
        repository = FakeFNOLRepository()
        repository.fields["policy.policy_number"] = FakeField(human_modified=True)
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()
        case.policy_number = "CP-CORRECTED"

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [make_value("policy.policy_number", "CP-WRONG")],
        )
        assert case.policy_number == "CP-CORRECTED"

    async def test_the_currency_is_applied_before_the_amounts_it_qualifies(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [
                make_value("financial.estimated_loss", "128,000", data_type="money"),
                make_value(CURRENCY_FIELD_KEY, "usd"),
            ],
        )
        assert case.currency == "USD"

    async def test_a_currency_that_is_not_a_code_is_ignored(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()
        await adapter.apply(
            case,  # type: ignore[arg-type]
            [make_value(CURRENCY_FIELD_KEY, "pounds sterling")],
        )
        assert case.currency == "GBP"

    async def test_the_parties_field_becomes_party_rows(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]

        result = await adapter.apply(
            FakeCase(),  # type: ignore[arg-type]
            [
                make_value(
                    PARTIES_FIELD_KEY,
                    None,
                    data_type="json",
                    typed=[
                        {
                            "role": "witness",
                            "name": "Dara Okonjo",
                            "organisation": None,
                            "email": "dara@example.test",
                            "phone": None,
                        },
                        {"role": "policyholder", "name": "Harborview Logistics Limited"},
                    ],
                )
            ],
        )

        assert result.parties_written == 2
        assert repository.parties[0]["name"] == "Dara Okonjo"
        assert repository.parties[0]["role"] == "witness"
        assert repository.parties[0]["email"] == "dara@example.test"
        # "policyholder" is normalised onto a known role rather than inventing one.
        assert repository.parties[1]["role"] == "insured"

    async def test_a_malformed_parties_answer_costs_the_parties_and_nothing_else(self) -> None:
        """Thirty scalar fields must not be lost to one badly-shaped list."""
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        result = await adapter.apply(
            case,  # type: ignore[arg-type]
            [
                make_value(PARTIES_FIELD_KEY, "not a list", data_type="json", typed="not a list"),
                make_value("policy.policy_number", "CP-1"),
            ],
        )
        assert result.parties_written == 0
        assert case.policy_number == "CP-1"

    async def test_a_party_with_no_name_is_not_written(self) -> None:
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        result = await adapter.apply(
            FakeCase(),  # type: ignore[arg-type]
            [
                make_value(
                    PARTIES_FIELD_KEY,
                    None,
                    data_type="json",
                    typed=[{"role": "witness", "name": "  "}, "a bare string"],
                )
            ],
        )
        assert result.parties_written == 0

    async def test_the_raw_loss_date_is_carried_through_unparsed(self) -> None:
        """The future-loss-date exception is raised from the string, not the column.

        A date the parser refused still has to produce the right exception, which
        it cannot do if only the parsed value survives this far.
        """
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case_without_a_date = FakeCase()
        result = await adapter.apply(
            case_without_a_date,  # type: ignore[arg-type]
            [make_value("loss.date_of_loss", "08 March 2099", data_type="datetime")],
        )
        assert result.raw_loss_date == "08 March 2099"
        assert case_without_a_date.date_of_loss is None

    async def test_the_coerced_instant_is_what_lands_on_the_claim_record(self) -> None:
        """One reading of the date, not two.

        The extraction resolved the words against the notice's date, stored the
        result and told the officer how it got there. Re-reading the text here
        would be a second opinion nobody asked for and nobody can see — and the
        column and the review screen would disagree the first time the two
        readings differed.
        """
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [
                make_value(
                    "loss.date_of_loss",
                    "overnight on Friday",
                    data_type="datetime",
                    typed="2026-03-13T22:00:00+00:00",
                )
            ],
        )
        assert case.date_of_loss == datetime(2026, 3, 13, 22, 0, tzinfo=UTC)

    async def test_a_value_with_no_coerced_form_is_read_against_the_notice(self) -> None:
        """The path a row written by an earlier run takes.

        `value_json` is empty on a value extracted before dates were resolved, so
        the text is re-read here — and against the same notice date, so the answer
        is the one the extraction would have given. This case's notice arrived on
        Monday 16 March 2026, which makes "yesterday" the Sunday.
        """
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [make_value("loss.date_of_loss", "yesterday", data_type="datetime")],
        )
        assert case.date_of_loss == datetime(2026, 3, 15, tzinfo=UTC)

    async def test_overall_confidence_averages_the_fields_that_answered(self) -> None:
        """Not all of them.

        A dataset with forty optional fields of which six apply is not a
        15%-confident reading. Completeness is the number that should fall when
        fields are missing, and it is measured separately and deterministically.
        """
        repository = FakeFNOLRepository()
        adapter = FNOLWriteBackAdapter(repository)  # type: ignore[arg-type]
        case = FakeCase()

        await adapter.apply(
            case,  # type: ignore[arg-type]
            [
                make_value("policy.policy_number", "CP-1", confidence=1.0),
                make_value("policy.insured_name", "Acme", confidence=0.5),
                make_value("loss.cause_of_loss", None, confidence=None),
            ],
        )
        assert case.extraction_confidence == pytest.approx(0.75)

    def test_every_mapped_attribute_exists_on_the_case_model(self) -> None:
        """A typo in the table would silently stop a field reaching the claim record."""
        from app.models.fnol import FNOLCase

        for key, mapping in FNOL_WRITEBACK.items():
            if mapping.attribute is None:
                continue
            assert hasattr(FNOLCase, mapping.attribute), (
                f"{key} maps to FNOLCase.{mapping.attribute}, which does not exist"
            )
