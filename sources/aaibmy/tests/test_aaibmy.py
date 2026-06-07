from aaibmy_ingest import aaibmy


# ── hub year-href enumeration ──────────────────────────────────────────────


def test_year_links_from_fixture(hub_html):
    links = aaibmy.year_links(hub_html)
    tails = [u.rsplit("/", 1)[-1] for u in links]
    # Old years carry the literal 'd' suffix; recent years do not.
    assert "2014d" in tails
    assert "2021d" in tails
    assert "2022" in tails
    assert "2026" in tails
    # No 'd' on modern years.
    assert "2022d" not in tails
    # Full expected span 2014-2026 = 13 years.
    assert len(links) == 13


def test_year_links_absolute_and_unique(hub_html):
    links = aaibmy.year_links(hub_html)
    assert all(u.startswith("https://www.mot.gov.my/") for u in links)
    assert len(links) == len(set(links))


def test_year_links_never_constructed_d_suffix_preserved(hub_html):
    # Regression guard: enumeration must keep era-specific 'd' tails
    # verbatim (constructing /2014 instead of /2014d would 404).
    links = aaibmy.year_links(hub_html)
    assert any(u.endswith("/2018d") for u in links)


# ── EN-only filter + bilingual dedupe ──────────────────────────────────────


def test_pdf_links_en_only_drops_malay(year2024_html):
    links = aaibmy.pdf_links(year2024_html)
    urls = [u for u, _ in links]
    # 2024 has a Malay-only copy under /my/AAIBmy…/SI 0224 — must be gone.
    assert not any("/my/" in u.lower() for u in urls)
    assert not any("aaibmy" in u.lower() for u in urls)
    assert all("/en/" in u.lower() for u in urls)


def test_pdf_links_dedupe_by_report_number():
    html = (
        '<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/2024/'
        '1.%20SI%200124%209M-ITX%20Final%20Report.pdf">a</a>'
        '<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/2024/'
        'SI%200124%209M-ITX%20Final%20Report%20v2.pdf">dup</a>'
    )
    links = aaibmy.pdf_links(html)
    assert len(links) == 1  # same report number SI 01/24 → one row


def test_pdf_links_count_from_fixture(year2022_html):
    links = aaibmy.pdf_links(year2022_html)
    # 2022 listing carries ~8-9 EN PDFs.
    assert 7 <= len(links) <= 12


# ── URL encoding of spaces / double-spaces / parens ────────────────────────


def test_pdf_url_encoding_double_space():
    html = ('<a href="/en/AAIB%20Statistic%20%20Accident%20Report%20Document/'
            '2022/8.%20A%200822P%209M-SSW%20Final%20Report.pdf">x</a>')
    (url, fn), = aaibmy.pdf_links(html)
    assert url.startswith("https://www.mot.gov.my/")
    assert "%20%20" in url           # double space preserved, encoded
    assert " " not in url            # no literal spaces in the URL
    assert fn == "8. A 0822P 9M-SSW Final Report"  # decoded for parsing


def test_pdf_url_encoding_rescues_literal_space():
    # Rare un-encoded href → still normalised to a space-free URL.
    html = ('<a href="/en/AAIB Statistic  Accident Report Document/2022/'
            'X 0122 Final Report.pdf">x</a>')
    links = aaibmy.pdf_links(html)
    assert links
    url = links[0][0]
    assert " " not in url
    assert "%20" in url


# ── filename number / reg / kind extraction ────────────────────────────────


def test_parse_filename_number_modern():
    m = aaibmy.parse_filename("8. A 0822P 9M-SSW Final Report")
    assert m["case_id"] == "a-08-22p"
    assert m["registration"] == "9M-SSW"
    assert m["report_kind"] == "Final"
    assert m["occurrence_type"] == "Accident"


def test_parse_filename_si_number():
    m = aaibmy.parse_filename("SI 0124 9M-ITX Final Report 23012024")
    assert m["case_id"] == "si-01-24"
    assert m["registration"] == "9M-ITX"
    assert m["occurrence_type"] == "Serious Incident"


def test_parse_filename_dashed_number_and_trailing_underscore():
    m = aaibmy.parse_filename("SI 0824 Final Report 9M-MXQ_")
    assert m["case_id"] == "si-08-24"
    assert m["registration"] == "9M-MXQ"   # trailing _ not consumed into reg


def test_parse_filename_dash_separated_number():
    m = aaibmy.parse_filename("Final Report SI 04-24 9M-LCM ")
    assert m["case_id"] == "si-04-24"
    assert m["registration"] == "9M-LCM"


def test_parse_filename_foreign_registrations():
    assert aaibmy.parse_filename("SI 0524 N566CB Final Report")["registration"] == "N566CB"
    assert aaibmy.parse_filename(
        "Final Report A 03-24 I-POOC updated")["registration"] == "I-POOC"
    assert aaibmy.parse_filename("A 03-24 I-POOC updated")["case_id"] == "a-03-24"


def test_parse_filename_date_keyed_legacy_no_number():
    m = aaibmy.parse_filename("07 July 2014")
    assert m["case_id"] is None
    assert m["registration"] is None


def test_make_case_id_fallback_and_collision():
    assert aaibmy.make_case_id("a-08-22p", "whatever") == "a-08-22p"
    # No number → slugified filename, capped at 40.
    cid = aaibmy.make_case_id(None, "07 July 2014")
    assert cid == "07-july-2014"
    # Collision suffixing.
    assert aaibmy.make_case_id("a-08-22p", "x", taken={"a-08-22p"}) == "a-08-22p-2"
    assert aaibmy.make_case_id(
        None, "07 July 2014", taken={"07-july-2014"}) == "07-july-2014-2"
