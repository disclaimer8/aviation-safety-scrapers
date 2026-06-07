from bfu_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(bfu_reports)")}
    assert {
        "case_id", "detail_url", "title", "event_class", "aircraft",
        "registration", "date_of_occurrence", "location", "operator",
        "pdf_url", "pdf_path", "narrative_text", "source_tier",
        "status", "discovered_at", "updated_at",
    } <= rcols
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(bfu_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause",
        "source_url", "report_type", "site_slug", "built_at",
    } <= acols


def test_country_default_de():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO bfu_accidents (case_id) VALUES ('BFU24-0173-3X')")
    row = conn.execute(
        "SELECT country FROM bfu_accidents WHERE case_id='BFU24-0173-3X'"
    ).fetchone()
    assert row["country"] == "DE"


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_BUILT == "built"
