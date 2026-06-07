from jtsb_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(jtsb_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_path", "jp_pdf_url",
        "title", "report_type", "category", "flight_phase",
        "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(jtsb_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    } <= acols


def test_jp_pdf_url_column_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(jtsb_reports)")}
    assert "jp_pdf_url" in rcols


def test_category_and_flight_phase_columns_exist():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(jtsb_reports)")}
    assert "category" in rcols
    assert "flight_phase" in rcols


def test_country_default_jp():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO jtsb_accidents (case_id) VALUES ('AA2024-7-3')")
    row = conn.execute(
        "SELECT country FROM jtsb_accidents WHERE case_id='AA2024-7-3'"
    ).fetchone()
    assert row["country"] == "JP"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_jtsb_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
