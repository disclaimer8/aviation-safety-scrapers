from dgacgt_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(dgacgt_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_path", "title", "report_no",
        "event_class", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier", "lang",
        "year", "status", "discovered_at", "updated_at",
    } <= rcols


def test_accidents_columns_identical_to_ciaiac_shape():
    # The dgacgt_accidents column set MUST match the standard *_accidents shape
    # (prod sync in P2 depends on this exact set).
    conn = db.connect(":memory:")
    db.init_schema(conn)
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(dgacgt_accidents)")}
    assert acols == {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    }


def test_country_default_gt():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO dgacgt_accidents (case_id) VALUES ('TG-MIC-2024-07-31')")
    row = conn.execute(
        "SELECT country FROM dgacgt_accidents WHERE case_id='TG-MIC-2024-07-31'"
    ).fetchone()
    assert row["country"] == "GT"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_dgacgt_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
