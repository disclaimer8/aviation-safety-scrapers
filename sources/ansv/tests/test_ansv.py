# tests/test_ansv.py
"""
Tests for ansv_ingest.ansv — WordPress category pagination + report PDF discovery.

All tests run against captured live fixtures:
  tests/fixtures/ansv_listing_p1.html   — page-1 of /category/relazioni-dinchiesta/
  tests/fixtures/ansv_report.html       — single report page (I-COLK AW139)
"""

import os
import pytest

from ansv_ingest import ansv

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name: str) -> str:
    path = os.path.join(FIXTURES, name)
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── module constants ──────────────────────────────────────────────────────────

def test_constants_defined():
    assert ansv.BASE == "https://ansv.it"
    assert ansv.LISTING_URL.startswith("https://ansv.it/category/relazioni-dinchiesta")
    assert isinstance(ansv.DELAY, float)
    assert "Mozilla" in ansv.UA


# ── page_url ─────────────────────────────────────────────────────────────────

def test_page_url_1_is_listing_url():
    assert ansv.page_url(1) == ansv.LISTING_URL


def test_page_url_n_contains_page_n():
    url = ansv.page_url(5)
    assert "/page/5/" in url
    assert url.startswith("https://ansv.it")


# ── last_page ─────────────────────────────────────────────────────────────────

def test_last_page_from_fixture():
    html = _load("ansv_listing_p1.html")
    n = ansv.last_page(html)
    # Live fixture shows page 56 as last; must be plausible (>=10)
    assert isinstance(n, int)
    assert n >= 10, f"Expected last_page >= 10, got {n}"


def test_last_page_fallback():
    assert ansv.last_page("no pagination here") == 1


# ── parse_listing ─────────────────────────────────────────────────────────────

def test_parse_listing_returns_ten_rows():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    assert len(rows) == 10, f"Expected 10 rows, got {len(rows)}"


def test_parse_listing_report_urls_absolute_https():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    for row in rows:
        assert row["report_url"].startswith("https://ansv.it/"), (
            f"Non-absolute report_url: {row['report_url']}"
        )


def test_parse_listing_known_entry_i_colk():
    """The I-COLK AW139 entry must be present with correct metadata."""
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    colk = next(
        (r for r in rows if r.get("registration") == "I-COLK"), None
    )
    assert colk is not None, "I-COLK not found in listing rows"
    assert colk["date_of_occurrence"] == "2024-03-16"
    assert colk["report_url"].endswith("i-colk/")
    # aircraft should contain the type model
    assert colk["aircraft"] is not None
    assert "AW139" in colk["aircraft"].upper() or "AW" in colk["aircraft"]


def test_parse_listing_known_entry_pt_mug():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    pt_mug = next(
        (r for r in rows if r.get("registration") == "PT-MUG"), None
    )
    assert pt_mug is not None, "PT-MUG not found in listing rows"
    assert pt_mug["date_of_occurrence"] == "2024-07-09"
    assert "pt-mug" in pt_mug["report_url"].lower()


def test_parse_listing_foreign_registration():
    """Foreign registrations (D-KSEI, PH-NFR) must be parsed correctly."""
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    regs = {r["registration"] for r in rows if r["registration"]}
    assert "D-KSEI" in regs, f"D-KSEI missing; regs={regs}"
    assert "PH-NFR" in regs, f"PH-NFR missing; regs={regs}"


def test_parse_listing_row_keys():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    required = {"report_url", "title", "aircraft", "registration",
                "date_of_occurrence", "location"}
    for row in rows:
        assert required <= row.keys(), f"Missing keys in row: {row}"


def test_parse_listing_no_missing_report_url():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    for row in rows:
        assert row["report_url"], f"Empty report_url in row: {row}"


def test_parse_listing_dates_iso_or_none():
    html = _load("ansv_listing_p1.html")
    rows = ansv.parse_listing(html)
    iso_re = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")
    for row in rows:
        d = row["date_of_occurrence"]
        if d is not None:
            assert iso_re.match(d), f"Non-ISO date: {d!r}"


# ── parse_report ──────────────────────────────────────────────────────────────

def test_parse_report_pdf_url():
    html = _load("ansv_report.html")
    result = ansv.parse_report(html)
    assert result["pdf_url"] is not None, "No pdf_url extracted from report page"
    assert result["pdf_url"].endswith(".pdf"), f"Not a PDF URL: {result['pdf_url']}"
    assert result["pdf_url"].startswith("https://"), "PDF URL is not absolute"


def test_parse_report_pdf_url_contains_relazione():
    html = _load("ansv_report.html")
    result = ansv.parse_report(html)
    assert "relazione" in result["pdf_url"].lower() or "Relazione" in result["pdf_url"]


def test_parse_report_pdf_url_i_colk():
    html = _load("ansv_report.html")
    result = ansv.parse_report(html)
    assert "I-COLK" in result["pdf_url"], f"Expected I-COLK in PDF URL, got: {result['pdf_url']}"


def test_parse_report_title():
    html = _load("ansv_report.html")
    result = ansv.parse_report(html)
    assert result["title"] is not None
    assert "I-COLK" in result["title"] or "AW139" in result["title"]


def test_parse_report_has_pdf_url_key():
    html = _load("ansv_report.html")
    result = ansv.parse_report(html)
    assert "pdf_url" in result


# ── make_case_id ──────────────────────────────────────────────────────────────

def test_make_case_id_primary():
    cid = ansv.make_case_id("I-COLK", "2024-03-16", "https://ansv.it/some-slug/")
    assert cid == "I-COLK_2024-03-16"


def test_make_case_id_foreign_reg():
    cid = ansv.make_case_id("PT-MUG", "2024-07-09", "https://ansv.it/some-slug/")
    assert cid == "PT-MUG_2024-07-09"


def test_make_case_id_fallback_no_reg():
    cid = ansv.make_case_id(None, "2024-03-16", "https://ansv.it/some-page-slug/")
    # fallback: derived from URL slug
    assert cid == "some-page-slug"


def test_make_case_id_fallback_no_date():
    cid = ansv.make_case_id("I-COLK", None, "https://ansv.it/some-page-slug/")
    assert cid == "some-page-slug"


def test_make_case_id_fallback_neither():
    cid = ansv.make_case_id(None, None, "https://ansv.it/relazioni-finali-di-inchiesta-18/")
    assert cid == "relazioni-finali-di-inchiesta-18"


def test_make_case_id_separator():
    cid = ansv.make_case_id("D-KSEI", "2020-07-18", "https://ansv.it/slug/")
    assert "_" in cid
    assert cid == "D-KSEI_2020-07-18"
