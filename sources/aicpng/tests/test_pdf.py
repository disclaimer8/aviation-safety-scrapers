"""pdf.extract_text robustness tests."""
from aicpng_ingest import pdf


def test_extract_empty_path():
    assert pdf.extract_text(None) == ""
    assert pdf.extract_text("") == ""


def test_extract_missing_file(tmp_path):
    assert pdf.extract_text(str(tmp_path / "nope.pdf")) == ""


def test_min_narrative_constant():
    assert pdf.MIN_NARRATIVE == 600
