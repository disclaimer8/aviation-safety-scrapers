from aet_ingest.pdf import extract_text, MIN_NARRATIVE


def test_min_narrative_constant():
    assert MIN_NARRATIVE == 600


def test_extract_text_missing_file():
    assert extract_text(None) == ""
    assert extract_text("/no/such/file.pdf") == ""
