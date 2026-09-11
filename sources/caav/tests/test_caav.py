"""Offline tests for caav_ingest.caav using saved HTML fixtures + synthetics."""
import os
import re

from caav_ingest import caav

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8", errors="replace") as f:
        return f.read()


# ── case_id helpers ────────────────────────────────────────────────────────

def test_normalize_case_id():
    assert caav._normalize_case_id("vn a392/2020") == "VN-A392-2020"
    assert caav._normalize_case_id("  VN--8650  ") == "VN-8650"
    assert caav._normalize_case_id("") == ""


def test_make_case_id_reg_and_date():
    assert caav.make_case_id("VN-8650", "2023-04-05", "http://x/a.pdf") == "VN-8650-2023-04-05"


def test_make_case_id_strips_doubled_vn_prefix():
    """Registration already starting with VN must not double the country prefix."""
    cid = caav.make_case_id("VN-A639", "2020-10-16", "http://x/a.pdf")
    assert cid == "VN-A639-2020-10-16"
    assert not cid.startswith("VN-VN")


def test_make_case_id_sha1_fallback_when_no_reg_or_date():
    cid = caav.make_case_id(None, None, "https://cdn/report.pdf")
    assert cid.startswith("VN-")
    # deterministic / intrinsic — same url → same id
    assert cid == caav.make_case_id("", "", "https://cdn/report.pdf")


def test_make_case_id_sha1_is_order_independent():
    """Two different urls give different ids; same url always the same id."""
    a = caav.make_case_id(None, None, "https://cdn/a.pdf")
    b = caav.make_case_id(None, None, "https://cdn/b.pdf")
    assert a != b
    assert a == caav.make_case_id(None, None, "https://cdn/a.pdf")


def test_make_case_id_empty_when_nothing():
    assert caav.make_case_id(None, None, None) == ""


# ── iter_listing_urls ──────────────────────────────────────────────────────

def test_iter_listing_urls_page1_only():
    urls = caav.iter_listing_urls()
    assert urls == [caav.INDEX_URL]


# ── parse_listing (real fixture) ───────────────────────────────────────────

def test_parse_listing_returns_report_rows():
    rows = caav.parse_listing(_fx("caav_index.html"))
    assert len(rows) == 11, f"expected 11 report rows, got {len(rows)}"


def test_parse_listing_detail_urls_absolute_doc_detail():
    rows = caav.parse_listing(_fx("caav_index.html"))
    for r in rows:
        assert r["detail_url"].startswith("https://english.caa.gov.vn/doc-detail/")
        assert r["detail_url"].endswith(".htm")


def test_parse_listing_pub_dates_iso_or_none():
    rows = caav.parse_listing(_fx("caav_index.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in rows:
        if r["pub_date"] is not None:
            assert iso.match(r["pub_date"]), r["pub_date"]


def test_parse_listing_titles_nonempty():
    rows = caav.parse_listing(_fx("caav_index.html"))
    for r in rows:
        assert r["listing_title"]


def test_parse_listing_no_duplicate_detail_urls():
    rows = caav.parse_listing(_fx("caav_index.html"))
    urls = [r["detail_url"] for r in rows]
    assert len(urls) == len(set(urls))


def test_parse_listing_skips_nav_anchors():
    """A doc-detail anchor with no date cell (nav menu) is not a report row."""
    html = (
        "<tr><td><a href='/doc-detail/menu-1.htm'>Some menu</a></td></tr>"
        "<tr><td><a href='/doc-detail/real-9.htm'>Real report</a></td>"
        "<td>01.02.2020</td><td>01.02.2020</td></tr>"
    )
    rows = caav.parse_listing(html)
    assert len(rows) == 1
    assert rows[0]["detail_url"].endswith("real-9.htm")


# ── parse_detail (real fixtures) ───────────────────────────────────────────

def test_parse_detail_bell505_slash_date_and_reg():
    d = caav.parse_detail(_fx("caav_detail_bell505.htm"))
    assert d["registration"] == "VN-8650"
    assert d["date_iso"] == "2023-04-05"
    assert d["aircraft"] == "Bell 505"
    assert d["pdf_url"].startswith("https://imgcaa.minhvujsc.com/")
    assert d["pdf_url"].endswith(".pdf")
    assert d["title"]


def test_parse_detail_worded_date():
    d = caav.parse_detail(_fx("caav_detail_worded_date.htm"))
    assert d["registration"] == "VN-A639"
    assert d["date_iso"] == "2020-10-16"
    assert d["aircraft"] == "Airbus A321"
    assert d["pdf_url"].endswith(".pdf")


def test_parse_detail_spaced_pdf_in_iframe():
    """PDF with a space in the filename, embedded via iframe/embed src."""
    d = caav.parse_detail(_fx("caav_detail_spaced_pdf.htm"))
    assert d["pdf_url"] is not None
    assert d["pdf_url"].startswith("https://imgcaa.minhvujsc.com/")
    assert d["pdf_url"].endswith(".pdf")
    assert " " in d["pdf_url"]  # raw url keeps the space; download() encodes it


def test_parse_detail_case_id_from_fixtures():
    d = caav.parse_detail(_fx("caav_detail_bell505.htm"))
    cid = caav.make_case_id(d["registration"], d["date_iso"], d["pdf_url"])
    assert cid == "VN-8650-2023-04-05"


# ── _parse_title_meta ──────────────────────────────────────────────────────

def test_parse_title_meta_slash_date():
    m = caav._parse_title_meta(
        "FINAL REPORT - SERIOUS INCIDENT VJ322 ON 14/06/2020 AT TAN SON NHAT"
    )
    assert m["date_iso"] == "2020-06-14"


def test_parse_title_meta_worded_date():
    m = caav._parse_title_meta("AIRBUS A321 VN-A639 ON 16th OCT, 2020")
    assert m["date_iso"] == "2020-10-16"
    assert m["registration"] == "VN-A639"
    assert m["aircraft"] == "Airbus A321"


def test_parse_title_meta_no_meta():
    m = caav._parse_title_meta("Some unrelated heading")
    assert m == {"registration": None, "date_iso": None, "aircraft": None}


# ── registration extraction (Finding 2: flight-number mis-extraction) ────────

def test_reg_prefers_aircraft_reg_over_flight_number():
    """'FLIGHT NUMBER VN125 AIRCRAFT VN-A870' must yield the reg, not VN125."""
    m = caav._parse_title_meta(
        "FINAL INVESTIGATION REPORT FLIGHT NUMBER VN125 AIRCRAFT VN-A870 DATED 02/04/2020"
    )
    assert m["registration"] == "VN-A870"


def test_reg_letter_mark_keeps_letter():
    """VN-A653 must NOT collapse to VN-4653 (the leading A dropped)."""
    m = caav._parse_title_meta(
        "INVESTIGATION RESULT OF FLIGHT NUMBER VJ380 AIRCRAFT VN-A653 DATED 17/04/2021"
    )
    assert m["registration"] == "VN-A653"


def test_reg_vn_b218():
    m = caav._parse_title_meta(
        "FINAL INVESTIGATION REPORT FLIGHT NUMBER VN1441 AIRCRAFT VN-B218 ON 19/12/2021"
    )
    assert m["registration"] == "VN-B218"


def test_reg_registration_keyword_context():
    m = caav._parse_title_meta(
        "INTERIM STATEMENT - INVESTIGATION AIRBUS A321 REGISTRATION VN-A639 FLIGHT NUMBER VJ260"
    )
    assert m["registration"] == "VN-A639"


def test_reg_numeric_mark_form_kept():
    """Numeric-mark reg with a dash (VN-8650) is still supported."""
    m = caav._parse_title_meta("FINAL REPORT BELL 505 AIRCRAFT VN-8650 ON 05/04/2023")
    assert m["registration"] == "VN-8650"


def test_reg_flight_number_only_no_garbage_reg():
    """A title with only a dashless flight number must yield NO registration."""
    m = caav._parse_title_meta(
        "FINAL INVESTIGATION REPORT FLIGHT NUMBER VN125 DATED 29/12/2018"
    )
    assert m["registration"] is None


def test_reg_vn_avn_typo_falls_back_to_sha1():
    """The live VN-AVN garbage title must not emit a reg → sha1 case_id."""
    title = "INVESTIGATION FLIGHT NUMBER VN AIRCRAFT VN ON 29/12/2018"
    m = caav._parse_title_meta(title)
    assert m["registration"] is None
    cid = caav.make_case_id(m["registration"], m["date_iso"], "https://cdn/x.pdf")
    # no garbage VN-AVN-... reg; sha1 fallback instead
    assert "AVN" not in cid
    assert cid == caav.make_case_id(None, None, "https://cdn/x.pdf")


# ── ID-enumeration discovery helpers (Finding 1) ─────────────────────────────

def test_detail_id_from_url():
    assert caav.detail_id_from_url(
        "https://english.caa.gov.vn/doc-detail/some-slug-30231.htm") == 30231
    assert caav.detail_id_from_url("https://x/doc-detail/no-id.htm") is None
    assert caav.detail_id_from_url("") is None


def test_id_window_from_seeds_anchored_on_max():
    """Window is anchored on the dense cluster (max), ignoring a stale outlier."""
    win = caav.id_window_from_seeds([29141, 30245, 30251, 30253])
    assert win[0] == 30253 - caav.ID_WINDOW_BACK
    assert win[-1] == 30253 + caav.ID_WINDOW_FWD
    # the stale 29141 outlier must NOT widen the window
    assert 29141 not in win
    assert 30223 in win and 30245 in win and 30253 in win


def test_id_window_from_seeds_empty():
    assert caav.id_window_from_seeds([]) == []
    assert caav.id_window_from_seeds([None]) == []


def test_is_report_detail_keeps_report_with_pdf():
    assert caav.is_report_detail({
        "title": "FINAL REPORT - FINAL INVESTIGATION REPORT ...",
        "pdf_url": "https://imgcaa.minhvujsc.com/a.pdf"})


def test_is_report_detail_keeps_titleless_with_pdf():
    """30251/30252-style titleless-but-valid accident PDFs are kept."""
    assert caav.is_report_detail({
        "title": "", "pdf_url": "https://imgcaa.minhvujsc.com/a.pdf"})


def test_is_report_detail_rejects_advisory_circular():
    """AC NN-NNN has a PDF but is not a report → excluded."""
    assert not caav.is_report_detail({
        "title": "AC 04-009 - GUIDANCE FOR USAGE OF PARTS REMOVED",
        "pdf_url": "https://imgcaa.minhvujsc.com/ac.pdf"})


def test_is_report_detail_rejects_empty_shell():
    """The empty-shell sentinel (no PDF) is never a report."""
    assert not caav.is_report_detail({"title": "", "pdf_url": None})


def test_event_class_from_title():
    assert caav.event_class_from_title(
        "FINAL REPORT INVESTIGATION OF ACCIDENT HELICOPTER BELL 505") == "Accident"
    assert caav.event_class_from_title(
        "INTERIM REPORT - SERIOUS INCIDENT INVESTIGATION A321") == "Serious incident"
    # 'serious incident' must win even when 'accident' word also appears
    assert caav.event_class_from_title(
        "SERIOUS INCIDENT - NOT AN ACCIDENT") == "Serious incident"


# ── _encode_url ────────────────────────────────────────────────────────────

def test_encode_url_spaces_and_parens():
    raw = "https://imgcaa.minhvujsc.com/2024/05/10/Final Report (4).pdf"
    enc = caav._encode_url(raw)
    assert " " not in enc
    assert "%20" in enc
    assert enc.startswith("https://imgcaa.minhvujsc.com/")
    assert enc.endswith(".pdf")
