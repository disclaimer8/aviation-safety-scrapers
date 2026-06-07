from araib_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"araib_reports", "araib_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO araib_accidents (case_id) VALUES ('aar2404')")
    row = conn.execute(
        "SELECT country, lang FROM araib_accidents").fetchone()
    assert row["country"] == "KR"
    assert row["lang"] == "en"


def test_reports_idx_pk(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO araib_reports (idx, dtl_url) "
                 "VALUES ('266499', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO araib_reports (idx, dtl_url) "
                     "VALUES ('266499', 'u2')")


def test_reports_dtl_url_unique(conn):
    import sqlite3
    import pytest
    conn.execute("INSERT INTO araib_reports (idx, dtl_url) "
                 "VALUES ('266499', 'u1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO araib_reports (idx, dtl_url) "
                     "VALUES ('265223', 'u1')")


def test_init_idempotent(conn):
    db.init_schema(conn)
