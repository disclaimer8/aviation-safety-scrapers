from aaid_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Cessna 208") == "cessna-208"
    assert slugify("  5Y-LOL!! ") == "5y-lol"


def test_slugify_collapses_runs():
    assert slugify("Boeing   737!!!") == "boeing-737"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Cessna 208", "5Y-LOL", "Wilson Airport") == "crash-cessna-208-5y-lol-wilson-airport"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "5Y-ABC", None) == "crash-5y-abc"
    assert make_site_slug(None, None, None) == "crash-aaid"
