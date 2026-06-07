from araib_ingest import araib
from tests.fixtures.synopsis_samples import (
    SYNOPSIS_HL8088, SYNOPSIS_AAR2203, SYNOPSIS_AIR1906,
)


# ── URL builders ────────────────────────────────────────────────────────────


def test_listing_page_url():
    assert araib.listing_page_url(1).endswith("&lcmspage=1")
    assert araib.listing_page_url(7).endswith("&lcmspage=7")
    assert "m_34591" in araib.listing_page_url(1)  # strict id↔m_ binding


def test_dtl_url():
    u = araib.dtl_url("262906")
    assert "DTL.jsp" in u and "idx=262906" in u and "id=eaib0401" in u
    assert "m_34591" in u


# ── tiny-stub detection (wrong-node 624-byte redirect) ─────────────────────


def test_looks_like_stub(stub_html):
    assert araib.looks_like_stub(stub_html)          # ~140 B redirect stub
    assert araib.looks_like_stub("")
    assert araib.looks_like_stub(None)
    assert not araib.looks_like_stub("X" * (araib.TINY_STUB_BYTES + 1))


# ── listing parse ───────────────────────────────────────────────────────────


def test_parse_listing_rows(listing1_html):
    rows = araib.parse_listing(listing1_html)
    # 5 data rows; the <thead> header row (no idx link) is skipped.
    assert len(rows) == 5
    by_idx = {r["idx"]: r for r in rows}
    assert set(by_idx) == {"266499", "263344", "262906", "256832", "247386"}
    r = by_idx["262906"]
    assert r["publish_date"] == "2025-01-31"
    assert "Jeju Air" in r["title"]
    assert "HL8088" in r["title"]
    assert r["view_count"] == "8733"
    assert "DTL.jsp" in r["dtl_url"] and "idx=262906" in r["dtl_url"]


def test_parse_listing_skips_paginator_and_header(listing1_html):
    rows = araib.parse_listing(listing1_html)
    # The paginator <a>s and the <thead> row carry no DTL idx → no phantom rows.
    assert all(r["idx"].isdigit() for r in rows)
    assert len(rows) == 5


def test_parse_listing_truncated_title_preserved(listing1_html):
    rows = araib.parse_listing(listing1_html)
    by_idx = {r["idx"]: r for r in rows}
    # Listing titles are '...'-truncated; the full title comes from the DTL page.
    assert by_idx["266499"]["title"].endswith("...")


# ── DTL parse incl. DWN.jsp fileName extraction ─────────────────────────────


def test_parse_dtl_human_filename(dtl_262906_html):
    d = araib.parse_dtl(dtl_262906_html)
    assert d["pdf_url"] == (
        "https://araib.molit.go.kr/LCMS/DWN.jsp?fold=/eaib0401/"
        "&fileName=HL8088+Preliminary+Report_English.pdf"
    )
    # Full untruncated title from the view table.
    assert d["title"] == "Preliminary Report of Jeju Air (HL8088, 7C2216)"


def test_parse_dtl_urlencoded_filename(dtl_247386_html):
    d = araib.parse_dtl(dtl_247386_html)
    # ⚠️ fileName scraped verbatim (url-encoded '%28AIR1906%29...'), not built.
    assert "%28AIR1906%29" in d["pdf_url"]
    assert d["pdf_url"].startswith("https://araib.molit.go.kr/LCMS/DWN.jsp")
    assert d["title"] == "Aircraft Serious Incident Report 29 October 2019"


def test_parse_dtl_no_pdf():
    d = araib.parse_dtl("<table class='board_view'><tr><th>Title</th>"
                        "<td>Report pending</td></tr></table>")
    assert d["pdf_url"] is None
    assert d["title"] == "Report pending"


# ── case-number extraction from synopsis ────────────────────────────────────


def test_extract_case_number_labelled():
    # 'Accident Number: AAR2404' (Jeju HL8088).
    assert araib.extract_case_number(SYNOPSIS_HL8088) == "aar2404"


def test_extract_case_number_araib_header_accident():
    # 'ARAIB/AAR2203' header form.
    assert araib.extract_case_number(SYNOPSIS_AAR2203) == "aar2203"


def test_extract_case_number_araib_header_incident():
    # 'ARAIB/AIR1906' — AIR (serious incident) letter group.
    assert araib.extract_case_number(SYNOPSIS_AIR1906) == "air1906"


def test_extract_case_number_none():
    assert araib.extract_case_number("No case number anywhere here.") is None
    assert araib.extract_case_number("") is None


def test_normalize_case_number_variants():
    assert araib.normalize_case_number("AAR 2404") == "aar2404"
    assert araib.normalize_case_number("AAR-2203") == "aar2203"
    assert araib.normalize_case_number("air1906") == "air1906"
    assert araib.normalize_case_number("XYZ1234") is None


# ── case_id fallback ────────────────────────────────────────────────────────


def test_case_id_from_canonical():
    assert araib.case_id_from("262906", "aar2404") == "aar2404"


def test_case_id_from_fallback():
    # No case number extractable → 'araib-{idx}'.
    assert araib.case_id_from("999999", None) == "araib-999999"


# ── registration (HL prefix, optional dash) ─────────────────────────────────


def test_extract_registration_from_synopsis():
    assert araib.extract_registration(SYNOPSIS_HL8088) == "HL8088"
    assert araib.extract_registration(SYNOPSIS_AAR2203) == "HL9678"


def test_extract_registration_dashed_normalised():
    # Listing title sometimes carries the dashed form 'HL-7525'.
    assert araib.extract_registration(
        "Final Investigation Report HL-7525 Accident") == "HL7525"


def test_extract_registration_none():
    assert araib.extract_registration("Boeing 737, no reg here") is None
    assert araib.extract_registration("") is None


# ── occurrence-date extraction (NOT the publish date) ───────────────────────


def test_extract_event_date_month_day_year():
    # 'December 29, 2024' (Jeju) — NOT the 2025.01.31 publish date.
    assert araib.extract_event_date(SYNOPSIS_HL8088) == "2024-12-29"


def test_extract_event_date_named_month():
    # 'November 27, 2022'.
    assert araib.extract_event_date(SYNOPSIS_AAR2203) == "2022-11-27"


def test_extract_event_date_day_month_year_abbrev():
    # '29 Oct, 2019' (abbreviated month, D Mon Year form).
    assert araib.extract_event_date(SYNOPSIS_AIR1906) == "2019-10-29"


def test_extract_event_date_none():
    assert araib.extract_event_date("no date present") is None
    assert araib.extract_event_date("") is None


# ── synopsis labelled fields ────────────────────────────────────────────────


def test_extract_operator_aircraft_location():
    assert "Jeju Air" in araib.extract_operator(SYNOPSIS_HL8088)
    assert "Boeing 737-800" in araib.extract_aircraft(SYNOPSIS_HL8088)
    assert "Muan" in araib.extract_location(SYNOPSIS_HL8088)


# ── report type ─────────────────────────────────────────────────────────────


def test_report_type_from_title():
    assert araib.report_type_from(
        "Preliminary Report of Jeju Air", "") == "Preliminary"
    assert araib.report_type_from(
        "Final Investigation Report HL-7525", "") == "Final"
    # ARAIB final reports self-identify as 'Aircraft Accident Report' etc. with
    # no 'final' keyword → treated as Final.
    assert araib.report_type_from(
        "Helicopter Crash", "Aircraft Accident Report\nARAIB/AAR2203") == "Final"
    assert araib.report_type_from("Helicopter Crash", "body text") is None
