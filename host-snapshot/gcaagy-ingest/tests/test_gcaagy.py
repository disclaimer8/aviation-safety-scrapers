import pytest

from gcaagy_ingest import gcaagy


# ── parse_listing (live fixture) ──────────────────────────────────────────────

def test_parse_listing_finds_all_pdfs(index_html):
    rows = gcaagy.parse_listing(index_html)
    assert len(rows) == 29


def test_parse_listing_case_ids_unique_and_intrinsic(index_html):
    rows = gcaagy.parse_listing(index_html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))                 # unique
    assert all(c.startswith("gcaagy-") for c in ids)  # namespaced
    # no encounter-order numeric suffix appended
    assert all(not c.split("-")[-1].isdigit() or "-" in c[:-1] for c in ids)


def test_parse_listing_urls_absolute_and_titled(index_html):
    rows = gcaagy.parse_listing(index_html)
    for r in rows:
        assert r["pdf_url"].startswith("https://www.gcaa-gy.org/pdf/")
        assert r["pdf_url"].endswith(".pdf")
        assert r["title"]                       # non-empty
        assert "<" not in r["title"]            # icon tags stripped


def test_parse_listing_includes_spaced_href(index_html):
    rows = gcaagy.parse_listing(index_html)
    # '8R-GTR Final Report.pdf' has a literal space; href kept verbatim
    spaced = [r for r in rows if r["case_id"] == "gcaagy-8r-gtr-final-report"]
    assert len(spaced) == 1
    assert " " in spaced[0]["pdf_url"]          # verbatim space preserved
    assert spaced[0]["pdf_url"].endswith("8R-GTR Final Report.pdf")


def test_parse_listing_known_cases(index_html):
    rows = gcaagy.parse_listing(index_html)
    ids = {r["case_id"] for r in rows}
    assert "gcaagy-fly-jamaica-accident-final-report" in ids
    assert "gcaagy-8r-gre-final-report" in ids
    assert "gcaagy-cal-accident-report-guyana" in ids


# ── make_case_id ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("href,expected", [
    ("pdf/8R-GRE_Final_Report.pdf", "gcaagy-8r-gre-final-report"),
    ("pdf/8R-GTR Final Report.pdf", "gcaagy-8r-gtr-final-report"),
    ("pdf/Fly_Jamaica_Accident_Final_Report.pdf", "gcaagy-fly-jamaica-accident-final-report"),
    ("pdf/tga8rghs.pdf", "gcaagy-tga8rghs"),
])
def test_make_case_id(href, expected):
    assert gcaagy.make_case_id(href) == expected


def test_make_case_id_is_deterministic():
    href = "pdf/8R-GTR Final Report.pdf"
    assert gcaagy.make_case_id(href) == gcaagy.make_case_id(href)


def test_make_case_id_urlencoded_equals_plain():
    assert gcaagy.make_case_id("pdf/8R-GTR%20Final%20Report.pdf") == \
           gcaagy.make_case_id("pdf/8R-GTR Final Report.pdf")


# ── _normalize_case_id / find_aaiiu_ref ───────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("AAIIU: 3/1/22/3", "aaiiu-3-1-22-3"),
    ("File No: AAIIU: 3.1.22", "aaiiu-3-1-22"),
    ("AAIIU 3/1/33", "aaiiu-3-1-33"),
    ("AAIIU:  3 / 1 / 32 ", "aaiiu-3-1-32"),
])
def test_normalize_case_id(raw, expected):
    assert gcaagy._normalize_case_id(raw) == expected


def test_normalize_case_id_none():
    assert gcaagy._normalize_case_id("") is None
    assert gcaagy._normalize_case_id("no digits here") is None


def test_find_aaiiu_ref_in_report(sample_report_text):
    assert gcaagy.find_aaiiu_ref(sample_report_text) == "aaiiu-3-1-22"


def test_find_aaiiu_ref_ignores_agency_token():
    # 'GAAIIU' is the agency name, not a file reference
    assert gcaagy.find_aaiiu_ref("GAAIIU 3/1/9 report by the unit") is None
    assert gcaagy.find_aaiiu_ref("Guyana GAAIIU") is None


def test_find_aaiiu_ref_accepts_dot_and_slash():
    assert gcaagy.find_aaiiu_ref("ref AAIIU: 3.1.22 here") == "aaiiu-3-1-22"
    assert gcaagy.find_aaiiu_ref("ref AAIIU: 3/2/8 here") == "aaiiu-3-2-8"


# ── find_registration ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("the aircraft 8R-GRE departed", "8R-GRE"),
    ("registered 8RGAB at the field", "8R-GAB"),
    ("foreign aircraft N524AT involved", "N524AT"),
    ("no registration present", None),
])
def test_find_registration(text, expected):
    assert gcaagy.find_registration(text) == expected


# ── extract_event_date ─────────────────────────────────────────────────────────

def test_extract_event_date_cover_block(cover_block_text):
    # 'Date of Accident - 14 AUGUST 2021'
    assert gcaagy.extract_event_date(cover_block_text) == "2021-08-14"


def test_extract_event_date_label_anchored_prefers_label():
    # A spurious word-date appears BEFORE the labelled event date; the
    # label-anchored date must win.
    text = (
        "Published 1 January 2099 by the unit.\n"
        "Date of Accident - 14 AUGUST 2021\n"
    )
    assert gcaagy.extract_event_date(text) == "2021-08-14"


@pytest.mark.parametrize("text,expected", [
    ("Date of Accident - 9TH NOVEMBER 2018", "2018-11-09"),
    ("Date of Occurrence - 21st February 2019", "2019-02-21"),
    ("Date of Accident - 18th January, 2014", "2014-01-18"),   # ordinal + comma
    ("Date of Accident - 14 AUGUST 2021", "2021-08-14"),       # no ordinal
    ("Date of Accident - 1st June 2007", "2007-06-01"),
    ("Date of Occurrence - 3RD MARCH, 2000", "2000-03-03"),
])
def test_extract_event_date_variants(text, expected):
    assert gcaagy.extract_event_date(text) == expected


def test_extract_event_date_fallback_no_label():
    # No 'Date of …' label → first word-month date in the head is used.
    assert gcaagy.extract_event_date("report dated 1st June 2007 here") == "2007-06-01"


def test_extract_event_date_none_when_absent():
    # The 2 known scanned/dateless reports must stay None gracefully.
    assert gcaagy.extract_event_date("no parseable date anywhere here") is None
    assert gcaagy.extract_event_date("") is None
    assert gcaagy.extract_event_date(None) is None


def test_extract_event_date_ignores_numeric_only():
    # A bare numeric date (no month word) is not a cover-block word-date.
    assert gcaagy.extract_event_date("Date of Accident - 14/08/2021") is None


# ── extract_aircraft / location / operator ─────────────────────────────────────

def test_extract_cover_fields_present(cover_block_text):
    assert gcaagy.extract_aircraft(cover_block_text) == "BN-2A-III-2 TRISLANDER"
    assert gcaagy.extract_location(cover_block_text) == \
        "HAAGS BOSCH SANITARY LANDFILL, REGION 4, GUYANA"
    assert gcaagy.extract_operator(cover_block_text) == "RORAIMA AIRWAYS INC"


def test_extract_cover_fields_label_variants():
    # 'Aircraft Manufacturer', 'Place of Accident/Region', 'Name of Operator'
    # variants (same-line and next-line values) all resolve.
    same_line = (
        "Aircraft Model/Type - BOEING 757-200\n"
        "Place of Accident/Region - CHEDDI JAGAN INTERNATIONAL AIRPORT\n"
        "Name of Operator - FLY JAMAICA AIRWAYS\n"
    )
    assert gcaagy.extract_aircraft(same_line) == "BOEING 757-200"
    assert gcaagy.extract_location(same_line) == "CHEDDI JAGAN INTERNATIONAL AIRPORT"
    assert gcaagy.extract_operator(same_line) == "FLY JAMAICA AIRWAYS"


def test_extract_cover_fields_next_line(sample_report_text):
    # The Fly Jamaica fixture puts each value on the line AFTER the label.
    assert gcaagy.extract_aircraft(sample_report_text) == "BOEING 757-200"
    assert gcaagy.extract_location(sample_report_text) == \
        "CHEDDI JAGAN INTERNATIONAL AIRPORT"
    assert gcaagy.extract_operator(sample_report_text) == "FLY JAMAICA AIRWAYS"


def test_extract_cover_fields_absent():
    assert gcaagy.extract_aircraft("nothing labelled here") is None
    assert gcaagy.extract_location("nothing labelled here") is None
    assert gcaagy.extract_operator("nothing labelled here") is None
    assert gcaagy.extract_aircraft("") is None
    assert gcaagy.extract_location(None) is None
