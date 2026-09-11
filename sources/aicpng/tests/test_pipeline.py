"""Pipeline tests with a fake client + saved fixtures (offline)."""
import os
import pytest

from aicpng_ingest import db, pipeline, aicpng

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class _Resp:
    def __init__(self, text="", content=b"", status=200):
        self.text = text
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Routes listing pages + detail page + a fake PDF download."""
    def __init__(self):
        self.page0 = _fx("aicpng_listing_page0.html")
        self.detail = _fx("aicpng_detail_final.html")

    def get(self, url, params=None, headers=None):
        if url == aicpng.LISTING_URL:
            page = (params or {}).get("page", 0)
            return _Resp(text=self.page0 if page == 0 else "")
        if "/investigation/" in url:
            return _Resp(text=self.detail)
        if url.lower().endswith(".pdf"):
            return _Resp(content=b"%PDF-1.5 fake")
        return _Resp(status=404)

    def close(self):
        pass


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.init_schema(c)
    return c


def test_discover_inserts_one_page(conn):
    n = pipeline.discover(conn, _Client())
    assert n == 20
    cnt = conn.execute("SELECT COUNT(*) FROM aicpng_reports").fetchone()[0]
    assert cnt == 20


def test_discover_idempotent(conn):
    cl = _Client()
    pipeline.discover(conn, cl)
    n2 = pipeline.discover(conn, cl)
    assert n2 == 0
    assert conn.execute("SELECT COUNT(*) FROM aicpng_reports").fetchone()[0] == 20


def test_discover_metadata_stored(conn):
    pipeline.discover(conn, _Client())
    row = conn.execute(
        "SELECT * FROM aicpng_reports WHERE case_id='AIC-26-1003'").fetchone()
    assert row["registration"] == "H4-HSA"
    assert row["date_of_occurrence"] == "2026-04-17"
    assert row["lang"] == "en"
    assert row["status"] == db.STATUS_NEW
    assert row["report_url"].endswith("/investigation/1096")


def test_fetch_downloads_pdf(conn, tmp_path):
    cl = _Client()
    pipeline.discover(conn, cl)
    n = pipeline.fetch(conn, cl, str(tmp_path / "pdfs"))
    assert n == 20
    row = conn.execute(
        "SELECT pdf_path, pdf_url, status FROM aicpng_reports "
        "WHERE case_id='AIC-26-1003'").fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] and os.path.exists(row["pdf_path"])
    assert row["pdf_url"].endswith(".pdf")


def test_parse_and_build_flow(conn, tmp_path, monkeypatch):
    cl = _Client()
    pipeline.discover(conn, cl)
    pipeline.fetch(conn, cl, str(tmp_path / "pdfs"))
    # stub pdftotext extraction with a long narrative
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "X" * 2000)
    pipeline.parse(conn)
    tier = conn.execute(
        "SELECT source_tier FROM aicpng_reports WHERE case_id='AIC-26-1003'"
    ).fetchone()[0]
    assert tier == "pdf"
    built = pipeline.build(conn)
    assert built == 20
    acc = conn.execute(
        "SELECT country, site_slug, source_url FROM aicpng_accidents "
        "WHERE case_id='AIC-26-1003'").fetchone()
    assert acc["country"] == "PG"
    assert acc["site_slug"] == "aic-26-1003"
    assert acc["source_url"].endswith(".pdf")


def test_build_skips_scanned(conn, tmp_path, monkeypatch):
    cl = _Client()
    pipeline.discover(conn, cl)
    pipeline.fetch(conn, cl, str(tmp_path / "pdfs"))
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "tiny")  # < ceil
    pipeline.parse(conn)
    tier = conn.execute(
        "SELECT source_tier FROM aicpng_reports WHERE case_id='AIC-26-1003'"
    ).fetchone()[0]
    assert tier == "scanned"
    built = pipeline.build(conn)
    assert built == 0
    assert conn.execute("SELECT COUNT(*) FROM aicpng_accidents").fetchone()[0] == 0
