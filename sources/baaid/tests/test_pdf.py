# tests/test_pdf.py
from baaid_ingest import pdf


def test_extract_text_none():
    assert pdf.extract_text(None) == ""


def test_extract_text_missing_file():
    assert pdf.extract_text("/nonexistent/x.pdf") == ""


def test_thresholds():
    assert pdf.MIN_NARRATIVE == 600
    assert pdf.SCANNED_CEILING == 500
    assert pdf.SCANNED_CEILING < pdf.MIN_NARRATIVE
