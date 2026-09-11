# tests/test_nonbleed.py
"""Non-bleed guards: source key 'baaid' must NEVER collide with 'aaid' (Kenya,
source 45e9), which is a substring of 'baaid'.  EXACT-MATCH keys everywhere."""
import re

from baaid_ingest import baaid, db


def _table_names(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_tables_are_baaid_not_aaid():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = _table_names(conn)
    assert "baaid_reports" in names
    assert "baaid_accidents" in names
    # The bare aaid_* tables (Kenya's) must NOT exist in this schema.
    assert "aaid_reports" not in names
    assert "aaid_accidents" not in names


def test_no_aaid_table_via_exact_match():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = _table_names(conn)
    # Every table that ends in '_reports'/'_accidents' must start with 'baaid_'
    for n in names:
        if n.endswith("_reports") or n.endswith("_accidents"):
            assert n.startswith("baaid_"), f"Non-baaid table leaked: {n!r}"
            # exact-match: stripping the 'b' must NOT yield a valid 'aaid_' table
            assert not n.startswith("aaid_")


def test_schema_string_uses_baaid_exact():
    # SCHEMA references baaid_* exactly; the only 'aaid' occurrences are inside
    # 'baaid' (i.e. always preceded by 'b').
    for m in re.finditer(r"aaid", db.SCHEMA):
        start = m.start()
        assert start > 0 and db.SCHEMA[start - 1] == "b", (
            f"Bare 'aaid' (not 'baaid') at offset {start} in SCHEMA"
        )


def test_country_default_is_bs_not_kenya():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO baaid_accidents (case_id) VALUES ('x')")
    conn.commit()
    row = conn.execute("SELECT country FROM baaid_accidents WHERE case_id='x'").fetchone()
    assert row["country"] == "BS", "Bahamas country default must be BS (not Kenya 'KE')"


def test_base_url_is_bahamas_not_kenya():
    assert "baaid.org" in baaid.BASE
    # ensure we never point at a bare 'aaid' Kenya host
    assert "aaid.go.ke" not in baaid.BASE


def test_make_site_slug_keyed_on_case_id():
    from baaid_ingest import text
    slug = text.make_site_slug("320f20_ABC123")
    assert slug == "320f20-abc123"
    # never produces an 'aaid' slug from a 'baaid' case_id
    assert not slug.startswith("aaid")
