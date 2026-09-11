# tests/test_pipeline.py
import os

from baaid_ingest import baaid, db, pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def test_discover_inserts_all(make_client):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    client = make_client({
        baaid.INDEX_URL: lambda u: _Resp(_fixture_bytes("baaid_accidents.html")),
    })
    n = pipeline.discover(conn, client)
    assert n >= 180
    cnt = conn.execute("SELECT COUNT(*) FROM baaid_reports").fetchone()[0]
    assert cnt == n


def test_discover_idempotent(make_client):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    routes = {baaid.INDEX_URL: lambda u: _Resp(_fixture_bytes("baaid_accidents.html"))}
    first = pipeline.discover(conn, make_client(routes))
    second = pipeline.discover(conn, make_client(routes))
    assert second == 0, "Second discover should insert nothing"
    cnt = conn.execute("SELECT COUNT(*) FROM baaid_reports").fetchone()[0]
    assert cnt == first


def test_discover_rows_english_and_bahamian(make_client):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    client = make_client({baaid.INDEX_URL: lambda u: _Resp(_fixture_bytes("baaid_accidents.html"))})
    pipeline.discover(conn, client)
    row = conn.execute("SELECT lang, pdf_url FROM baaid_reports LIMIT 1").fetchone()
    assert row["lang"] == "en"
    assert "baaid.org" in row["pdf_url"]


def test_build_skips_scanned():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO baaid_reports (case_id, status, source_tier, narrative_text, pdf_url) "
        "VALUES ('scan1', ?, 'scanned', '', 'http://x/scan.pdf')",
        (db.STATUS_PARSED,),
    )
    conn.commit()
    built = pipeline.build(conn)
    assert built == 0
    st = conn.execute("SELECT status FROM baaid_reports WHERE case_id='scan1'").fetchone()["status"]
    assert st == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM baaid_accidents").fetchone()[0] == 0


def test_build_skips_short_narrative():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    conn.execute(
        "INSERT INTO baaid_reports (case_id, status, source_tier, narrative_text, pdf_url) "
        "VALUES ('short1', ?, 'short', 'tiny', 'http://x/s.pdf')",
        (db.STATUS_PARSED,),
    )
    conn.commit()
    assert pipeline.build(conn) == 0
    st = conn.execute("SELECT status FROM baaid_reports WHERE case_id='short1'").fetchone()["status"]
    assert st == db.STATUS_SKIPPED


def test_build_emits_bs_accident():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    narrative = "N123AB Piper accident narrative. " * 10
    conn.execute(
        "INSERT INTO baaid_reports (case_id, status, source_tier, narrative_text, "
        "pdf_url, registration, aircraft, date_of_occurrence, event_class) "
        "VALUES ('320f20-abc', ?, 'pdf', ?, 'http://x/a.pdf', 'N123AB', 'Piper PA-28', "
        "'2022-05-01', 'OCC-2022/0009')",
        (db.STATUS_PARSED, narrative),
    )
    conn.commit()
    assert pipeline.build(conn) == 1
    row = conn.execute("SELECT * FROM baaid_accidents WHERE case_id='320f20-abc'").fetchone()
    assert row["country"] == "BS"
    assert row["registration"] == "N123AB"
    assert row["site_slug"] == "320f20-abc"
    assert row["report_type"] == "OCC-2022/0009"


class _Resp:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass
