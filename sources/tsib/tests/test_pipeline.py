"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from tsib_ingest import db, pipeline, tsib

_PDF_A = "https://isomer-user-content.by.gov.sg/287/aaaaaaaa-0000-0000-0000-000000000001/a.pdf"
_PDF_B = "https://isomer-user-content.by.gov.sg/287/bbbbbbbb-0000-0000-0000-000000000002/b.pdf"


def _anchor(label, href):
    return (f'<a target="_blank" aria-label="{label}" href="{href}">x</a>')


# Two distinct anchors → one page worth of two reports.
_PAGE_TWO_ROWS = (
    _anchor("19 May 2025 Status Past Reports Boeing B737-800 (9M-MLL) "
            "Incident (opens in new tab)", _PDF_A)
    + _anchor("6 September 2024 Status Past Reports Boeing B787-9 (9V-OJD) "
              "Accident (opens in new tab)", _PDF_B)
)


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    """`pages` maps page-number -> listing HTML; `pdfs` maps url -> bytes."""
    def __init__(self, pages=None, pdfs=None):
        self.pages = pages if pages is not None else {1: _PAGE_TWO_ROWS}
        self.pdfs = pdfs or {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append((url, params))
        if url == tsib.LISTING_URL:
            page = (params or {}).get("page", 1)
            # Default: clamp behaviour — every page returns page-1 content.
            html = self.pages.get(page, self.pages.get(1, ""))
            return FakeResp(text=html)
        # PDF download (url is percent-encoded; match on the raw too)
        return FakeResp(content=self.pdfs.get(url, b""))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(tsib, "DELAY", 0)


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2
    rows = {r["pdf_url"]: r for r in conn.execute("SELECT * FROM tsib_reports")}
    assert set(rows) == {_PDF_A, _PDF_B}
    # case_id NULL until fetch; metadata from the listing present
    assert rows[_PDF_A]["case_id"] is None
    assert rows[_PDF_A]["status"] == db.STATUS_NEW
    assert rows[_PDF_A]["date_of_occurrence"] == "2025-05-19"
    assert rows[_PDF_A]["report_kind"] == "Incident"
    assert rows[_PDF_A]["registration"] == "9M-MLL"
    assert rows[_PDF_B]["report_kind"] == "Accident"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_discover_clamp_stop_repeated_page(conn):
    # Every page returns the SAME content (the real site's ?page=N no-op).
    # discover must walk page 1, then stop at page 2 (first PDF repeats).
    client = FakeClient(pages={1: _PAGE_TWO_ROWS})  # any page -> page-1 html
    n = pipeline.discover(conn, client, max_pages=30)
    assert n == 2
    listing_calls = [p for (u, p) in client.requested if u == tsib.LISTING_URL]
    # page 1 (inserts) + page 2 (detects clamp, stops) = 2 listing fetches
    assert listing_calls == [{"page": 1}, {"page": 2}]


def test_discover_walks_real_second_page(conn):
    # If the server ever truly paginates, distinct pages are all ingested.
    p2 = _anchor("21 May 2024 Status Past Reports Boeing B777-300ER Accident "
                 "(opens in new tab)",
                 "https://isomer-user-content.by.gov.sg/287/"
                 "cccccccc-0000-0000-0000-000000000003/c.pdf")
    client = FakeClient(pages={1: _PAGE_TWO_ROWS, 2: p2, 3: ""})
    n = pipeline.discover(conn, client, max_pages=30)
    assert n == 3


# ── fetch ────────────────────────────────────────────────────────────────────

def _discover_two(conn):
    pipeline.discover(conn, FakeClient())


def test_fetch_pdf_tier_and_case_id(conn, tmp_path, monkeypatch):
    _discover_two(conn)
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: "TIB/AAI/CAS.246 " + ("N" * 9000))
    client = FakeClient(pdfs={tsib.percent_encode(_PDF_A): b"%PDF a",
                              tsib.percent_encode(_PDF_B): b"%PDF b"})
    assert pipeline.fetch(conn, client, pdf_dir=str(tmp_path)) == 2
    rows = {r["pdf_url"]: r for r in conn.execute("SELECT * FROM tsib_reports")}
    assert rows[_PDF_A]["source_tier"] == "pdf"
    assert len(rows[_PDF_A]["narrative_text"]) > 9000
    # case_id resolved from PDF text; second one collides → suffix
    cids = {rows[_PDF_A]["case_id"], rows[_PDF_B]["case_id"]}
    assert "tib-aai-cas-246" in cids
    assert "tib-aai-cas-246-2" in cids
    assert rows[_PDF_A]["status"] == db.STATUS_PARSED


def test_fetch_scanned_tier_uuid_fallback(conn, tmp_path, monkeypatch):
    _discover_two(conn)
    # empty text layer → scanned tier, case_id from the URL UUID
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    client = FakeClient(pdfs={tsib.percent_encode(_PDF_A): b"%PDF scan",
                              tsib.percent_encode(_PDF_B): b"%PDF scan"})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute("SELECT * FROM tsib_reports WHERE pdf_url=?",
                       (_PDF_A,)).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["case_id"] == "tsib-aaaaaaaa-0000-0000-0000-000000000001"
    assert row["status"] == db.STATUS_PARSED


def test_fetch_download_failure_stays_new(conn, tmp_path, monkeypatch):
    _discover_two(conn)

    def boom(client, url, dest):
        raise RuntimeError("network down")

    monkeypatch.setattr(pipeline.tsib, "download_pdf", boom)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    statuses = {r["status"] for r in
                conn.execute("SELECT status FROM tsib_reports")}
    assert statuses == {db.STATUS_NEW}  # unchanged → retried next cycle


# ── build ────────────────────────────────────────────────────────────────────

def _fetch_two_pdf(conn, tmp_path, monkeypatch, text):
    _discover_two(conn)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    client = FakeClient(pdfs={tsib.percent_encode(_PDF_A): b"%PDF a",
                              tsib.percent_encode(_PDF_B): b"%PDF b"})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))


def test_build(conn, tmp_path, monkeypatch):
    _fetch_two_pdf(conn, tmp_path, monkeypatch, "TIB/AAI/CAS.246 " + "N" * 9000)
    assert pipeline.build(conn) == 2
    acc = {r["source_url"]: r for r in
           conn.execute("SELECT * FROM tsib_accidents")}
    assert set(acc) == {_PDF_A, _PDF_B}
    assert acc[_PDF_A]["country"] == "SG"
    assert acc[_PDF_A]["event_date"] == "2025-05-19"
    assert acc[_PDF_A]["report_type"] == "Incident"


def test_build_floor_skips_short(conn, tmp_path, monkeypatch):
    _fetch_two_pdf(conn, tmp_path, monkeypatch, "short")  # < 300 chars
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM tsib_accidents").fetchone()["c"] == 0
    statuses = {r["status"] for r in
                conn.execute("SELECT status FROM tsib_reports")}
    assert statuses == {db.STATUS_SKIPPED}


def test_build_idempotent(conn, tmp_path, monkeypatch):
    _fetch_two_pdf(conn, tmp_path, monkeypatch, "TIB/AAI/CAS.246 " + "N" * 9000)
    pipeline.build(conn)
    conn.execute("UPDATE tsib_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)  # INSERT OR REPLACE → no duplicate rows
    assert conn.execute(
        "SELECT COUNT(*) c FROM tsib_accidents").fetchone()["c"] == 2
