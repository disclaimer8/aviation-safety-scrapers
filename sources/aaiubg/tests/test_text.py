from aaiubg_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Leonardo AW139") == "leonardo-aw139"
    assert slugify("  LZ-PTS!! ") == "lz-pts"


def test_make_site_slug_from_case_id():
    assert make_site_slug("LZ-PTS_2022-08-08") == "lz-pts-2022-08-08"
    assert make_site_slug("G-EZBV_2022-11-17") == "g-ezbv-2022-11-17"


def test_make_site_slug_filename_fallback_case_id():
    assert make_site_slug("bg-report-r22-eng-ft") == "bg-report-r22-eng-ft"


def test_make_site_slug_empty_fallback():
    assert make_site_slug("") == "aaiubg"


def test_make_site_slug_lowercases():
    s = make_site_slug("LZ-PTS_2022-08-08")
    assert s == s.lower()
