"""Offline tests for aaiubg_ingest.aaiubg using saved live HTML fixtures.

Includes a non-bleed source-key suite asserting aaiubg is kept distinct from
the substring-adjacent sources aaiu (Ireland), aaiube (Belgium), aaib (UK).
"""
import os
import re

import pytest

from aaiubg_ingest import aaiubg, db

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
# case_id is either <REG>_<YYYY-MM-DD> or a 'bg-...' filename fallback.
CASE_ID_RE = re.compile(r"^([A-Z0-9]{2,3}-?[A-Z0-9]{2,6}_\d{4}-\d{2}-\d{2}|bg-[a-z0-9-]+)$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── iter_year_urls ──────────────────────────────────────────────────────────

def test_iter_year_urls_returns_list():
    urls = aaiubg.iter_year_urls(_fixture("aaiubg_index.html"))
    assert isinstance(urls, list)
    assert len(urls) >= 7, f"Expected >=7 year URLs, got {len(urls)}"


def test_iter_year_urls_all_absolute_and_category_193():
    urls = aaiubg.iter_year_urls(_fixture("aaiubg_index.html"))
    for u in urls:
        assert u.startswith("https://www.mtc.government.bg/en/category/193/"), u


def test_iter_year_urls_includes_2024():
    urls = aaiubg.iter_year_urls(_fixture("aaiubg_index.html"))
    assert any("2024" in u for u in urls)


def test_iter_year_urls_no_duplicates():
    urls = aaiubg.iter_year_urls(_fixture("aaiubg_index.html"))
    assert len(urls) == len(set(urls))


# ── parse_listing — multi-report year (2016/2017) ───────────────────────────

def test_parse_2016_2017_multi_rows():
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2016_2017.html"))
    assert len(rows) == 6, f"Expected 6 reports in 2016/2017 page, got {len(rows)}"


def test_parse_2016_2017_case_id_format():
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2016_2017.html"))
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"Bad case_id {r['case_id']!r}"


def test_parse_2016_2017_known_row():
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2016_2017.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "TC-ATF_2016-09-08" in by_id
    r = by_id["TC-ATF_2016-09-08"]
    assert r["event_class"] == "Serious incident"
    assert r["registration"] == "TC-ATF"
    assert r["date_of_occurrence"] == "2016-09-08"
    assert r["pdf_url"].startswith("https://www.mtc.government.bg/")
    assert r["pdf_url"].endswith(".pdf")


def test_parse_2016_2017_no_registration_fallback_case_id():
    """The unregistered R22 helicopter report falls back to a 'bg-' case_id."""
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2016_2017.html"))
    no_reg = [r for r in rows if r["registration"] is None]
    assert no_reg, "Expected at least one without-registration report"
    for r in no_reg:
        assert r["case_id"].startswith("bg-"), r["case_id"]


# ── parse_listing — mixed-date-format year (2020) ───────────────────────────

def test_parse_2020_rows_and_date_formats():
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2020.html"))
    assert len(rows) == 6
    by_id = {r["case_id"]: r for r in rows}
    # 'D Month YYYY' airprox date form
    assert "D-ASXP_2018-08-12" in by_id
    # numeric DD.MM.YYYY date form
    assert "LZ-ACS_2020-05-09" in by_id


# ── parse_listing — single-report year (2024) ───────────────────────────────

def test_parse_2024_single_row():
    rows = aaiubg.parse_listing(_fixture("aaiubg_year_2024.html"))
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "G-EZBV_2022-11-17"
    assert r["event_class"] == "Serious incident"


# ── cross-fixture invariants ────────────────────────────────────────────────

def test_all_pdf_urls_absolute_and_pdf():
    for fx in ("aaiubg_year_2016_2017.html", "aaiubg_year_2020.html", "aaiubg_year_2024.html"):
        for r in aaiubg.parse_listing(_fixture(fx)):
            assert r["pdf_url"].startswith("https://"), r["pdf_url"]
            assert r["pdf_url"].endswith(".pdf"), r["pdf_url"]


def test_all_dates_iso_when_present():
    for fx in ("aaiubg_year_2016_2017.html", "aaiubg_year_2020.html", "aaiubg_year_2024.html"):
        for r in aaiubg.parse_listing(_fixture(fx)):
            if r["date_of_occurrence"]:
                assert ISO_RE.match(r["date_of_occurrence"]), r["date_of_occurrence"]


def test_no_duplicate_case_ids_within_page():
    for fx in ("aaiubg_year_2016_2017.html", "aaiubg_year_2020.html", "aaiubg_year_2024.html"):
        ids = [r["case_id"] for r in aaiubg.parse_listing(_fixture(fx))]
        assert len(ids) == len(set(ids)), f"dup case_id in {fx}: {ids}"


def test_titles_nonempty():
    for r in aaiubg.parse_listing(_fixture("aaiubg_year_2020.html")):
        assert r["title"]


# ── make_case_id / _normalize_case_id ───────────────────────────────────────

def test_make_case_id_reg_plus_date():
    assert aaiubg.make_case_id("LZ-PTS", "2022-08-08", "x.pdf") == "LZ-PTS_2022-08-08"


def test_make_case_id_filename_fallback():
    cid = aaiubg.make_case_id(None, None, "final_report_r22_eng_ft.pdf")
    assert cid.startswith("bg-")
    assert "r22" in cid


def test_make_case_id_is_intrinsic_order_independent():
    """Same report → same case_id regardless of which year-page yields it."""
    a = aaiubg.make_case_id("LZ-LDM", "2018-07-16", "en_final_report_lz_ldm_16_07_2018p.pdf")
    b = aaiubg.make_case_id("LZ-LDM", "2018-07-16", "totally_different_name.pdf")
    assert a == b == "LZ-LDM_2018-07-16"


def test_normalize_case_id_idempotent_and_uppercases_reg():
    assert aaiubg._normalize_case_id("lz-pts_2022-08-08") == "LZ-PTS_2022-08-08"
    once = aaiubg._normalize_case_id("LZ-PTS_2022-08-08")
    assert aaiubg._normalize_case_id(once) == once  # idempotent


# ── NON-BLEED source-key suite (aaiubg vs aaiu / aaiube / aaib) ─────────────

def test_source_key_is_aaiubg_not_neighbours():
    """Module/table identifiers must be exactly 'aaiubg', never a neighbour."""
    assert aaiubg.__name__.endswith("aaiubg")
    assert "aaiubg_reports" in db.SCHEMA
    assert "aaiubg_accidents" in db.SCHEMA


def test_schema_has_no_neighbour_source_tables():
    """Schema must not accidentally reference aaiu/aaiube/aaib tables."""
    for bad in ("aaiu_reports", "aaiube_reports", "aaib_reports",
                "aaiu_accidents", "aaiube_accidents", "aaib_accidents"):
        assert bad not in db.SCHEMA, f"neighbour table leaked: {bad}"


def test_table_names_exact_match_not_substring():
    """Exact-match guard: 'aaiu' / 'aaiube' / 'aaib' must NOT be whole table
    names even though they are substrings of 'aaiubg'."""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "aaiubg_reports" in names
    assert "aaiubg_accidents" in names
    for bad in ("aaiu_reports", "aaiube_reports", "aaib_reports",
                "aaiu_accidents", "aaiube_accidents", "aaib_accidents",
                "aaiib_reports", "aaid_reports", "aaibmy_reports"):
        assert bad not in names, f"neighbour table created: {bad}"


def test_country_default_is_bg_not_neighbour():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaiubg_accidents (case_id) VALUES ('LZ-X_2020-01-01')")
    c = conn.execute("SELECT country FROM aaiubg_accidents").fetchone()["country"]
    assert c == "BG"  # Bulgaria, not IE/BE/GB
