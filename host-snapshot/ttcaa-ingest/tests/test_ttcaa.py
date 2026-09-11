# tests/test_ttcaa.py
"""Tests for TTCAA ingest — unit tests only."""
import pytest
from ttcaa_ingest import ttcaa


# ── make_case_id ──────────────────────────────────────────────────────────────

def test_make_case_id_with_year_and_reg():
    url = "https://caa.gov.tt/wp-content/uploads/2023/01/Investigation-Report-into-9Y-TJU-Crash-Landing.pdf"
    assert ttcaa.make_case_id(url, registration="9Y-TJU", year="2021") == "TTCAA-2021-9Y-TJU"


def test_make_case_id_reg_from_url():
    url = "https://caa.gov.tt/wp-content/uploads/2023/01/Investigation-Report-into-9Y-TJU-Crash-Landing.pdf"
    cid = ttcaa.make_case_id(url)
    assert cid == "TTCAA-9Y-TJU"


def test_make_case_id_is_deterministic():
    url = "https://caa.gov.tt/wp-content/uploads/2023/01/Investigation-Report-into-9Y-TJU-Crash-Landing.pdf"
    assert ttcaa.make_case_id(url, "9Y-TJU", "2021") == ttcaa.make_case_id(url, "9Y-TJU", "2021")


# ── _keep_filename_re ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("fname,expected", [
    ("Investigation-Report-into-9Y-TJU-Crash-Landing.pdf", True),
    ("Preliminary-Investigation-Report-9Y-XYZ.pdf", True),
    ("Final-Investigation-Report.pdf", True),
    ("TTCAA-Annual-Report-2022.pdf", False),
    ("Notice-to-Operators-2021.pdf", False),
])
def test_keep_filename_filter(fname, expected):
    url = f"https://caa.gov.tt/wp-content/uploads/2023/01/{fname}"
    result = bool(ttcaa._KEEP_FILENAME_RE.search(fname))
    assert result == expected


# ── extract_event_date ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Date and Time of Accident: October 19, 2021 @ 09:47L.", "2021-10-19"),
    ("On October 19, 2021 at approximately 9:47 local time", "2021-10-19"),
    ("Date of Accident - 22 December 2009", "2009-12-22"),
    ("Date of Occurrence - 30 May 1996", "1996-05-30"),
])
def test_extract_event_date(text, expected):
    assert ttcaa.extract_event_date(text) == expected


def test_extract_event_date_none():
    assert ttcaa.extract_event_date("") is None
    assert ttcaa.extract_event_date(None) is None


# ── extract_registration ──────────────────────────────────────────────────────

def test_extract_registration_9ytju():
    text = "involving 9Y-TJU, a Diamond Aircraft (DA-40, light single engine aircraft)"
    assert ttcaa.extract_registration(text) == "9Y-TJU"


def test_extract_registration_none():
    assert ttcaa.extract_registration("no registration here") is None


# ── SEED_REPORTS ──────────────────────────────────────────────────────────────

def test_seed_reports_not_empty():
    assert len(ttcaa.SEED_REPORTS) >= 1


def test_seed_reports_all_final():
    """All seed entries should be final reports (only known public report is final)."""
    for url, rtype in ttcaa.SEED_REPORTS:
        assert rtype == "Final", f"unexpected report type for {url}: {rtype}"


def test_seed_reports_9ytju_present():
    urls = [url for url, _ in ttcaa.SEED_REPORTS]
    assert any("9Y-TJU" in url for url in urls)
