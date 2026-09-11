"""Offline tests for taiib_ingest.taiib using the saved listing fixture."""
import os
import re

from taiib_ingest import taiib

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── source constants ─────────────────────────────────────────────────────────

def test_index_url_has_trailing_zero():
    # ⚠️ The "-0" suffix is mandatory; without it the page 302-redirects.
    assert taiib.INDEX_URL.endswith("aviacijas-nobeiguma-zinojumi-0")
    assert taiib.BASE == "https://www.taiib.gov.lv"


# ── parse_listing on the live fixture ────────────────────────────────────────

def test_parse_listing_returns_rows():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    assert len(rows) >= 25, f"Expected >=25 rows, got {len(rows)}"


def test_parse_listing_unique_case_ids():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids in listing"


def test_parse_listing_pdf_urls_absolute_media():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    for r in rows:
        assert r["pdf_url"].startswith("https://www.taiib.gov.lv/lv/media/")
        assert r["pdf_url"].endswith("/download?attachment")


def test_parse_listing_case_id_normalized():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    for r in rows:
        cid = r["case_id"]
        assert cid == cid.lower()
        assert re.match(r"^[a-z0-9-]+$", cid), f"Bad case_id: {cid!r}"
        assert not cid.startswith("-") and not cid.endswith("-")


def test_parse_listing_most_rows_have_date():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    with_date = [r for r in rows if r["date_of_occurrence"]]
    assert len(with_date) >= len(rows) - 2, "Too many rows missing a date"


def test_parse_listing_most_rows_have_registration():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    with_reg = [r for r in rows if r["registration"]]
    assert len(with_reg) >= len(rows) - 3, "Too many rows missing a registration"


def test_parse_listing_dates_iso():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in rows:
        if r["date_of_occurrence"]:
            assert iso.match(r["date_of_occurrence"]), r["date_of_occurrence"]


def test_parse_listing_known_row_yl_eva_2024():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "yl-eva-2024-05-04" in by_id, sorted(by_id)[:5]
    row = by_id["yl-eva-2024-05-04"]
    assert row["registration"] == "YL-EVA"
    assert row["date_of_occurrence"] == "2024-05-04"
    assert row["event_class"] == "Accident"
    assert row["lang"] == "lv"
    assert row["pdf_url"].endswith("/lv/media/665/download?attachment")


def test_parse_listing_english_serious_incident_row():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "yl-aap-2023-03-08" in by_id
    row = by_id["yl-aap-2023-03-08"]
    assert row["registration"] == "YL-AAP"
    assert row["event_class"] == "Serious incident"
    assert row["lang"] == "en"


def test_parse_listing_reference_based_case_id():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    ids = {r["case_id"] for r in rows}
    # MID 737 carries "Nr. 4-02/2-24(2-25)" → reference-derived id
    assert "4-02-2-24-2-25" in ids, "reference-derived case_id missing"


def test_parse_listing_collision_breaks_with_media_id():
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    ids = [r["case_id"] for r in rows]
    # Two YL-BBT / 2017-11-23 rows (LV + EN) must be disambiguated by media id.
    bbt = [c for c in ids if c.startswith("yl-bbt-2017-11-23")]
    assert len(bbt) == 2
    assert all("-m" in c for c in bbt)
    assert len(set(bbt)) == 2


def test_parse_listing_intrinsic_order_independent():
    """case_ids are derived from intrinsic fields (reference / reg+date /
    media-id), never from encounter position.  Disambiguating suffixes use the
    intrinsic media-id form '-m<id>', not a positional '-1'/'-2' counter."""
    html = _fixture("taiib_index.html")
    rows = taiib.parse_listing(html)
    ids = sorted(r["case_id"] for r in rows)
    # Re-parsing yields the identical set (deterministic, repeatable).
    assert ids == sorted(r["case_id"] for r in taiib.parse_listing(html))
    # The ONLY disambiguating suffix shape allowed is '-m<digits>' (media id).
    media_ids = {r["media_id"] for r in rows}
    for cid in ids:
        m = re.search(r"-m(\d+)$", cid)
        if m:
            assert m.group(1) in media_ids, f"{cid}: suffix not an intrinsic media id"


# ── field parsers (unit) ──────────────────────────────────────────────────────

def test_parse_date_latvian():
    assert taiib.parse_date("2024. gada 4. maijā") == "2024-05-04"
    assert taiib.parse_date("2018. gada 16. oktobra") == "2018-10-16"


def test_parse_date_english():
    assert taiib.parse_date("on March 8, 2023") == "2023-03-08"
    assert taiib.parse_date("on NOVEMBER 13,2012") == "2012-11-13"
    assert taiib.parse_date("on 8 August 2021") == "2021-08-08"


def test_parse_date_none():
    assert taiib.parse_date("no date here") is None
    assert taiib.parse_date("") is None


def test_parse_registration():
    assert taiib.parse_registration("reģistrācijas Nr. YL-EVA, 2024") == "YL-EVA"
    assert taiib.parse_registration("registration YL-AAP operated") == "YL-AAP"
    assert taiib.parse_registration("registered LY-BDJ (Lithuania)") == "LY-BDJ"


def test_parse_reference():
    assert taiib.parse_reference("Nr. 4-02/2-24(2-25) PAR") == "4-02/2-24(2-25)"
    assert taiib.parse_reference("No.4-02/8-12/5-13 ON") == "4-02/8-12/5-13"
    assert taiib.parse_reference("no ref text") is None


def test_classify_event():
    assert taiib.classify_event("aviācijas nelaimes gadījumu") == "Accident"
    assert taiib.classify_event("aviācijas nopietnu incidentu") == "Serious incident"
    assert taiib.classify_event("serious incident to the aircraft") == "Serious incident"
    assert taiib.classify_event("FINAL REPORT on accident") == "Accident"


def test_normalize_case_id():
    assert taiib._normalize_case_id("4-02/2-24(2-25)") == "4-02-2-24-2-25"
    assert taiib._normalize_case_id("  YL-EVA / 2024 ") == "yl-eva-2024"


def test_make_case_id_priority():
    # reference wins
    assert taiib.make_case_id("4-02/8-12", "YL-X", "2012-10-20", "303") == "4-02-8-12"
    # reg+date next
    assert taiib.make_case_id(None, "YL-EVA", "2024-05-04", "665") == "yl-eva-2024-05-04"
    # date-only (no reg) keeps date + intrinsic media id
    assert taiib.make_case_id(None, None, "2018-10-16", "67") == "taiib-2018-10-16-m67"
    # nothing → bare media id
    assert taiib.make_case_id(None, None, None, "68") == "m68"


# ── ⚠️ non-bleed suite: taiib must NOT collide with tsib / tsb / taic ─────────

def test_no_bleed_module_name_is_taiib():
    assert taiib.__name__.endswith("taiib_ingest.taiib")


def test_no_bleed_url_is_latvia_not_singapore_canada_nz():
    u = taiib.INDEX_URL.lower()
    assert "taiib.gov.lv" in u            # Latvia
    assert "tsib" not in u                # NOT Singapore TSIB
    assert "tsb.gc.ca" not in u           # NOT Canada TSB
    assert "taic" not in u                # NOT NZ TAIC
    assert "mot.gov.sg" not in u


def test_no_bleed_country_default_is_lv():
    from taiib_ingest import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO taiib_accidents (case_id) VALUES ('x')")
    row = conn.execute("SELECT country FROM taiib_accidents WHERE case_id='x'").fetchone()
    assert row["country"] == "LV"          # NOT SG / CA / NZ


def test_no_bleed_table_names():
    from taiib_ingest import db
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "taiib_reports" in tables and "taiib_accidents" in tables
    # exact-match keys must not appear as table names
    for foreign in ("tsib_reports", "tsib_accidents", "tsb_reports",
                    "tsb_accidents", "taic_reports", "taic_accidents"):
        assert foreign not in tables


def test_no_bleed_case_id_count_distinct_from_siblings():
    """Sanity: case_id corpus is the LV catalogue (~31), not a sibling's size."""
    rows = taiib.parse_listing(_fixture("taiib_index.html"))
    assert 25 <= len(rows) <= 45
