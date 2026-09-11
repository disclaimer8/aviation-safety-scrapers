from aaiib_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(aaiib_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_path", "aaiib_ref",
        "title", "event_class", "aircraft", "registration", "date_of_occurrence",
        "location", "operator", "narrative_text", "source_tier", "lang",
        "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(aaiib_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    } <= acols


def test_country_default_ph():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO aaiib_accidents (case_id) VALUES ('AAIIB-2023-RP-C1174')")
    row = conn.execute(
        "SELECT country FROM aaiib_accidents WHERE case_id='AAIIB-2023-RP-C1174'"
    ).fetchone()
    assert row["country"] == "PH"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_aaiib_reports_status" in indexes


def test_tables_are_aaiib_namespaced_not_aaib():
    """Substring-hazard guard: our tables are aaiib_*, NOT aaib_* / aaiu_* etc."""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "aaiib_reports" in tables
    assert "aaiib_accidents" in tables
    # the dense neighbouring family must NOT exist
    for bad in ("aaib_reports", "aaib_accidents", "aaibmy_reports",
                "aaiu_reports", "aaiube_reports"):
        assert bad not in tables


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
