from aaib_ingest import db

def test_init_schema_creates_tables_and_inserts():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"aaib_reports", "aaib_accidents"} <= tables
    conn.execute(
        "INSERT INTO aaib_reports (slug, status, discovered_at) VALUES (?,?,?)",
        ("foo-bar", db.STATUS_NEW, db.now_ms()),
    )
    row = conn.execute("SELECT status FROM aaib_reports WHERE slug='foo-bar'").fetchone()
    assert row["status"] == "new"

def test_status_constants():
    assert (db.STATUS_NEW, db.STATUS_FETCHED, db.STATUS_PARSED, db.STATUS_BUILT, db.STATUS_SKIPPED) == \
        ("new", "fetched", "parsed", "built", "skipped")
