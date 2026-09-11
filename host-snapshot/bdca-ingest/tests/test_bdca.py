# tests/test_bdca.py
"""Offline tests for bdca_ingest.bdca using the saved PhocaDownload fixture."""
import os
import re

from bdca_ingest import bdca

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^BDCA-\d{4}-[A-Z0-9-]+$")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── make_case_id / _normalize_case_id ──────────────────────────────────────

def test_make_case_id_basic():
    assert bdca.make_case_id(2023, "V3-HIN") == "BDCA-2023-V3-HIN"
    assert bdca.make_case_id(2019, "N936AN") == "BDCA-2019-N936AN"


def test_make_case_id_with_dup_marker():
    assert bdca.make_case_id(1995, "V3-HFD", "2") == "BDCA-1995-V3-HFD-2"


def test_make_case_id_normalizes_whitespace_and_case():
    assert bdca.make_case_id(2023, "  v3 hin  ") == "BDCA-2023-V3-HIN"


def test_normalize_case_id_collapses():
    assert bdca._normalize_case_id("  bdca  2023   v3-hin ") == "BDCA-2023-V3-HIN"


# ── parse_listing (live fixture) ───────────────────────────────────────────

def test_parse_listing_returns_rows():
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    assert len(rows) >= 7, f"Expected >=7 report rows, got {len(rows)}"


def test_parse_listing_case_id_format():
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"Bad case_id: {r['case_id']!r}"


def test_parse_listing_no_duplicate_case_ids():
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids in listing"


def test_parse_listing_known_recent_row():
    """2023 V3-HIN is the most recent report."""
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "BDCA-2023-V3-HIN" in by_id
    row = by_id["BDCA-2023-V3-HIN"]
    assert row["registration"] == "V3-HIN"
    assert row["year"] == 2023
    assert row["event_class"] == "Accident"
    assert row["pdf_url"].startswith("https://")
    assert "download=400" in row["pdf_url"]


def test_parse_listing_foreign_registration_n_number():
    """N936AN (US-registered, 2019) is carried correctly."""
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "BDCA-2019-N936AN" in by_id
    assert by_id["BDCA-2019-N936AN"]["registration"] == "N936AN"


def test_parse_listing_duplicate_registration_disambiguated():
    """V3-HFD appears twice (1997 and 1995); both yield distinct case_ids."""
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    ids = {r["case_id"] for r in rows}
    hfd = {r["case_id"] for r in rows if r["registration"] == "V3-HFD"}
    assert len(hfd) == 2, f"Expected 2 distinct V3-HFD case_ids, got {hfd}"
    assert "BDCA-1997-V3-HFD" in ids


def test_parse_listing_all_pdf_urls_absolute():
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    for r in rows:
        assert r["pdf_url"].startswith("https://"), r["pdf_url"]
        assert "?download=" in r["pdf_url"]


def test_parse_listing_titles_nonempty():
    rows = bdca.parse_listing(_fixture("bdca_index.html"))
    for r in rows:
        assert r["title"], f"Empty title for {r['case_id']}"


# ── synthetic guards ───────────────────────────────────────────────────────

def test_parse_listing_synthetic_row():
    html = (
        '<div class="pd-title"><a class="" '
        'href="/index.php/accident-investigation-unit-aiu/accident-reports'
        '?download=999:final-report-v3-zzz">'
        '2024 - Belize Aircraft Accident &amp; Investigation (Final Report V3-ZZZ)'
        '</a></div>'
    )
    rows = bdca.parse_listing(html)
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "BDCA-2024-V3-ZZZ"
    assert r["registration"] == "V3-ZZZ"
    assert r["year"] == 2024
    assert r["pdf_url"].startswith("https://www.civilaviation.gov.bz")


def test_parse_listing_dup_marker_synthetic():
    html = (
        '<div class="pd-title"><a '
        'href="/index.php/x?download=12:final-report-v3-hfd-2">'
        '1995 - Belize Aircraft Accident &amp; Investigation (Final Report V3-HFD [2])'
        '</a></div>'
    )
    rows = bdca.parse_listing(html)
    assert len(rows) == 1
    assert rows[0]["case_id"] == "BDCA-1995-V3-HFD-2"


def test_parse_listing_skips_non_download_anchors():
    html = '<div class="pd-title"><a href="/some/other/page">Not a report</a></div>'
    assert bdca.parse_listing(html) == []
