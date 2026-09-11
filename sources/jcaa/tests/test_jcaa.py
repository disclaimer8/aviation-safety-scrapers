# tests/test_jcaa.py
"""Tests for JCAA ingest — unit tests only, no live HTTP."""
import pytest
from jcaa_ingest import jcaa


# ── make_case_id ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stem,expected", [
    ("1996-May-30-6Y-JGT-Final-Report-1", "JCAA-1996-6Y-JGT"),
    ("2009-Dec-22-AA331-FINAL-REPORT",   "JCAA-2009-AA331"),
    ("2014-Sept-05-N900KN-Final-Report", "JCAA-2014-N900KN"),
    ("2014-Mar-31-JBU876-Final-Report",  "JCAA-2014-JBU876"),
    ("2016-Nov-10-N101KA-Final-Report",  "JCAA-2016-N101KA"),
])
def test_make_case_id_date_reg_stems(stem, expected):
    assert jcaa.make_case_id(stem) == expected


def test_make_case_id_is_deterministic():
    s = "1996-May-30-6Y-JGT-Final-Report-1"
    assert jcaa.make_case_id(s) == jcaa.make_case_id(s)


# ── _is_accident_pdf ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.jcaa.gov.jm/wp-content/uploads/2024/06/1996-May-30-6Y-JGT-Final-Report-1.pdf", True),
    ("https://www.jcaa.gov.jm/wp-content/uploads/2024/06/2009-Dec-22-AA331-FINAL-REPORT.pdf", True),
    ("https://www.jcaa.gov.jm/wp-content/uploads/2024/06/2008-Jul-07-AJM036-Final-Report-SERIOUS-INCIDENT.pdf", False),
    ("https://www.jcaa.gov.jm/wp-content/uploads/2026/05/JCAA-Statistical-Report-Jan-Mar-2026.pdf", False),
    ("https://www.jcaa.gov.jm/wp-content/uploads/2026/04/Jamaica-Aviation-Safety-Report-2025.pdf", False),
    ("https://www.jcaa.gov.jm/wp-content/uploads/2024/08/News-Release-ACCIDENT-INVESTIGATION-August-31-2010-1.pdf", False),
])
def test_is_accident_pdf_filter(url, expected):
    assert jcaa._is_accident_pdf(url) == expected


# ── _extract_from_filename ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,exp_year,exp_date,exp_reg", [
    (
        "https://x.com/2024/06/1996-May-30-6Y-JGT-Final-Report-1.pdf",
        1996, "1996-05-30", "6Y-JGT",
    ),
    (
        "https://x.com/2024/06/2009-Dec-22-AA331-FINAL-REPORT.pdf",
        2009, "2009-12-22", "AA331",
    ),
    (
        "https://x.com/2024/06/2014-Sept-05-N900KN-Final-Report.pdf",
        2014, "2014-09-05", "N900KN",
    ),
    (
        "https://x.com/2024/06/2016-Nov-10-N101KA-Final-Report.pdf",
        2016, "2016-11-10", "N101KA",
    ),
])
def test_extract_from_filename(url, exp_year, exp_date, exp_reg):
    year, event_date, reg, stem = jcaa._extract_from_filename(url)
    assert year == exp_year
    assert event_date == exp_date
    assert reg == exp_reg


# ── extract_event_date from report text ───────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Date and Time of Accident: October 19, 2021", "2021-10-19"),
    ("Date of Accident - 22 December 2009", "2009-12-22"),
    ("Date of Occurrence - 30 May 1996", "1996-05-30"),
    ("Date of Accident - 9th November 2018", "2018-11-09"),
])
def test_extract_event_date_from_text(text, expected):
    assert jcaa.extract_event_date(text) == expected


def test_extract_event_date_none_on_empty():
    assert jcaa.extract_event_date("") is None
    assert jcaa.extract_event_date(None) is None


# ── no-dups: case_ids for the known JCAA PDF set are unique ───────────────────

def test_known_jcaa_pdfs_unique_case_ids():
    filenames = [
        "1996-May-30-6Y-JGT-Final-Report-1",
        "2009-Dec-22-AA331-FINAL-REPORT",
        "2014-Mar-31-JBU876-Final-Report",
        "2014-Sept-05-N900KN-Final-Report",
        "2016-Nov-10-N101KA-Final-Report",
    ]
    ids = [jcaa.make_case_id(s) for s in filenames]
    assert len(ids) == len(set(ids)), f"duplicate case_ids: {ids}"


def test_value_regex_does_not_backtrack_on_blank_text_layer():
    """A scanned PDF whose text layer is only newlines must not hang the parse.

    The original pattern was `\\s*[-:–—]?\\s*(?:\\n\\s*)*([^\\n]+)`. \\s already
    matches \\n, so the two constructs could divide the same run of newlines
    exponentially many ways, and input that never reaches [^\\n]+ doubled the
    match time per newline — 24 newlines took 0.9s.

    Asserting a wall-clock budget rather than inspecting the pattern: what
    matters is that it finishes, and a rewrite that reintroduces the
    ambiguity should fail here.
    """
    import time

    for n in (24, 200, 2500):
        start = time.perf_counter()
        jcaa._VALUE_RE.match("\n" * n)
        assert time.perf_counter() - start < 0.5, (
            f"{n} newlines took too long — the value regex is backtracking again"
        )


def test_value_regex_skips_a_separator_on_its_own_line():
    """Behaviour the possessive rewrite had to preserve."""
    m = jcaa._VALUE_RE.match(" \n:\nthe value\nnext")
    assert m and m.group(1) == "the value"
