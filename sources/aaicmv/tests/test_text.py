# tests/test_text.py
from aaicmv_ingest import text


def test_slugify():
    assert text.slugify("8Q-TBB DHC-6") == "8q-tbb-dhc-6"
    assert text.slugify("  Male' International ") == "male-international"


def test_make_site_slug():
    s = text.make_site_slug("DHC-6-300", "8Q-TBB", "Bathala")
    assert s == "crash-dhc-6-300-8q-tbb-bathala"


def test_make_site_slug_lowercase_only():
    s = text.make_site_slug("DHC-6", "8Q-TBB", None)
    assert s == s.lower()


def test_strip_html():
    assert text.strip_html("<p>Final <b>report</b></p>") == "Final report"
