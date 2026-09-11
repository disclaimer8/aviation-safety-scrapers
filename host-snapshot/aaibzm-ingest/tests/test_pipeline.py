import pathlib

import pytest

from aaibzm_ingest import db, pipeline, aaibzm

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()
    @property
    def text(self):
        return self.content.decode("utf-8", "replace")
    def raise_for_status(self):
        pass


class FakeClient:
    """Serves the index page; download() writes a stub PDF file."""
    def __init__(self, index_html, pdf_bytes=b"%PDF-1.4 stub"):
        self._index = index_html
        self._pdf_bytes = pdf_bytes
        self.downloads = []
    def get(self, url, headers=None):
        return FakeResp(self._index)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def index_html():
    return (FIXTURES / "aaibzm_index.html").read_text(encoding="utf-8", errors="replace")


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_all(conn, index_html):
    n = pipeline.discover(conn, FakeClient(index_html))
    assert n == 7
    rows = conn.execute(
        "SELECT case_id, lang, status, registration, date_of_occurrence, "
        "event_class FROM aaibzm_reports"
    ).fetchall()
    assert len(rows) == 7
    assert all(r["lang"] == "en" for r in rows)
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    # card fields stored at discover time
    assert all(r["registration"] for r in rows)
    assert all(r["date_of_occurrence"] for r in rows)
    classes = {r["event_class"] for r in rows}
    assert "Accident" in classes
    assert "Serious Incident" in classes


def test_discover_idempotent(conn, index_html):
    pipeline.discover(conn, FakeClient(index_html))
    second = pipeline.discover(conn, FakeClient(index_html))
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM aaibzm_reports").fetchone()[0] == 7


# ── fetch (monkeypatched download + no sleep) ─────────────────────────────────

def test_fetch_downloads_and_advances(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    written = []
    def fake_download(client, url, dest, detail_url=None):
        pathlib.Path(dest).write_bytes(b"%PDF stub")
        written.append((url, dest, detail_url))
    monkeypatch.setattr(aaibzm, "download", fake_download)

    n = pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    assert n == 7
    assert len(written) == 7
    fetched = conn.execute(
        "SELECT COUNT(*) FROM aaibzm_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchone()[0]
    assert fetched == 7
    # detail_url passed through for fallback resolution
    assert all(d and d.endswith(".php") for _, _, d in written)


def test_fetch_download_failure_stays_new(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    monkeypatch.setattr(aaibzm, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    still_new = conn.execute(
        "SELECT COUNT(*) FROM aaibzm_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchone()[0]
    assert still_new == 7  # none advanced


# ── parse (scanned-aware) ─────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path):
    conn.execute(
        "INSERT INTO aaibzm_reports (case_id, pdf_path, status, lang) VALUES (?,?,?,?)",
        (case_id, pdf_path, db.STATUS_FETCHED, "en"),
    )
    conn.commit()


def test_parse_tiers(conn, tmp_path, monkeypatch):
    long_txt = "Aircraft 9J-YVT accident. " + "x" * 800 + " on 10th January 2020 end"
    short_txt = "y" * 550
    scanned_txt = "z" * 100
    texts = {"p_long": long_txt, "p_short": short_txt, "p_scan": scanned_txt, "p_none": ""}

    for cid in texts:
        _seed_fetched(conn, cid, f"/tmp/{cid}.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: texts[pathlib.Path(p).stem])

    pipeline.parse(conn)
    rows = {r["case_id"]: r for r in conn.execute(
        "SELECT case_id, source_tier, registration, date_of_occurrence FROM aaibzm_reports"
    ).fetchall()}
    assert rows["p_long"]["source_tier"] == "pdf"
    assert rows["p_short"]["source_tier"] == "short"
    assert rows["p_scan"]["source_tier"] == "scanned"
    assert rows["p_none"]["source_tier"] == "none"
    # registration + date back-filled from long text (card left them empty here)
    assert rows["p_long"]["registration"] == "9J-YVT"
    assert rows["p_long"]["date_of_occurrence"] == "2020-01-10"


# ── build (scanned/short skipped) ─────────────────────────────────────────────

def test_build_emits_and_skips(conn):
    conn.execute(
        "INSERT INTO aaibzm_reports (case_id, status, narrative_text, registration, "
        "location, aircraft, event_class, date_of_occurrence, pdf_url) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("aaibzm-9j-yvt", db.STATUS_PARSED, "n" * 900, "9J-YVT", "Maramba",
         "Tanarg Neo Microlight", "Accident", "2020-01-10",
         "https://aaib.org.zm/reports/9J-YVT.pdf"),
    )
    conn.execute(
        "INSERT INTO aaibzm_reports (case_id, status, narrative_text, event_class) "
        "VALUES (?,?,?,?)",
        ("scan", db.STATUS_PARSED, "z" * 40, "Accident"),
    )
    conn.commit()

    built = pipeline.build(conn)
    assert built == 1

    acc = conn.execute("SELECT * FROM aaibzm_accidents").fetchall()
    assert len(acc) == 1
    row = acc[0]
    assert row["case_id"] == "aaibzm-9j-yvt"
    assert row["country"] == "ZM"
    assert row["report_type"] == "Accident"
    assert row["event_date"] == "2020-01-10"
    assert row["site_slug"].startswith("crash-")
    assert row["source_url"].endswith(".pdf")

    statuses = {r["case_id"]: r["status"] for r in conn.execute(
        "SELECT case_id, status FROM aaibzm_reports").fetchall()}
    assert statuses["aaibzm-9j-yvt"] == db.STATUS_BUILT
    assert statuses["scan"] == db.STATUS_SKIPPED
