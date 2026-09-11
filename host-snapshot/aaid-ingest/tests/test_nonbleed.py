# tests/test_nonbleed.py
"""
Non-bleed guards: the source key `aaid` and its tables must NOT collide with
the nearby keys aaib / aaiu / aaiube / aaibmy / aaiib (exact-match only).

These protect against substring confusion when 40+ sources share a prod DB.
"""
import re

from aaid_ingest import aaid, db

# Keys that are dangerously close to 'aaid' and must stay distinct.
_NEIGHBOURS = ["aaib", "aaiu", "aaiube", "aaibmy", "aaiib"]


def test_module_names_are_aaid_not_neighbours():
    assert aaid.__name__ == "aaid_ingest.aaid"
    assert aaid.BASE == "https://aaid.transport.go.ke"
    assert aaid.INDEX_URL.endswith("/final-reports")
    for n in _NEIGHBOURS:
        assert n not in aaid.BASE.replace("aaid", "")  # 'aaid' is the only token


def test_schema_uses_aaid_tables_exactly():
    sql = db.SCHEMA
    assert "aaid_reports" in sql
    assert "aaid_accidents" in sql
    assert "idx_aaid_reports_status" in sql
    # No neighbour table names leaked in.
    for n in _NEIGHBOURS:
        assert f"{n}_reports" not in sql, f"{n}_reports leaked into schema"
        assert f"{n}_accidents" not in sql, f"{n}_accidents leaked into schema"


def test_table_names_exact_match_in_live_db():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "aaid_reports" in names
    assert "aaid_accidents" in names
    for n in _NEIGHBOURS:
        assert f"{n}_reports" not in names
        assert f"{n}_accidents" not in names


def test_aaid_token_is_not_a_substring_hit_of_neighbours():
    """'aaid' must be matched exactly, never as a substring of aaib/aaiu/etc."""
    token = "aaid"
    for n in _NEIGHBOURS:
        # exact inequality
        assert token != n
        # 'aaid' is not contained in any neighbour and vice-versa
        assert token not in n, f"{token} is a substring of {n}"
        assert n not in token, f"{n} is a substring of {token}"


def test_country_default_is_ke_not_neighbour_country():
    """KE (Kenya) — must not accidentally inherit GB(aaib)/IE(aaiu) etc."""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaid_accidents (case_id) VALUES ('X-2024-01-01')")
    c = conn.execute("SELECT country FROM aaid_accidents").fetchone()["country"]
    assert c == "KE"
    assert c not in ("GB", "IE", "MY", "BE")  # aaib/aaiu/aaibmy/aaiube/aaiube homelands
