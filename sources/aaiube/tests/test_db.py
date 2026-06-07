from aaiube_ingest import db


def test_schema_tables(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aaiube_reports", "aaiube_accidents"} <= tables


def test_accidents_country_default(conn):
    conn.execute("INSERT INTO aaiube_accidents (case_id) VALUES ('aaiu-2022-09-12-01')")
    row = conn.execute("SELECT country FROM aaiube_accidents").fetchone()
    assert row["country"] == "BE"


def test_reports_pdf_url_pk(conn):
    conn.execute(
        "INSERT INTO aaiube_reports (pdf_url, case_id) VALUES ('u1', 'c1')")
    # same pdf_url → IntegrityError on PK
    import sqlite3
    try:
        conn.execute(
            "INSERT INTO aaiube_reports (pdf_url, case_id) VALUES ('u1', 'c2')")
        assert False, "expected PK violation"
    except sqlite3.IntegrityError:
        pass


def test_init_idempotent(conn):
    db.init_schema(conn)
