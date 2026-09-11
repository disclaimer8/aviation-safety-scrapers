# tests/test_eccaa.py
"""Offline tests for eccaa_ingest.eccaa using the saved AIG Reports fixture."""
import os
import re

from eccaa_ingest import eccaa

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── encode_pdf_url ──────────────────────────────────────────────────────────

def test_encode_pdf_url_spaces_and_parens():
    raw = ("http://www.eccaa.aero/images/stories/docs/far/"
           "Final Accident Report Cessna 402-C (J8-SXY) 5 Aug 2010.pdf")
    enc = eccaa.encode_pdf_url(raw)
    assert " " not in enc
    assert "%20" in enc
    assert "(" not in enc and ")" not in enc
    assert enc.startswith("http://www.eccaa.aero/images/stories/docs/far/")
    assert enc.endswith(".pdf")


# ── _normalize_case_id / make_case_id (intrinsic, no suffix) ────────────────

def test_make_case_id_reg_plus_date():
    assert eccaa.make_case_id("J8-SXY", "2010-08-05") == "J8-SXY-2010-08-05"


def test_make_case_id_reg_only():
    assert eccaa.make_case_id("N8862F", None) == "N8862F"


def test_make_case_id_normalizes_whitespace():
    assert eccaa.make_case_id("j8 vax", "2006-11-19") == "J8-VAX-2006-11-19"


def test_make_case_id_no_encounter_suffix():
    """Same reg+date always yields the SAME id (intrinsic, deterministic)."""
    a = eccaa.make_case_id("J8-SXY", "2010-08-05")
    b = eccaa.make_case_id("J8-SXY", "2010-08-05")
    assert a == b
    assert not re.search(r"-\d+$", a.replace("2010-08-05", ""))  # no trailing seq


# ── parse_listing against the real AIG Reports fixture ──────────────────────

def test_parse_listing_returns_seven_finals():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    # 9 PDF links in the page; 2 are '_'-prefixed (press release + preliminary)
    assert len(rows) == 7, f"expected 7 final reports, got {len(rows)}"


def test_parse_listing_skips_underscore_files():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    for r in rows:
        assert "PRESS RELEASE" not in r["title"].upper()
        assert "PRELIMINARY" not in r["title"].upper()


def test_parse_listing_pdf_urls_encoded_and_absolute():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    for r in rows:
        assert r["pdf_url"].startswith("http")
        assert r["pdf_url"].endswith(".pdf")
        assert " " not in r["pdf_url"]


def test_parse_listing_metadata_extracted():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    by_reg = {r["registration"]: r for r in rows}

    # Cessna 402-C (J8-SXY) 5 Aug 2010 -> St Vincent (VC)
    c402 = by_reg["J8-SXY"]
    assert c402["aircraft"].lower().startswith("cessna 402")
    assert c402["date_of_occurrence"] == "2010-08-05"
    assert c402["country"] == "VC"
    assert c402["case_id"] == "J8-SXY-2010-08-05"
    assert c402["event_class"] == "Accident"

    # Commander 500-S (J8-VAX) 19 Nov 2006 -> St Vincent
    assert by_reg["J8-VAX"]["date_of_occurrence"] == "2006-11-19"
    assert by_reg["J8-VAX"]["country"] == "VC"

    # PA-28-151 (N8862F) 12 Jan 2017 -> US
    assert by_reg["N8862F"]["country"] == "US"
    assert by_reg["N8862F"]["date_of_occurrence"] == "2017-01-12"


def test_parse_listing_country_is_multi_valued():
    """country must NOT be a single fixed value — derive >= 3 distinct."""
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    countries = {r["country"] for r in rows}
    assert len(countries) >= 3, f"expected multi-country, got {countries}"
    assert "VC" in countries
    assert "US" in countries


def test_parse_listing_no_duplicate_case_ids():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_parse_listing_all_dates_iso():
    rows = eccaa.parse_listing(_fixture("eccaa_aig_reports.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in rows:
        if r["date_of_occurrence"] is not None:
            assert iso.match(r["date_of_occurrence"]), r["date_of_occurrence"]
