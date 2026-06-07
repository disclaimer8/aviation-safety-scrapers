from tsb_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(tsb_reports)")}
    assert {
        "case_id", "report_url", "title", "occurrence_type", "aircraft",
        "registration", "date_of_occurrence", "location", "operator",
        "occurrence_status", "narrative_text", "source_tier",
        "status", "discovered_at", "updated_at",
    } <= rcols
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(tsb_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause",
        "source_url", "report_type", "site_slug", "built_at",
    } <= acols


def test_occurrence_status_and_pipeline_status_are_distinct():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(tsb_reports)")}
    assert "occurrence_status" in rcols
    assert "status" in rcols


def test_country_default_ca():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO tsb_accidents (case_id) VALUES ('A11Q0170')")
    row = conn.execute(
        "SELECT country FROM tsb_accidents WHERE case_id='A11Q0170'"
    ).fetchone()
    assert row["country"] == "CA"


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_BUILT == "built"
