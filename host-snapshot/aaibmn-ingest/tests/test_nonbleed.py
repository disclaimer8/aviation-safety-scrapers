"""Non-bleed guard: aaibmn (Mongolia) must NOT collide with the taken `aaib`
(UK) source key anywhere — table names, country, slug fallback, source_url host.
"""
import pathlib

from aaibmn_ingest import db, aaibmn, text

PKG_DIR = pathlib.Path(__file__).resolve().parent.parent / "aaibmn_ingest"


def test_tables_are_aaibmn_not_aaib():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "aaibmn_reports" in names
    assert "aaibmn_accidents" in names
    # the bare UK key must not appear as a standalone table
    assert "aaib_reports" not in names
    assert "aaib_accidents" not in names


def test_country_is_mn_not_gb():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaibmn_accidents (case_id) VALUES ('x')")
    assert conn.execute(
        "SELECT country FROM aaibmn_accidents").fetchone()["country"] == "MN"


def test_slug_fallback_is_aaibmn():
    assert text.make_site_slug(None, None, None) == "crash-aaibmn"
    assert "aaib-" not in text.make_site_slug(None, None, None)


def test_base_host_is_mongolia():
    assert aaibmn.BASE == "https://aaib.gov.mn"
    assert ".gov.mn" in aaibmn.LISTING_URL


def test_no_bare_aaib_table_token_in_source():
    """No 'aaib_reports' / 'aaib_accidents' (UK) token anywhere in the package
    source — only the aaibmn_ prefixed names."""
    bad = []
    for py in PKG_DIR.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        for token in ("aaib_reports", "aaib_accidents", "'aaib'", '"aaib"'):
            if token in src:
                bad.append((py.name, token))
    assert not bad, f"bare-aaib tokens leaked: {bad}"


def test_count_aaibmn_table_refs_present():
    """Sanity-count: aaibmn_ table tokens are used across the package."""
    total = 0
    for py in PKG_DIR.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        total += src.count("aaibmn_reports") + src.count("aaibmn_accidents")
    assert total >= 5, f"expected aaibmn_ table refs across modules, got {total}"
