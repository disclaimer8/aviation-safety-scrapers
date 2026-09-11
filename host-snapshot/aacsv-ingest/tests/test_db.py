from aacsv_ingest import db


def test_schema_creates_tables(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "aacsv_reports" in names
    assert "aacsv_accidents" in names


def test_accidents_country_default_sv(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    conn.execute("INSERT INTO aacsv_accidents (case_id) VALUES ('X')")
    conn.commit()
    row = conn.execute("SELECT country FROM aacsv_accidents WHERE case_id='X'").fetchone()
    assert row["country"] == "SV"


def test_accidents_columns_match_ciaiac():
    expected = {
        "case_id", "event_date", "aircraft", "registration", "operator",
        "location", "country", "narrative_text", "probable_cause", "source_url",
        "report_type", "site_slug", "built_at",
    }
    cols = set()
    for line in db.SCHEMA.splitlines():
        line = line.strip()
        if line and line[0].isalpha() and not line.upper().startswith(("CREATE", "PRIMARY")):
            cols.add(line.split()[0])
    assert expected <= cols
