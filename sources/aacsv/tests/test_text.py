from aacsv_ingest import text


def test_slugify_lowercases_and_dashes():
    assert text.slugify("YS-289 PE!!") == "ys-289-pe"


def test_make_site_slug_prefix():
    s = text.make_site_slug(None, "YS-289PE", "Ilopango")
    assert s.startswith("crash-")
    assert "ys-289pe" in s


def test_make_site_slug_fallback():
    assert text.make_site_slug(None, None, None) == "crash-aacsv"


def test_strip_html():
    assert text.strip_html("<p>Hola  &amp; <b>mundo</b></p>") == "Hola & mundo"
