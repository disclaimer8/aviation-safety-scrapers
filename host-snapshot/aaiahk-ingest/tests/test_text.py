from aaiahk_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Airbus A350-1041") == "airbus-a350-1041"
    assert slugify("  B-LXA!! ") == "b-lxa"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Boeing 747-8F", "B-LJE", "Hong Kong International Airport") == \
        "crash-boeing-747-8f-b-lje-hong-kong-international-airport"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "B-ABCD", None) == "crash-b-abcd"
    assert make_site_slug(None, None, None) == "crash-aaiahk"
