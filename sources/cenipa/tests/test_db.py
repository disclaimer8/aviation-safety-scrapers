from cenipa_ingest import db


def test_schema_tables_and_columns():
    conn = db.connect(":memory:")
    db.init_schema(conn)

    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(cenipa_reports)")}
    assert {
        "case_id", "report_url", "pdf_url", "pdf_url_pt", "pdf_url_en", "pdf_path",
        "title", "classificacao", "occurrence_type", "aircraft", "registration",
        "date_of_occurrence", "location", "operator", "narrative_text", "source_tier",
        "lang", "status", "discovered_at", "updated_at",
    } <= rcols

    acols = {r["name"] for r in conn.execute("PRAGMA table_info(cenipa_accidents)")}
    assert {
        "case_id", "event_date", "aircraft", "registration", "operator", "location",
        "country", "narrative_text", "probable_cause", "source_url", "report_type",
        "site_slug", "built_at",
    } <= acols


def test_country_default_br():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute("INSERT INTO cenipa_accidents (case_id) VALUES ('A-076/CENIPA/2023')")
    row = conn.execute(
        "SELECT country FROM cenipa_accidents WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row["country"] == "BR"


def test_pdf_url_pt_en_columns_exist():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO cenipa_reports (case_id, pdf_url_pt, pdf_url_en) "
        "VALUES ('IG-029/CENIPA/2025', 'http://pt.pdf', 'http://en.pdf')"
    )
    row = conn.execute(
        "SELECT pdf_url_pt, pdf_url_en FROM cenipa_reports WHERE case_id='IG-029/CENIPA/2025'"
    ).fetchone()
    assert row["pdf_url_pt"] == "http://pt.pdf"
    assert row["pdf_url_en"] == "http://en.pdf"


def test_lang_column_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO cenipa_reports (case_id, lang) VALUES ('TEST-001', 'pt')"
    )
    row = conn.execute(
        "SELECT lang FROM cenipa_reports WHERE case_id='TEST-001'"
    ).fetchone()
    assert row["lang"] == "pt"


def test_classificacao_column_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO cenipa_reports (case_id, classificacao) "
        "VALUES ('A-001/CENIPA/2024', 'ACIDENTE')"
    )
    row = conn.execute(
        "SELECT classificacao FROM cenipa_reports WHERE case_id='A-001/CENIPA/2024'"
    ).fetchone()
    assert row["classificacao"] == "ACIDENTE"


def test_status_index_exists():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    indexes = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert "idx_cenipa_reports_status" in indexes


def test_now_ms_and_status_constants():
    assert isinstance(db.now_ms(), int)
    assert db.STATUS_NEW == "new"
    assert db.STATUS_FETCHED == "fetched"
    assert db.STATUS_PARSED == "parsed"
    assert db.STATUS_BUILT == "built"
    assert db.STATUS_SKIPPED == "skipped"
