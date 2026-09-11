from mzisi_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Pipistrel Virus") == "pipistrel-virus"
    assert slugify("  S5-DES!! ") == "s5-des"


def test_make_site_slug_combines_parts():
    assert make_site_slug("Cessna 172", "S5-DLM", "Ljubljana") == "crash-cessna-172-s5-dlm-ljubljana"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "S5-PGD", None) == "crash-s5-pgd"
    assert make_site_slug(None, None, None) == "crash-mzisi"
