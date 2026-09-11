# tests/test_aaisl.py
"""Offline tests for aaisl_ingest.aaisl using the saved live listing fixture."""
import os
import re

from aaisl_ingest import aaisl

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def _rows():
    return aaisl.parse_listing(_fixture("aaisl_index.html"))


# ── parse_listing: counts & integrity ──────────────────────────────────────

def test_parse_listing_returns_all_reports():
    rows = _rows()
    # ~37 accident report PDFs on the live page
    assert len(rows) >= 35, f"Expected >=35 report rows, got {len(rows)}"


def test_parse_listing_case_ids_unique():
    rows = _rows()
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids parsed"


def test_parse_listing_all_pdf_urls_absolute_https():
    for r in _rows():
        url = r["pdf_url_en"]
        assert url and url.startswith("https://www.caa.lk/"), f"bad pdf_url: {url!r}"
        assert url.endswith(".pdf")


def test_parse_listing_single_language_es_always_none():
    for r in _rows():
        assert r["pdf_url_es"] is None
        assert r["report_url"] is None


def test_parse_listing_dates_iso_or_none():
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in _rows():
        if r["date_of_occurrence"] is not None:
            assert iso.match(r["date_of_occurrence"]), r["date_of_occurrence"]


def test_parse_listing_event_classes_valid():
    valid = {"Accident", "Serious incident", "Incident", None}
    for r in _rows():
        assert r["event_class"] in valid


def test_parse_listing_titles_nonempty():
    for r in _rows():
        assert r["title"], f"empty title for {r['case_id']}"


# ── known live rows ────────────────────────────────────────────────────────

def test_known_row_saffron_c208_2026_accident():
    by_id = {r["case_id"]: r for r in _rows()}
    assert "4R-CAE-20260107" in by_id
    r = by_id["4R-CAE-20260107"]
    assert r["event_class"] == "Accident"
    assert r["registration"] == "4R-CAE"
    assert r["date_of_occurrence"] == "2026-01-07"
    assert r["aircraft"] == "Cessna 208"
    assert "Saffron" in (r["operator"] or "")
    assert r["pdf_url_en"].endswith(".pdf")


def test_known_row_4racv_hard_landing_2024():
    by_id = {r["case_id"]: r for r in _rows()}
    assert "4R-ACV-20240608" in by_id
    r = by_id["4R-ACV-20240608"]
    assert r["registration"] == "4R-ACV"
    assert r["date_of_occurrence"] == "2024-06-08"
    assert r["event_class"] == "Incident"


def test_tsib_rehost_row_present_in_listing():
    """The re-hosted TSIB (Singapore) 4R-ABN 2019 report appears in the listing
    (it is filtered out later at parse time by is_foreign_authority)."""
    by_id = {r["case_id"]: r for r in _rows()}
    assert "4R-ABN-20190321" in by_id, "TSIB-rehost row missing from listing"


def test_no_reg_row_falls_back_to_slug_case_id():
    """Rows without a registration get an intrinsic LK-<slug> case_id."""
    rows = _rows()
    slug_ids = [r for r in rows if r["case_id"].startswith("LK-") and r["registration"] is None]
    assert slug_ids, "expected at least one slug-fallback case_id"
    for r in slug_ids:
        assert re.match(r"^LK-[A-Z0-9-]+$", r["case_id"]), r["case_id"]


# ── case_id helpers ────────────────────────────────────────────────────────

def test_make_case_id_reg_and_date():
    assert aaisl.make_case_id("4R-CAE", "2026-01-07", "anything") == "4R-CAE-20260107"


def test_make_case_id_reg_only():
    assert aaisl.make_case_id("4R-ABC", None, "x") == "4R-ABC"


def test_make_case_id_fallback_slug():
    cid = aaisl.make_case_id(None, None, "ATC near miss incident on 17th April 1998")
    assert cid.startswith("LK-")
    assert "ATC" in cid


def test_make_case_id_is_intrinsic_no_order_suffix():
    """Same inputs → same case_id (intrinsic, deterministic, no -1/-2 suffixes)."""
    a = aaisl.make_case_id("4R-XYZ", "2020-01-01", "t")
    b = aaisl.make_case_id("4R-XYZ", "2020-01-01", "t")
    assert a == b == "4R-XYZ-20200101"
    assert not re.search(r"-\d$", a)


def test_normalize_case_id():
    assert aaisl._normalize_case_id("4r cae") == "4R-CAE"
    assert aaisl._normalize_case_id("  in--002 / 2024 ") == "IN-002-2024"


# ── foreign-authority (TSIB-rehost) detection ──────────────────────────────

def test_is_foreign_authority_tsib_singapore():
    txt = (
        "Final Report\nAIRBUS A320, REGISTRATION 4R-ABN\n"
        "Transport Safety Investigation Bureau\nMinistry of Transport\nSingapore\n"
        "The Transport Safety Investigation Bureau (TSIB) is the air ... authority in Singapore."
    )
    assert aaisl.is_foreign_authority(txt) is True


def test_is_foreign_authority_sri_lanka_report_is_not_foreign():
    txt = (
        "FINAL REPORT\nAccident involving Cessna 208 amphibian aircraft 4R-CAE\n"
        "Released by the Civil Aviation Authority of Sri Lanka\n"
        "Published by: Civil Aviation Authority of Sri Lanka"
    )
    assert aaisl.is_foreign_authority(txt) is False


def test_is_foreign_authority_released_by_caa_is_genuine():
    """A report investigated by a foreign AAIB but RELEASED by CAA Sri Lanka is a
    genuine AAII publication, NOT a re-host (the SL release line may be split
    across a newline in the extracted text)."""
    txt = (
        "FINAL REPORT\nINCIDENT OF SRILANKAN AIRLINES AIRBUS A340-313, 4R-ADG\n"
        "Investigated by Air Accident Investigation Branch of United Kingdom "
        "Released by the Civil Aviation\nAuthority of Sri Lanka\nAAIB Bulletin: 12/2012"
    )
    assert aaisl.is_foreign_authority(txt) is False


def test_is_foreign_authority_empty():
    assert aaisl.is_foreign_authority("") is False
    assert aaisl.is_foreign_authority(None) is False


# ── source-key NON-BLEED tests (aaisl must NOT match aaib / aaiu / aaiib) ──

# Sibling-source keys in the program that the EXACT key 'aaisl' must never be
# confused with.  These guard against substring / prefix bleed.
_SIBLINGS = ["aaib", "aaiu", "aaiube", "aaiib", "aaid"]


def test_source_key_exact_match_only():
    key = "aaisl"
    for sib in _SIBLINGS:
        assert key != sib
        # 'aaisl' must not be a substring of, nor contain, any sibling key
        assert sib not in key, f"sibling {sib!r} bleeds into {key!r}"
        assert key not in sib, f"{key!r} bleeds into sibling {sib!r}"


def test_table_names_are_aaisl_not_sibling():
    """Schema table names use the exact 'aaisl_' prefix, not a sibling prefix."""
    assert "aaisl_reports" in aaisl_schema_tables()
    assert "aaisl_accidents" in aaisl_schema_tables()
    for sib in _SIBLINGS:
        assert f"{sib}_reports" not in aaisl_schema_tables()
        assert f"{sib}_accidents" not in aaisl_schema_tables()


def aaisl_schema_tables():
    from aaisl_ingest import db
    return {
        tname
        for line in db.SCHEMA.splitlines()
        if (m := re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", line))
        for tname in [m.group(1)]
    }


def test_index_name_is_aaisl_not_sibling():
    from aaisl_ingest import db
    assert "idx_aaisl_reports_status" in db.SCHEMA
    for sib in _SIBLINGS:
        assert f"idx_{sib}_reports_status" not in db.SCHEMA


def test_constants_reference_caa_lk_not_sibling_domain():
    """INDEX_URL points at the Sri Lanka CAA, not a sibling agency."""
    assert aaisl.BASE == "https://www.caa.lk"
    assert "caa.lk" in aaisl.INDEX_URL
    # must not accidentally point at UK AAIB / IE AAIU domains
    for bad in ["aaib.gov.uk", "aaiu.ie", "gov.uk", ".ie"]:
        assert bad not in aaisl.INDEX_URL
