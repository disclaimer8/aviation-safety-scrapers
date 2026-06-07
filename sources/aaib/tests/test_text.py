from aaib_ingest.text import strip_html, slugify, make_site_slug

def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"

def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""

def test_slugify_basic():
    assert slugify("Leonardo AW139") == "leonardo-aw139"
    assert slugify("  G-CIMU!! ") == "g-cimu"

def test_make_site_slug_combines_parts():
    assert make_site_slug("Leonardo AW139", "G-CIMU", "Norwich Airport") == "crash-leonardo-aw139-g-cimu-norwich-airport"

def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "G-ABCD", None) == "crash-g-abcd"
    assert make_site_slug(None, None, None) == "crash-aaib"
