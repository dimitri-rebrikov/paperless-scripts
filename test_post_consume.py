"""Tests for the OCR page gate.

That gate is the only place in this script where a mistake silently destroys
document text, so it is the one piece worth unit testing. It is deliberately
free of I/O, imports of pypaperless and any environment lookups.
"""

import pytest

from post_consume import ocr_gate


@pytest.mark.parametrize(
    ("mime_type", "page_count", "limit_pages", "expected"),
    [
        # PDFs inside the budget are safe
        ("application/pdf", 3, 5, True),
        ("application/pdf", 5, 5, True),
        # PDFs beyond the budget would lose their text
        ("application/pdf", 6, 5, False),
        ("application/pdf", 20, 5, False),
        # paperless-ngx reports no page count for some PDFs: do not guess
        ("application/pdf", None, 5, False),
        ("application/pdf", 0, 5, False),
        # a limit of 0 means paperless-gpt has no limit at all
        ("application/pdf", 12, 0, True),
        ("application/pdf", None, 0, True),
        # single images count as one page even without a reported page count
        ("image/jpeg", 1, 5, True),
        ("image/jpeg", None, 5, True),
        ("image/png", 1, 1, True),
        ("image/tiff", 1, 0, True),
        # everything else is left alone
        ("text/plain", 1, 5, False),
        ("text/plain", 1, 0, False),
        ("application/msword", 1, 5, False),
        ("", None, 5, False),
        (None, None, 5, False),
    ],
)
def test_ocr_gate(
    mime_type: str | None,
    page_count: int | None,
    limit_pages: int,
    expected: bool,
) -> None:
    queue_ocr, reason = ocr_gate(mime_type, page_count, limit_pages)

    assert queue_ocr is expected
    assert reason, "every decision must be explainable in the log"


def test_ocr_gate_reason_names_the_limit() -> None:
    _, reason = ocr_gate("application/pdf", 20, 5)

    assert "20" in reason
    assert "5" in reason


def test_ocr_gate_skips_unknown_page_count() -> None:
    queue_ocr, reason = ocr_gate("application/pdf", None, 5)

    assert queue_ocr is False
    assert "unknown" in reason
