# tests/test_baaid.py
"""Offline tests for baaid_ingest.baaid against a saved /accidents fixture."""
import os
import re

import pytest

from baaid_ingest import baaid

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]+$")  # PREFIX-HASH (normalised)


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def rows():
    return baaid.parse_listing(_fixture("baaid_accidents.html"))


# ── enumeration ───────────────────────────────────────────────────────────────

def test_parse_listing_full_db(rows):
    # The live DB had 222 unique PDFs (2001-2026) — confirm full enumeration,
    # well above the ~180 headline estimate (i.e. not just page 1).
    assert len(rows) >= 180, f"Expected >=180 reports, got {len(rows)}"


def test_case_ids_unique(rows):
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids"


def test_case_id_format(rows):
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"Bad case_id: {r['case_id']!r}"


def test_case_id_no_encounter_suffix(rows):
    # Intrinsic file-id ids must NOT carry an encounter-order numeric suffix
    # appended by us (e.g. '...-1', '...-2').  The hash itself is the id.
    for r in rows:
        # file-id has exactly one '-' (between prefix and hash) after normalising
        assert r["case_id"].count("-") == 1, f"Unexpected suffix: {r['case_id']!r}"


def test_pdf_urls_absolute_and_pdf(rows):
    for r in rows:
        u = r["pdf_url"]
        assert u.startswith("https://www.baaid.org/_files/ugd/")
        assert u.endswith(".pdf")


def test_years_span_2001_to_recent(rows):
    years = {r["date_of_occurrence"] for r in rows if r["date_of_occurrence"]}
    assert "2001" in years
    assert any(y >= "2024" for y in years)


def test_split_registration_merged(rows):
    # Wix split "N702"+"SV" / "C6-P"+"AA" across spans; merge must recombine.
    regs = {r["registration"] for r in rows if r["registration"]}
    assert "N702SV" in regs, "split reg N702SV not merged"
    assert "C6-PAA" in regs, "split reg C6-PAA not merged"


def test_under_investigation_occ_captured(rows):
    occs = {r["occ_listing"] for r in rows if r["occ_listing"]}
    assert any(o and o.startswith("OCC-2026") for o in occs)


def test_most_rows_have_registration(rows):
    with_reg = sum(1 for r in rows if r["registration"])
    assert with_reg >= len(rows) - 5


# ── case_id normalisation ─────────────────────────────────────────────────────

def test_make_case_id_from_file_id():
    assert baaid.make_case_id("320f20_D8C2DFF228") == "320f20-d8c2dff228"


def test_normalize_case_id_handles_occ_slash():
    assert baaid._normalize_case_id("OCC-2024/0044") == "occ-2024-0044"


def test_normalize_case_id_strips_edges():
    assert baaid._normalize_case_id("  _ABC__123_ ") == "abc-123"


# ── OCC normalisation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("OCC-2024/0044", "OCC-2024/0044"),
    ("OCC 2024/0044", "OCC-2024/0044"),
    ("OCC-2024-44", "OCC-2024/0044"),
    ("OCC-0026-0002", "OCC-2026/0002"),       # 00YY typo -> 20YY
    ("Report AO-20-000015 final", "AO-2020/000015"),
])
def test_normalize_occ(raw, expected):
    assert baaid.normalize_occ(raw) == expected


def test_normalize_occ_none():
    assert baaid.normalize_occ("no occurrence number here") is None
    assert baaid.normalize_occ("") is None
