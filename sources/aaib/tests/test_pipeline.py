import os
from aaib_ingest import db, govuk, pipeline
from tests.conftest import FakeResp


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _search_routes(slugs):
    def page(url, params):
        if params["start"] != 0:
            return FakeResp(json_data={"total": len(slugs), "results": []})
        return FakeResp(json_data={"total": len(slugs), "results": [
            {"link": f"/aaib-reports/{s}", "title": s.upper(), "public_timestamp": "2026-01-01"} for s in slugs
        ]})
    return {govuk.SEARCH_URL: page}


def test_discover_inserts_new_rows(make_client):
    conn = _conn()
    client = make_client(_search_routes(["a", "b"]))
    assert pipeline.discover(conn, client) == 2
    rows = conn.execute("SELECT slug, status FROM aaib_reports ORDER BY slug").fetchall()
    assert [r["slug"] for r in rows] == ["a", "b"]
    assert rows[0]["status"] == "new"


def test_discover_delta_skips_known(make_client):
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('a','new',1)")
    client = make_client(_search_routes(["a", "b"]))
    inserted = pipeline.discover(conn, client, full=False)
    assert inserted == 1  # only "b" is new


def test_fetch_enriches_and_picks_pdf(make_client, tmp_path):
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('x','new',1)")
    content = {"details": {
        "body": "<p>Summary &amp; cause</p>",
        "metadata": {"aircraft_type": "Leonardo AW139", "registration": "G-CIMU",
                     "location": "Norwich", "date_of_occurrence": "2022-06-13",
                     "report_type": "field-investigation", "aircraft_category": ["commercial-rotorcraft"]},
        "attachments": [
            {"content_type": "application/pdf", "title": "Glossary", "url": "https://gov.uk/g.pdf"},
            {"content_type": "application/pdf", "title": "Main", "url": "https://gov.uk/m.pdf"},
        ],
    }}
    routes = {
        f"{govuk.CONTENT_URL}/aaib-reports/x": FakeResp(json_data=content),
        "https://gov.uk/m.pdf": FakeResp(content=b"%PDF-bytes"),
    }
    client = make_client(routes)
    assert pipeline.fetch(conn, client, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM aaib_reports WHERE slug='x'").fetchone()
    assert row["status"] == "fetched"
    assert row["registration"] == "G-CIMU"
    assert row["aircraft_category"] == "commercial-rotorcraft"
    assert row["body_text"] == "Summary & cause"
    assert row["pdf_url"] == "https://gov.uk/m.pdf"
    assert os.path.exists(row["pdf_path"])


def test_parse_prefers_pdf_then_falls_back(monkeypatch):
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug,status,pdf_path,body_text) VALUES ('big','fetched','b.pdf',?)",
                 ("short body",))
    conn.execute("INSERT INTO aaib_reports (slug,status,pdf_path,body_text) VALUES ('small','fetched','s.pdf',?)",
                 ("x" * 700,))
    monkeypatch.setattr(pipeline, "extract_text",
                        lambda p: ("P" * 1000) if p == "b.pdf" else "tiny")
    assert pipeline.parse(conn) == 2
    big = conn.execute("SELECT narrative_text,source_tier FROM aaib_reports WHERE slug='big'").fetchone()
    small = conn.execute("SELECT narrative_text,source_tier FROM aaib_reports WHERE slug='small'").fetchone()
    assert big["source_tier"] == "pdf" and len(big["narrative_text"]) == 1000
    assert small["source_tier"] == "body" and len(small["narrative_text"]) == 700


def test_build_maps_and_skips_non_occurrence():
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug,status,aircraft_type,registration,location,"
                 "date_of_occurrence,narrative_text,report_type) "
                 "VALUES ('good','parsed','Leonardo AW139','G-CIMU','Norwich','2022-06-13','narr','field-investigation')")
    conn.execute("INSERT INTO aaib_reports (slug,status,aircraft_type,registration,narrative_text) "
                 "VALUES ('svc','parsed',NULL,NULL,'annual report')")
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aaib_accidents WHERE case_id='good'").fetchone()
    assert acc["site_slug"] == "crash-leonardo-aw139-g-cimu-norwich"
    assert acc["country"] == "GB"
    assert acc["source_url"] == "https://www.gov.uk/aaib-reports/good"
    assert conn.execute("SELECT status FROM aaib_reports WHERE slug='svc'").fetchone()["status"] == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM aaib_accidents").fetchone()[0] == 1


def test_fetch_with_no_pdf(make_client, tmp_path):
    """A report whose attachments contain only a non-PDF and a glossary PDF
    should still be fetched with body_text populated and pdf_url/pdf_path NULL."""
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('nopdf','new',1)")
    content = {"details": {
        "body": "<p>Body text here</p>",
        "metadata": {"aircraft_type": "Cessna 172", "registration": "G-TEST",
                     "location": "London", "date_of_occurrence": "2025-01-01",
                     "report_type": "field-investigation", "aircraft_category": ["general-aviation"]},
        "attachments": [
            {"content_type": "text/html", "title": "Report HTML", "url": "https://gov.uk/r.html"},
            {"content_type": "application/pdf", "title": "Glossary", "url": "https://gov.uk/g.pdf"},
        ],
    }}
    routes = {
        f"{govuk.CONTENT_URL}/aaib-reports/nopdf": FakeResp(json_data=content),
    }
    client = make_client(routes)
    assert pipeline.fetch(conn, client, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM aaib_reports WHERE slug='nopdf'").fetchone()
    assert row["status"] == "fetched"
    assert row["pdf_url"] is None
    assert row["pdf_path"] is None
    assert row["body_text"] == "Body text here"


def test_fetch_isolates_per_report_errors(make_client, tmp_path):
    """A failing report (500) must not abort the fetch run; subsequent rows
    must still be processed, and the failed row stays STATUS_NEW for retry."""
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('err','new',1)")
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('ok','new',2)")
    ok_content = {"details": {
        "body": "<p>OK body</p>",
        "metadata": {"aircraft_type": "Boeing 737", "registration": "G-OKRG",
                     "location": "Manchester", "date_of_occurrence": "2024-05-01",
                     "report_type": "field-investigation", "aircraft_category": ["commercial"]},
        "attachments": [
            {"content_type": "application/pdf", "title": "Main Report", "url": "https://gov.uk/ok.pdf"},
        ],
    }}
    routes = {
        f"{govuk.CONTENT_URL}/aaib-reports/err": FakeResp(status_code=500),
        f"{govuk.CONTENT_URL}/aaib-reports/ok": FakeResp(json_data=ok_content),
        "https://gov.uk/ok.pdf": FakeResp(content=b"%PDF"),
    }
    client = make_client(routes)
    # Must not raise despite the 500 on 'err'
    result = pipeline.fetch(conn, client, str(tmp_path))
    assert result == 2  # iterated over both rows
    err_row = conn.execute("SELECT status FROM aaib_reports WHERE slug='err'").fetchone()
    ok_row = conn.execute("SELECT status FROM aaib_reports WHERE slug='ok'").fetchone()
    assert err_row["status"] == "new", "failed report must remain STATUS_NEW for retry"
    assert ok_row["status"] == "fetched", "successful report must be fetched despite earlier error"


def test_fetch_accepts_scalar_aircraft_category(make_client, tmp_path):
    """aircraft_category as a plain string (not a list) must be stored as-is."""
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('scalar','new',1)")
    content = {"details": {
        "body": "<p>Scalar category</p>",
        "metadata": {"aircraft_type": "Piper PA-28", "registration": "G-SCAL",
                     "location": "Bristol", "date_of_occurrence": "2023-03-10",
                     "report_type": "field-investigation",
                     "aircraft_category": "general-aviation"},
        "attachments": [],
    }}
    routes = {
        f"{govuk.CONTENT_URL}/aaib-reports/scalar": FakeResp(json_data=content),
    }
    client = make_client(routes)
    assert pipeline.fetch(conn, client, str(tmp_path)) == 1
    row = conn.execute("SELECT aircraft_category FROM aaib_reports WHERE slug='scalar'").fetchone()
    assert row["aircraft_category"] == "general-aviation"


def test_fetch_keeps_body_when_pdf_download_fails(make_client, tmp_path):
    conn = _conn()
    conn.execute("INSERT INTO aaib_reports (slug, status, discovered_at) VALUES ('p','new',1)")
    content = {"details": {
        "body": "<p>Body survives</p>",
        "metadata": {"aircraft_type": "Cessna 172", "registration": "G-XXXX"},
        "attachments": [{"content_type": "application/pdf", "title": "Main", "url": "https://gov.uk/broken.pdf"}],
    }}
    routes = {
        f"{govuk.CONTENT_URL}/aaib-reports/p": FakeResp(json_data=content),
        "https://gov.uk/broken.pdf": FakeResp(status_code=500),
    }
    client = make_client(routes)
    assert pipeline.fetch(conn, client, str(tmp_path)) == 1
    row = conn.execute("SELECT * FROM aaib_reports WHERE slug='p'").fetchone()
    assert row["status"] == "fetched"
    assert row["pdf_path"] is None
    assert row["body_text"] == "Body survives"
    assert row["registration"] == "G-XXXX"
