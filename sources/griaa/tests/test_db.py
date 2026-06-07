from griaa_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(griaa_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_url_es", "pdf_url_en", "pdf_path",
        "title", "event_class", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier", "lang",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(griaa_accidents)")}
    # Column set MUST be identical to ciaiac_accidents (prod sync depends on it).
    assert acols == {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    }


def test_country_default_co():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO griaa_accidents (case_id) VALUES ('COL-24-58-DIACC')")
    row = conn.execute(
        "SELECT country FROM griaa_accidents WHERE case_id='COL-24-58-DIACC'"
    ).fetchone()
    assert row["country"] == "CO"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_griaa_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
