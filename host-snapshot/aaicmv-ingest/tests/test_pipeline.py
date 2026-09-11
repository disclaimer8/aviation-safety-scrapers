# tests/test_pipeline.py
import os
import pytest

from aaicmv_ingest import db, pipeline, aaicmv
from tests._fx import load


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_discover_inserts(conn, make_client):
    html = load("aaicmv_index.html").encode("utf-8")
    from tests.conftest import FakeResp
    client = make_client({aaicmv.INDEX_URL: FakeResp(content=html)})
    n = pipeline.discover(conn, client)
    assert n == 45
    # idempotent: a second run inserts nothing
    assert pipeline.discover(conn, client) == 0
    row = conn.execute(
        "SELECT lang, country FROM aaicmv_reports "
        "LEFT JOIN aaicmv_accidents USING(case_id) WHERE case_id='MV-2024-03-final'"
    ).fetchone()
    assert row["lang"] == "en"


def test_build_emits_accident(conn):
    # seed one parsed row with a long English narrative
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicmv_reports (case_id, pdf_url, title, event_class, "
        "aircraft, registration, date_of_occurrence, narrative_text, source_tier, "
        "status, discovered_at, updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("MV-2024-03-final", "https://caa.gov.mv/attachments/x.pdf",
         "Final report", "Accident", "DHC-6-300", "8Q-TBB", "2024-10-13",
         "X" * 900, "pdf", db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    built = pipeline.build(conn)
    assert built == 1
    acc = conn.execute(
        "SELECT * FROM aaicmv_accidents WHERE case_id='MV-2024-03-final'"
    ).fetchone()
    assert acc["country"] == "MV"
    assert acc["registration"] == "8Q-TBB"
    assert acc["site_slug"].startswith("crash-")


def test_build_skips_scanned(conn):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicmv_reports (case_id, narrative_text, source_tier, "
        "status, discovered_at, updated_at) VALUES (?,?,?,?,?,?)",
        ("MV-2001-01-report", "tiny", "scanned", db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    assert pipeline.build(conn) == 0
    r = conn.execute(
        "SELECT status FROM aaicmv_reports WHERE case_id='MV-2001-01-report'"
    ).fetchone()
    assert r["status"] == db.STATUS_SKIPPED


def test_parse_scanned_tier(conn, tmp_path):
    # a PDF path that pdftotext yields ~nothing for -> 'scanned'
    fake_pdf = tmp_path / "scan.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 not really text")
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicmv_reports (case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("MV-2004-01-report", str(fake_pdf), db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()
    pipeline.parse(conn)
    r = conn.execute(
        "SELECT source_tier FROM aaicmv_reports WHERE case_id='MV-2004-01-report'"
    ).fetchone()
    assert r["source_tier"] in ("scanned", "none")
