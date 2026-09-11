from aaiib_ingest.text import strip_html, slugify, make_site_slug, month_name_date_to_iso


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("AAIIB-2023-RP-C1174") == "aaiib-2023-rp-c1174"
    assert slugify("  RP-C8846!! ") == "rp-c8846"


def test_make_site_slug_lowercases_case_id():
    assert make_site_slug("AAIIB-2023-RP-C1174") == "aaiib-2023-rp-c1174"
    assert make_site_slug("AAIIB-2025/RP-C8798") == "aaiib-2025-rp-c8798"


def test_make_site_slug_fallback():
    assert make_site_slug("") == "aaiib"
    assert make_site_slug(None) == "aaiib"


def test_month_name_date_to_iso():
    assert month_name_date_to_iso("JANUARY 24, 2023") == "2023-01-24"
    assert month_name_date_to_iso("October 23, 2022") == "2022-10-23"
    assert month_name_date_to_iso("Feb 6, 2014") == "2014-02-06"
    assert month_name_date_to_iso("") is None
    assert month_name_date_to_iso("garbage") is None
    assert month_name_date_to_iso("Smarch 99, 2020") is None
