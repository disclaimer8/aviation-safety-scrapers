from ansv_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(ansv_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_path",
        "title", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "report_type", "narrative_text", "source_tier",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(ansv_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    } <= acols


def test_country_default_it():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO ansv_accidents (case_id) VALUES ('I-COLK_2024-03-16')")
    row = conn.execute(
        "SELECT country FROM ansv_accidents WHERE case_id='I-COLK_2024-03-16'"
    ).fetchone()
    assert row["country"] == "IT"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_ansv_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
