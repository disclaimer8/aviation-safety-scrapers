# tests/test_aaiib.py
"""Offline tests for aaiib_ingest.aaiib using saved HTML fixtures."""
import os
import re

from aaiib_ingest import aaiib

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^AAIIB-\d{4}-[A-Z0-9-]+$")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── iter_year_urls ─────────────────────────────────────────────────────────

def test_iter_year_urls_returns_list():
    urls = aaiib.iter_year_urls(_fixture("aaiib_index.html"))
    assert isinstance(urls, list)
    assert len(urls) >= 15, f"Expected >=15 year URLs, got {len(urls)}"


def test_iter_year_urls_includes_known_years():
    urls = aaiib.iter_year_urls(_fixture("aaiib_index.html"))
    assert any("2008-accidents" in u for u in urls)
    assert any("2023-accidents" in u for u in urls)
    assert any("2025-accidents" in u for u in urls)


def test_iter_year_urls_all_absolute():
    urls = aaiib.iter_year_urls(_fixture("aaiib_index.html"))
    for u in urls:
        assert u.startswith("http"), f"Not absolute: {u!r}"


def test_iter_year_urls_no_duplicates():
    urls = aaiib.iter_year_urls(_fixture("aaiib_index.html"))
    assert len(urls) == len(set(urls))


# ── parse_listing — 2008 (dated filenames) ─────────────────────────────────

def test_parse_listing_2008_returns_rows():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2008.html"), "2008")
    assert len(rows) >= 10, f"Expected >=10 rows from 2008, got {len(rows)}"


def test_parse_listing_2008_case_id_format():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2008.html"), "2008")
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"Bad case_id: {r['case_id']!r}"
        assert r["case_id"].startswith("AAIIB-2008-")


def test_parse_listing_2008_extracts_filename_dates():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2008.html"), "2008")
    dated = [r for r in rows if r["date_of_occurrence"]]
    assert len(dated) >= 10
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in dated:
        assert iso_re.match(r["date_of_occurrence"])
        assert r["date_of_occurrence"].startswith("2008-")


def test_parse_listing_2008_known_row():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2008.html"), "2008")
    by_id = {r["case_id"]: r for r in rows}
    assert "AAIIB-2008-RP-C229" in by_id
    row = by_id["AAIIB-2008-RP-C229"]
    assert row["registration"] == "RP-C229"
    assert row["date_of_occurrence"] == "2008-02-01"
    assert row["pdf_url"].endswith(".pdf")
    assert row["event_class"] == "Accident"


# ── parse_listing — 2023 (undated filenames) ───────────────────────────────

def test_parse_listing_2023_returns_rows():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2023.html"), "2023")
    assert len(rows) >= 8, f"Expected >=8 rows from 2023, got {len(rows)}"


def test_parse_listing_2023_case_ids():
    rows = aaiib.parse_listing(_fixture("aaiib_year_2023.html"), "2023")
    ids = {r["case_id"] for r in rows}
    assert "AAIIB-2023-RP-C1174" in ids


def test_parse_listing_pdf_urls_absolute():
    for fx, yr in (("aaiib_year_2008.html", "2008"), ("aaiib_year_2023.html", "2023")):
        for r in aaiib.parse_listing(_fixture(fx), yr):
            assert r["pdf_url"].startswith("http")
            assert r["pdf_url"].lower().endswith(".pdf")


def test_parse_listing_no_duplicate_case_ids():
    for fx, yr in (("aaiib_year_2008.html", "2008"), ("aaiib_year_2023.html", "2023")):
        rows = aaiib.parse_listing(_fixture(fx), yr)
        ids = [r["case_id"] for r in rows]
        assert len(ids) == len(set(ids))


def test_parse_listing_excludes_boilerplate():
    """The site-wide boilerplate PDFs (manuals/CSR/memo) must never appear."""
    for fx, yr in (("aaiib_year_2008.html", "2008"), ("aaiib_year_2023.html", "2023")):
        for r in aaiib.parse_listing(_fixture(fx), yr):
            u = r["pdf_url"].lower()
            for bad in ("ownership", "concession", "corporate-governance",
                        "fit-and-proper", "no-gift", "social-responsibility",
                        "aerodrome", "qms", "memo_", "environmental"):
                assert bad not in u, f"boilerplate leaked: {r['pdf_url']}"


# ── case_id helpers ────────────────────────────────────────────────────────

def test_make_case_id_basic():
    assert aaiib.make_case_id("2023", "RP-C1174") == "AAIIB-2023-RP-C1174"
    assert aaiib.make_case_id("2022", "HL7525") == "AAIIB-2022-HL7525"


def test_make_case_id_missing_parts():
    assert aaiib.make_case_id("", "RP-C1174") is None
    assert aaiib.make_case_id("2023", None) is None


def test_normalize_case_id_collapses_whitespace():
    assert aaiib._normalize_case_id("  AAIIB - 2023 / RP-C1174 ") == "AAIIB-2023/RP-C1174"
    assert aaiib._normalize_case_id("AAIIB-2023- RP-C1174") == "AAIIB-2023-RP-C1174"
    assert aaiib._normalize_case_id("aaiib-2023-rp-c1174") == "AAIIB-2023-RP-C1174"


# ── registration / accident-pdf filtering ──────────────────────────────────

def test_extract_registration_rp_marks():
    assert aaiib._extract_registration(".../RP-C1124_accident-09252008.pdf") == "RP-C1124"
    assert aaiib._extract_registration(".../Accident-RP-R8278.pdf") == "RP-R8278"


def test_extract_registration_foreign_marks():
    assert aaiib._extract_registration(".../Accident-HL7525-Final-Report.pdf") == "HL7525"
    assert aaiib._extract_registration(".../A6-ENN-Accident-Final-Report.pdf") == "A6-ENN"
    assert aaiib._extract_registration(".../B-5498-Accident.pdf") == "B-5498"


def test_is_accident_pdf_accepts_reports():
    assert aaiib.is_accident_pdf("https://x/Accident-RP-C1174.pdf")
    assert aaiib.is_accident_pdf("https://x/Final-Report-RP-R3298-.pdf")
    assert aaiib.is_accident_pdf("https://x/Interim-Statement-RP-C3424.pdf")


def test_is_accident_pdf_rejects_boilerplate_and_non_reports():
    assert not aaiib.is_accident_pdf("https://x/No-Gift-Policy-Signed.pdf")
    assert not aaiib.is_accident_pdf("https://x/Ownership-and-Operations-Manual.pdf")
    # accident keyword but no registration mark
    assert not aaiib.is_accident_pdf("https://x/accident-reporting-form.pdf")


# ── PDF metadata extraction ────────────────────────────────────────────────

_SAMPLE_HEADER = (
    "AIRCRAFT ACCIDENT INVESTIGATION AND INQUIRY BOARD\n"
    "FINAL REPORT\nHL7525\nAIRBUS A330-322\n"
    "OPERATOR: KOREAN AIR LINES CO., LTD.\n"
    "TYPE OF OPERATION: SCHEDULED COMMERCIAL OPERATION\n"
    "DATE OF OCCURRENCE: OCTOBER 23, 2022\n"
    "PLACE OF OCCURRENCE: MACTAN-CEBU INTERNATIONAL AIRPORT, CEBU, PHILIPPINES\n\n"
    "AAIIB-2025-046\nTABLE OF CONTENTS\n"
)


def test_extract_pdf_metadata_full():
    m = aaiib.extract_pdf_metadata(_SAMPLE_HEADER)
    assert m["aaiib_ref"] == "AAIIB-2025-046"
    assert m["date_iso"] == "2022-10-23"
    assert "MACTAN-CEBU" in m["location"]
    assert m["operator"] == "KOREAN AIR LINES CO., LTD."


def test_extract_pdf_metadata_empty():
    m = aaiib.extract_pdf_metadata("")
    assert all(v is None for v in m.values())


def test_extract_pdf_metadata_no_ref():
    m = aaiib.extract_pdf_metadata("DATE OF OCCURRENCE: FEBRUARY 19, 2024\n")
    assert m["aaiib_ref"] is None
    assert m["date_iso"] == "2024-02-19"


# ── final-report preference when multiple PDFs share a registration ─────────

def test_parse_listing_prefers_final_over_interim():
    html = (
        '<a href="https://x/Interim-Statement-1st-KE631-HL7525-as-of-23-Oct-2023.pdf">i1</a>'
        '<a href="https://x/Interim-Statement-2nd-KE631-HL7525-as-of-22-Oct-2024.pdf">i2</a>'
        '<a href="https://x/Accident-HL7525-Final-Report.pdf">final</a>'
    )
    rows = aaiib.parse_listing(html, "2022")
    hl = [r for r in rows if r["case_id"] == "AAIIB-2022-HL7525"]
    assert len(hl) == 1
    assert hl[0]["pdf_url"].endswith("Accident-HL7525-Final-Report.pdf")


def test_parse_listing_prefers_accident_over_interim():
    html = (
        '<a href="https://x/Interim-Statement-RP-C3424.pdf">i</a>'
        '<a href="https://x/Accident-RP-C3424.pdf">a</a>'
    )
    rows = aaiib.parse_listing(html, "2025")
    rc = [r for r in rows if r["case_id"] == "AAIIB-2025-RP-C3424"]
    assert len(rc) == 1
    assert "Accident-RP-C3424.pdf" in rc[0]["pdf_url"]
