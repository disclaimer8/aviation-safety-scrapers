from beacg_ingest import db


def test_schema_creates_tables():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "beacg_reports" in tables
    assert "beacg_accidents" in tables
    conn.close()


def test_accidents_columns_match_ciaiac_shape():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(beacg_accidents)").fetchall()]
    assert cols == [
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause",
        "source_url", "report_type", "site_slug", "built_at",
    ]
    conn.close()


def test_country_defaults_to_cg():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO beacg_accidents (case_id) VALUES ('x')")
    row = conn.execute("SELECT country FROM beacg_accidents WHERE case_id='x'").fetchone()
    assert row["country"] == "CG"
    conn.close()


def test_init_schema_idempotent():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    db.init_schema(conn)  # must not raise
    conn.close()
