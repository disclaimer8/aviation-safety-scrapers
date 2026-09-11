# tests/test_text.py
from baaid_ingest import text


def test_slugify():
    assert text.slugify("OCC-2024/0044") == "occ-2024-0044"
    assert text.slugify("") == ""


def test_make_site_slug_is_lowercased_case_id():
    assert text.make_site_slug("320F20_D8C2") == "320f20-d8c2"
    assert text.make_site_slug("OCC-2024/0044") == "occ-2024-0044"


def test_make_site_slug_fallback():
    assert text.make_site_slug("") == "baaid"


def test_strip_html():
    assert text.strip_html("<p>hi <b>there</b></p>") == "hi there"
