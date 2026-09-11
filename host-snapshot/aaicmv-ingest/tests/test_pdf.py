# tests/test_pdf.py
from aaicmv_ingest import pdf


def test_extract_text_missing_path():
    assert pdf.extract_text(None) == ""
    assert pdf.extract_text("/nonexistent/zzz.pdf") == ""


def test_min_narrative_constant():
    assert pdf.MIN_NARRATIVE == 600
