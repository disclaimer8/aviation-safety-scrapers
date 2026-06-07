from dgacgt_ingest import text


def test_slugify_basic():
    assert text.slugify("TG-MIC-2024-07-31") == "tg-mic-2024-07-31"
    assert text.slugify("A B/C") == "a-b-c"
    assert text.slugify("") == ""


def test_make_site_slug_from_case_id():
    assert text.make_site_slug("TG-MIC-2024-07-31") == "tg-mic-2024-07-31"
    assert text.make_site_slug("DGACGT-2006-01-25") == "dgacgt-2006-01-25"
    # site_slug must be only [a-z0-9-]
    s = text.make_site_slug("N-431SR-2011-01-10")
    assert all(c.isalnum() or c == "-" for c in s)


def test_strip_html():
    assert text.strip_html("<p>hola  mundo</p>") == "hola mundo"
    assert text.strip_html("") == ""
