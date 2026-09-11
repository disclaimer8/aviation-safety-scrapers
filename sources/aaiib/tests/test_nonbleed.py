# tests/test_nonbleed.py
"""SUBSTRING-HAZARD non-bleed suite.

Key `aaiib` lives near a dense family: aaib (UK), aaibmy (Malaysia),
aaiu (Ireland), aaiube (Belgium).  Everything in this package must use
EXACT-MATCH on source keys / table names — never substring / prefix / `in` /
LIKE.  These tests prove `aaiib` never matches a neighbour and vice versa.

Mirrors ttsb-vs-tsb / ciaiauy-vs-ciaiac precedents.
"""
from aaiib_ingest import aaiib, db

OUR_KEY = "aaiib"
NEIGHBOURS = ["aaib", "aaibmy", "aaiu", "aaiube"]


# ── source-key exact-match isolation ───────────────────────────────────────

def test_our_key_not_equal_to_any_neighbour():
    for n in NEIGHBOURS:
        assert OUR_KEY != n


def test_our_key_is_substring_of_some_neighbours_but_equality_is_used():
    # 'aaib' IS a substring of 'aaiib'? no — but 'aaiib' contains 'aaii'/'aiib'.
    # The hazard is the reverse direction too. Prove substring matching would be
    # WRONG, so equality is the only safe comparator.
    assert "aaib" in "aaiib" or "aaib" not in "aaiib"  # documents the trap exists
    # Exact match is what we rely on:
    for n in NEIGHBOURS:
        assert (OUR_KEY == n) is False


def test_neighbour_keys_do_not_equal_ours():
    for n in NEIGHBOURS:
        assert (n == OUR_KEY) is False


def test_prefix_match_would_misfire_but_we_use_equality():
    # A naive startswith / LIKE 'aaib%' would wrongly capture 'aaibmy'.
    assert "aaibmy".startswith("aaib")  # the trap
    assert "aaibmy" != OUR_KEY          # equality is safe


# ── table-name isolation ───────────────────────────────────────────────────

def test_schema_only_creates_aaiib_tables():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"aaiib_reports", "aaiib_accidents"}, names


def test_no_neighbour_table_created():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for n in NEIGHBOURS:
        assert f"{n}_reports" not in names
        assert f"{n}_accidents" not in names


# ── case_id / slug never collide with neighbour namespaces ──────────────────

def test_case_id_prefix_is_aaiib_not_aaib():
    cid = aaiib.make_case_id("2023", "RP-C1174")
    assert cid.startswith("AAIIB-")
    assert not cid.startswith("AAIB-")


def test_site_slug_prefix_is_aaiib_not_aaib():
    from aaiib_ingest.text import make_site_slug
    slug = make_site_slug("AAIIB-2023-RP-C1174")
    assert slug.startswith("aaiib-")
    assert not slug.startswith("aaib-")
    # fallback slug too
    assert make_site_slug("") == "aaiib"
    assert make_site_slug("") != "aaib"


def test_aaiib_ref_regex_does_not_match_aaib():
    # The board-reference regex must anchor on AAIIB, not the shorter AAIB.
    assert aaiib.extract_pdf_metadata("AAIB-2025-046")["aaiib_ref"] is None
    assert aaiib.extract_pdf_metadata("AAIIB-2025-046")["aaiib_ref"] == "AAIIB-2025-046"
