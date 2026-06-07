from ciaiauy_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(ciaiauy_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_url_es", "pdf_url_en", "pdf_path",
        "title", "event_class", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier", "lang",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(ciaiauy_accidents)")}
    # Column set MUST be identical to ciaiac_accidents for P2 prod sync.
    assert acols == {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    }


def test_country_default_uy():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO ciaiauy_accidents (case_id) VALUES ('caso-611')")
    row = conn.execute(
        "SELECT country FROM ciaiauy_accidents WHERE case_id='caso-611'"
    ).fetchone()
    assert row["country"] == "UY"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_ciaiauy_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
