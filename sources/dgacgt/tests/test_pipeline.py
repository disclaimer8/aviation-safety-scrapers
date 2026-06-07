from pathlib import Path

from dgacgt_ingest import db, dgacgt, pipeline
from dgacgt_ingest.pdf import MIN_NARRATIVE, SCANNED_THRESHOLD


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_discover_inserts_from_autoindex(read_fixture, make_client):
    index = read_fixture("dgacgt_index.html")
    y2024 = read_fixture("dgacgt_year_2024.html")
    y2008 = read_fixture("dgacgt_year_2008.html")
    y2006 = read_fixture("dgacgt_year_2006.html")

    # Route the index + the 3 fixture years; all other years return empty page.
    routes = {dgacgt.INDEX_URL: type("R", (), {"text": index, "raise_for_status": lambda s: None})()}

    def make_resp(txt):
        return type("R", (), {"text": txt, "raise_for_status": lambda s: None})()

    # Map every discovered year URL; fixtures for 3, empty for the rest.
    for url in dgacgt.iter_year_urls(index):
        if url.endswith("/2024/"):
            routes[url] = make_resp(y2024)
        elif url.endswith("/2008/"):
            routes[url] = make_resp(y2008)
        elif url.endswith("/2006/"):
            routes[url] = make_resp(y2006)
        else:
            routes[url] = make_resp("<html></html>")

    client = make_client(routes)
    conn = _conn()
    n = pipeline.discover(conn, client)
    assert n == 5 + 20 + 11  # 2024 + 2008 + 2006

    # spot-check a known case_id landed with reg + date
    row = conn.execute(
        "SELECT registration, date_of_occurrence, year, pdf_url "
        "FROM dgacgt_reports WHERE case_id='TG-MIC-2024-07-31'"
    ).fetchone()
    assert row["registration"] == "TG-MIC"
    assert row["date_of_occurrence"] == "2024-07-31"
    assert row["year"] == 2024
    assert row["pdf_url"].endswith(".pdf")


def test_discover_idempotent(read_fixture, make_client):
    index = read_fixture("dgacgt_index.html")
    y2024 = read_fixture("dgacgt_year_2024.html")

    def make_resp(txt):
        return type("R", (), {"text": txt, "raise_for_status": lambda s: None})()

    routes = {dgacgt.INDEX_URL: make_resp(index)}
    for url in dgacgt.iter_year_urls(index):
        routes[url] = make_resp(y2024 if url.endswith("/2024/") else "<html></html>")

    conn = _conn()
    first = pipeline.discover(conn, make_client(routes))
    second = pipeline.discover(conn, make_client(routes))
    assert first == 5
    assert second == 0  # nothing new on re-run


def _seed_fetched(conn, case_id, pdf_path, registration="TG-AAA", date="2024-01-01"):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO dgacgt_reports (case_id, pdf_url, pdf_path, registration, "
        "date_of_occurrence, year, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (case_id, "http://x/" + case_id + ".pdf", pdf_path, registration, date,
         2024, db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()


def test_parse_tiers(tmp_path, monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "A-LONG", "/x/long.pdf")
    _seed_fetched(conn, "B-SHORT", "/x/short.pdf")
    _seed_fetched(conn, "C-SCAN", "/x/scan.pdf")
    _seed_fetched(conn, "D-NONE", None)

    texts = {
        "/x/long.pdf": "L" * (MIN_NARRATIVE + 50),
        "/x/short.pdf": "S" * (SCANNED_THRESHOLD + 10),
        "/x/scan.pdf": "tiny",
    }
    monkeypatch.setattr(pipeline, "extract_text", lambda p: texts.get(p, ""))

    pipeline.parse(conn)
    tiers = dict(conn.execute(
        "SELECT case_id, source_tier FROM dgacgt_reports"
    ).fetchall())
    assert tiers["A-LONG"] == "pdf"
    assert tiers["B-SHORT"] == "short"
    assert tiers["C-SCAN"] == "scanned"
    assert tiers["D-NONE"] == "none"


def test_build_skips_scanned_and_emits_accident(monkeypatch):
    conn = _conn()
    _seed_fetched(conn, "TG-MIC-2024-07-31", "/x/long.pdf", "TG-MIC", "2024-07-31")
    _seed_fetched(conn, "TG-SCN-2024-02-02", "/x/scan.pdf", "TG-SCN", "2024-02-02")

    texts = {"/x/long.pdf": "Narrativa " * 200, "/x/scan.pdf": "img"}
    monkeypatch.setattr(pipeline, "extract_text", lambda p: texts.get(p, ""))

    pipeline.parse(conn)
    built = pipeline.build(conn)
    assert built == 1

    acc = conn.execute(
        "SELECT * FROM dgacgt_accidents WHERE case_id='TG-MIC-2024-07-31'"
    ).fetchone()
    assert acc["country"] == "GT"
    assert acc["registration"] == "TG-MIC"
    assert acc["event_date"] == "2024-07-31"
    assert acc["site_slug"] == "tg-mic-2024-07-31"
    assert acc["source_url"].endswith(".pdf")

    # scanned one is skipped, no accident row
    assert conn.execute(
        "SELECT 1 FROM dgacgt_accidents WHERE case_id='TG-SCN-2024-02-02'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT status FROM dgacgt_reports WHERE case_id='TG-SCN-2024-02-02'"
    ).fetchone()["status"] == db.STATUS_SKIPPED


def test_fetch_download_failure_keeps_new(tmp_path, monkeypatch):
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO dgacgt_reports (case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("X-1", "http://x/fail.pdf", db.STATUS_NEW, ts, ts),
    )
    conn.commit()

    def boom(client, url, dest):
        raise RuntimeError("boom")
    monkeypatch.setattr(dgacgt, "download", boom)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    pipeline.fetch(conn, object(), str(tmp_path))
    assert conn.execute(
        "SELECT status FROM dgacgt_reports WHERE case_id='X-1'"
    ).fetchone()["status"] == db.STATUS_NEW
