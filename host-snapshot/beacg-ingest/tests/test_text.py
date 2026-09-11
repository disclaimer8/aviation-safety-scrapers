from beacg_ingest import text


def test_slugify():
    assert text.slugify("BEA-03-2023 Rapport Final") == "bea-03-2023-rapport-final"
    assert text.slugify("") == ""


def test_make_site_slug():
    assert text.make_site_slug("Boeing B737-36N", "TN-AKC", "Brazzaville") == \
        "crash-boeing-b737-36n-tn-akc-brazzaville"


def test_make_site_slug_fallback():
    assert text.make_site_slug(None, None, None) == "crash-beacg"


def test_strip_html():
    assert text.strip_html("<i></i>Africa Airlines") == "Africa Airlines"
