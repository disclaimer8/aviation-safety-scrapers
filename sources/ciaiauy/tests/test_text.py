from ciaiauy_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tren de aterrizaje &amp; motor</p>  <b>falla</b>") == "Tren de aterrizaje & motor falla"


def test_strip_html_collapses_nbsp():
    assert strip_html("Informe Final\xa0CX-MGP") == "Informe Final CX-MGP"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Piper PA31") == "piper-pa31"
    assert slugify("  CX-MGP!! ") == "cx-mgp"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Piper PA31", "CX-MGP", "Aeropuerto de Carrasco") == "crash-piper-pa31-cx-mgp-aeropuerto-de-carrasco"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "CX-MGP", None) == "crash-cx-mgp"
    assert make_site_slug(None, None, None) == "crash-ciaiauy"
