from caav_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Engine &amp; gear</p>  <b>fail</b>") == "Engine & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Airbus A321") == "airbus-a321"
    assert slugify("  VN-A392!! ") == "vn-a392"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Airbus A321", "VN-A392", "Tan Son Nhat") == \
        "crash-airbus-a321-vn-a392-tan-son-nhat"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "VN-8650", None) == "crash-vn-8650"
    assert make_site_slug(None, None, None) == "crash-caav"
