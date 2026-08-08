from cins_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(cins_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_url_es", "pdf_url_en", "pdf_path",
        "title", "event_class", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier", "lang",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(cins_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    } <= acols


def test_country_default_rs():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO cins_accidents (case_id) VALUES ('01-24')")
    row = conn.execute(
        "SELECT country FROM cins_accidents WHERE case_id='01-24'"
    ).fetchone()
    assert row["country"] == "RS"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_cins_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
