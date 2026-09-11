from taiib_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Tecnam P2008") == "tecnam-p2008"
    assert slugify("  YL-EVA!! ") == "yl-eva"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Tecnam P2008", "YL-EVA", "Adazi") == "crash-tecnam-p2008-yl-eva-adazi"


def test_make_site_slug_diacritics_collapse():
    # Latvian diacritics (Ā, ž) are not [a-z0-9] → collapsed to '-' separators.
    assert make_site_slug(None, "YL-EVA", "Ādaži") == "crash-yl-eva-da-i"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "YL-ABCD", None) == "crash-yl-abcd"
    assert make_site_slug(None, None, None) == "crash-taiib"
