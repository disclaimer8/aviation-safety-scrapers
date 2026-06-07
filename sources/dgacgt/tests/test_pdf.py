from dgacgt_ingest import pdf


def test_extract_text_missing_path():
    assert pdf.extract_text(None) == ""
    assert pdf.extract_text("") == ""


def test_extract_text_nonexistent_file_returns_empty():
    assert pdf.extract_text("/no/such/file_xyz.pdf") == ""


def test_thresholds():
    assert pdf.MIN_NARRATIVE > pdf.SCANNED_THRESHOLD
    assert pdf.SCANNED_THRESHOLD == 500
