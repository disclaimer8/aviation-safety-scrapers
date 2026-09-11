from gcaagy_ingest import text


def test_slugify():
    assert text.slugify("8R-GRE Final Report") == "8r-gre-final-report"
    assert text.slugify("") == ""


def test_make_site_slug():
    assert text.make_site_slug("BN2A Islander", "8R-GRA", "Eteringbang") == \
        "crash-bn2a-islander-8r-gra-eteringbang"


def test_make_site_slug_fallback():
    assert text.make_site_slug(None, None, None) == "crash-gcaagy"


def test_strip_html():
    assert text.strip_html("<i></i>Fly Jamaica") == "Fly Jamaica"
