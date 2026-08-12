"""Attachment validation and text extraction.

The two things this layer must get right are refusing what should not be stored
and never raising over what cannot be read — an unreadable survey report is a
normal Tuesday, not a failed request.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.domain.enums import DocumentExtractionStatus
from app.services.documents.text import extract_text
from app.services.documents.validation import (
    DocumentValidationError,
    document_kind,
    sanitise_filename,
    validate_upload,
)

MAX = 5_000_000


class TestFilenameSanitising:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("report .pdf", "report .pdf"),
            (r"C:\Users\bob\estimate.csv", "estimate.csv"),
            ("...hidden.txt", "hidden.txt"),
            ("", "attachment"),
        ],
    )
    def test_path_and_control_characters_are_removed(self, raw: str, expected: str) -> None:
        assert sanitise_filename(raw) == expected


class TestUploadValidation:
    def test_an_empty_file_is_refused(self) -> None:
        with pytest.raises(DocumentValidationError):
            validate_upload("report.pdf", b"", max_bytes=MAX)

    def test_a_file_over_the_limit_is_refused_with_the_limit_named(self) -> None:
        with pytest.raises(DocumentValidationError, match="MB limit"):
            validate_upload("report.txt", b"x" * 200, max_bytes=100)

    def test_an_unlisted_extension_is_refused(self) -> None:
        with pytest.raises(DocumentValidationError, match="not a file type"):
            validate_upload("payload.exe", b"anything", max_bytes=MAX)

    def test_an_executable_wearing_a_pdf_name_is_refused(self) -> None:
        with pytest.raises(DocumentValidationError, match="program"):
            validate_upload("invoice.pdf", b"MZ\x90\x00rest", max_bytes=MAX)

    def test_a_file_whose_bytes_contradict_its_name_is_refused(self) -> None:
        with pytest.raises(DocumentValidationError, match="does not contain the file type"):
            validate_upload("scan.png", b"just some text, not a png", max_bytes=MAX)

    def test_the_content_type_comes_from_the_extension_not_the_client(self) -> None:
        _, content_type = validate_upload(
            "note.txt", b"hello", max_bytes=MAX, declared_content_type="application/x-evil"
        )
        assert content_type == "text/plain"

    def test_a_valid_pdf_is_accepted(self) -> None:
        name, content_type = validate_upload("report.pdf", b"%PDF-1.7\nbody", max_bytes=MAX)
        assert (name, content_type) == ("report.pdf", "application/pdf")

    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("image/png", "photo"),
            ("text/csv", "spreadsheet"),
            ("message/rfc822", "email"),
            ("application/pdf", "document"),
        ],
    )
    def test_kinds_map_to_what_the_screen_draws(self, content_type: str, expected: str) -> None:
        assert document_kind(content_type) == expected


class TestTextExtraction:
    def test_plain_text_comes_back_whole(self) -> None:
        result = extract_text("text/plain", b"Crime reference: WY-2026-88123")
        assert result.status is DocumentExtractionStatus.EXTRACTED
        assert "WY-2026-88123" in result.text

    def test_a_csv_repeats_its_headers_so_a_row_reads_alone(self) -> None:
        content = b"Item,Cost\nRoof panels,14190.00\nStock write-off,38600.00\n"
        result = extract_text("text/csv", content)
        assert result.status is DocumentExtractionStatus.EXTRACTED
        assert "Item: Roof panels; Cost: 14190.00" in result.text

    def test_json_is_flattened_into_labelled_lines(self) -> None:
        result = extract_text("application/json", b'{"policy": {"number": "POL-2026-0041"}}')
        assert "policy number: POL-2026-0041" in result.text

    def test_malformed_json_fails_without_raising(self) -> None:
        result = extract_text("application/json", b"{not json")
        assert result.status is DocumentExtractionStatus.FAILED
        assert result.text == ""
        assert result.error is not None

    def test_a_docx_is_read_out_of_its_xml(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                "Estimated loss: GBP 128,000</w:t></w:r></w:p></w:body></w:document>",
            )
        result = extract_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            buffer.getvalue(),
        )
        assert result.status is DocumentExtractionStatus.EXTRACTED
        assert "Estimated loss: GBP 128,000" in result.text

    def test_a_scanned_pdf_is_reported_as_needing_ocr(self) -> None:
        result = extract_text("application/pdf", b"%PDF-1.7\n/Type/Page\n(no text operators)")
        assert result.status is DocumentExtractionStatus.UNSUPPORTED
        assert "optical character recognition" in (result.error or "").lower()

    def test_an_image_is_stored_but_claims_no_text(self) -> None:
        result = extract_text("image/png", b"\x89PNG\r\n\x1a\n")
        assert result.status is DocumentExtractionStatus.UNSUPPORTED
        assert result.text == ""

    def test_an_unknown_type_is_unsupported_rather_than_an_error(self) -> None:
        result = extract_text("application/x-unheard-of", b"bytes")
        assert result.status is DocumentExtractionStatus.UNSUPPORTED

    def test_a_corrupt_office_file_does_not_raise(self) -> None:
        result = extract_text(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04 truncated",
        )
        assert result.status in (
            DocumentExtractionStatus.FAILED,
            DocumentExtractionStatus.UNSUPPORTED,
        )
