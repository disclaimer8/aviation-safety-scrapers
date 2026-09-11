# tests/test_pdf.py
from ainhr_ingest import pdf


def test_extract_text_empty_path():
    assert pdf.extract_text(None) == ""
    assert pdf.extract_text("") == ""


def test_thresholds_ordered():
    assert pdf.SCANNED_FLOOR < pdf.MIN_NARRATIVE


def test_extract_missing_file_graceful(tmp_path):
    assert pdf.extract_text(str(tmp_path / "nope.pdf")) == ""
