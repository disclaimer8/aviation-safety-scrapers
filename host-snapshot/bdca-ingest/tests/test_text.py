from bdca_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Cessna C208EX") == "cessna-c208ex"
    assert slugify("  V3-HIN!! ") == "v3-hin"


def test_make_site_slug_combines_parts():
    assert make_site_slug("C208EX CARAVAN", "V3-HIN", "Placencia Airport") == "crash-c208ex-caravan-v3-hin-placencia-airport"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "V3-HIN", None) == "crash-v3-hin"
    assert make_site_slug(None, None, None) == "crash-bdca"
