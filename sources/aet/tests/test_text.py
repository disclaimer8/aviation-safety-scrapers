from aet_ingest import text


def test_slugify():
    assert text.slugify("CESSNA C177 Factual Report") == "cessna-c177-factual-report"
    assert text.slugify("") == ""


def test_make_site_slug():
    assert text.make_site_slug("Cessna C177", "LX-AIF", "Stegen") == \
        "crash-cessna-c177-lx-aif-stegen"


def test_make_site_slug_fallback():
    assert text.make_site_slug(None, None, None) == "crash-aet"


def test_strip_html():
    assert text.strip_html("<i></i>Final report") == "Final report"
