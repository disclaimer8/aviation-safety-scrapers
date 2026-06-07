from ciaape_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Rotor &amp; tren</p>  <b>falla</b>") == "Rotor & tren falla"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Cessna 208B") == "cessna-208b"
    assert slugify("  OB-1332!! ") == "ob-1332"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Airbus A320N", "CC-BHB", "Lima") == "crash-airbus-a320n-cc-bhb-lima"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "OB-1332", None) == "crash-ob-1332"
    assert make_site_slug(None, None, None) == "crash-ciaape"
