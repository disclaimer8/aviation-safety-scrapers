from ciaado_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tren &amp; motor</p>  <b>falla</b>") == "Tren & motor falla"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Zenair CH-2000") == "zenair-ch-2000"
    assert slugify("  HI-878!! ") == "hi-878"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Zenair CH-2000", "HI-878", "MDJB") == "crash-zenair-ch-2000-hi-878-mdjb"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "HI-878", None) == "crash-hi-878"
    assert make_site_slug(None, None, None) == "crash-ciaado"
