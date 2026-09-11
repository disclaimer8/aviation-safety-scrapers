"""Scraper tests against a live-captured EN report-listing fixture."""
import os
import re

from aaibmn_ingest import aaibmn

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def test_parse_listing_extracts_rows():
    rows = aaibmn.parse_listing(_load("aaibmn_report_p1.html"))
    # page 1 has 20 PDF rows
    assert len(rows) == 20
    for r in rows:
        assert r["pdf_url"].startswith("https://aaib.gov.mn/uploads/")
        assert r["pdf_url"].endswith(".pdf")
        assert r["lang"] == "en"
        assert r["title"]


def test_parse_listing_fields_first_row():
    rows = aaibmn.parse_listing(_load("aaibmn_report_p1.html"))
    r0 = next(r for r in rows if "JU-1088" in (r["registration"] or "")
              or "1088" in r["title"])
    assert r0["registration"] == "JU-1088"
    assert r0["date_of_occurrence"] == "2024-02-27"
    assert r0["event_class"] == "Incident"
    # case_id intrinsic = registration + date
    assert r0["case_id"] == "ju-1088-2024-02-27"


def test_parse_listing_foreign_registration():
    rows = aaibmn.parse_listing(_load("aaibmn_report_p1.html"))
    eicxv = [r for r in rows if r["registration"] == "EI-CXV"]
    assert eicxv, "expected an EI-CXV (foreign) registration row"


def test_case_ids_are_intrinsic_no_order_suffix():
    rows = aaibmn.parse_listing(_load("aaibmn_report_p1.html"))
    for r in rows:
        # no trailing -1 / -2 encounter-order suffix tacked on a date
        assert not re.search(r"\d{4}-\d{2}-\d{2}-\d+$", r["case_id"])


def test_make_case_id_intrinsic():
    assert aaibmn.make_case_id("JU-1088", "2024-02-27", "t") == "ju-1088-2024-02-27"
    assert aaibmn.make_case_id(None, "2013-10-18", "Mi 8T JU-5566 report") == \
        "mi-8t-ju-5566-report-2013-10-18"
    assert aaibmn.make_case_id("EI-CXV", None, "t") == "ei-cxv"


def test_normalize_case_id():
    assert aaibmn._normalize_case_id("JU-1088 / x") == "ju-1088-x"
    assert aaibmn._normalize_case_id("") == ""


def test_parse_listing_page2():
    rows = aaibmn.parse_listing(_load("aaibmn_report_p2.html"))
    assert len(rows) == 9
