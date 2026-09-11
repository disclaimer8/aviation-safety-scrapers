from aaibzm_ingest import text


def test_slugify():
    assert text.slugify("9J-YVT Final Report") == "9j-yvt-final-report"
    assert text.slugify("") == ""


def test_make_site_slug():
    assert text.make_site_slug("Tanarg Neo Microlight", "9J-YVT", "Maramba") == \
        "crash-tanarg-neo-microlight-9j-yvt-maramba"


def test_make_site_slug_fallback():
    assert text.make_site_slug(None, None, None) == "crash-aaibzm"


def test_strip_html():
    assert text.strip_html("<i></i>Tanarg Neo") == "Tanarg Neo"
