import pathlib

import pytest

from gcaagy_ingest import db, pipeline, gcaagy

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
    return (FIXTURES / "gcaagy_index.html").read_text(encoding="utf-8", errors="replace")


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_all(conn, index_html):
    n = pipeline.discover(conn, FakeClient(index_html))
    assert n == 29
    rows = conn.execute("SELECT case_id, lang, status FROM gcaagy_reports").fetchall()
    assert len(rows) == 29
    assert all(r["lang"] == "en" for r in rows)
    assert all(r["status"] == db.STATUS_NEW for r in rows)


def test_discover_idempotent(conn, index_html):
    pipeline.discover(conn, FakeClient(index_html))
    second = pipeline.discover(conn, FakeClient(index_html))
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM gcaagy_reports").fetchone()[0] == 29


# ── fetch (monkeypatched download + no sleep) ─────────────────────────────────

def test_fetch_downloads_and_advances(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    written = []
    def fake_download(client, url, dest):
        pathlib.Path(dest).write_bytes(b"%PDF stub")
        written.append((url, dest))
    monkeypatch.setattr(gcaagy, "download", fake_download)

    n = pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    assert n == 29
    assert len(written) == 29
    fetched = conn.execute(
        "SELECT COUNT(*) FROM gcaagy_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchone()[0]
    assert fetched == 29
    # spaced-href row downloaded too
    assert any("8R-GTR" in u for u, _ in written)


def test_fetch_download_failure_stays_new(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gcaagy, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    still_new = conn.execute(
        "SELECT COUNT(*) FROM gcaagy_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchone()[0]
    assert still_new == 29  # none advanced


# ── parse (scanned-aware) ─────────────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path):
    conn.execute(
        "INSERT INTO gcaagy_reports (case_id, pdf_path, status, lang) VALUES (?,?,?,?)",
        (case_id, pdf_path, db.STATUS_FETCHED, "en"),
    )
    conn.commit()


def test_parse_tiers(conn, tmp_path, monkeypatch):
    long_txt = "Aircraft 8R-GRE accident. " + "x" * 800 + " AAIIU: 3/1/33 end"
    short_txt = "y" * 550
    scanned_txt = "z" * 100
    texts = {"p_long": long_txt, "p_short": short_txt, "p_scan": scanned_txt, "p_none": ""}

    for cid in texts:
        _seed_fetched(conn, cid, f"/tmp/{cid}.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: texts[pathlib.Path(p).stem])

    pipeline.parse(conn)
    rows = {r["case_id"]: r for r in conn.execute(
        "SELECT case_id, source_tier, registration, report_url FROM gcaagy_reports"
    ).fetchall()}
    assert rows["p_long"]["source_tier"] == "pdf"
    assert rows["p_short"]["source_tier"] == "short"
    assert rows["p_scan"]["source_tier"] == "scanned"
    assert rows["p_none"]["source_tier"] == "none"
    # AAIIU ref + registration harvested from long text
    assert rows["p_long"]["report_url"] == "aaiiu-3-1-33"
    assert rows["p_long"]["registration"] == "8R-GRE"


# ── build (scanned/short skipped) ─────────────────────────────────────────────

def test_build_emits_and_skips(conn, monkeypatch):
    # one good (pdf), one scanned -> skipped
    conn.execute(
        "INSERT INTO gcaagy_reports (case_id, status, narrative_text, registration, "
        "location, event_class, pdf_url, report_url) VALUES (?,?,?,?,?,?,?,?)",
        ("good", db.STATUS_PARSED, "n" * 900, "8R-GRE", "Ogle", "Accident",
         "https://www.gcaa-gy.org/pdf/x.pdf", "aaiiu-3-1-33"),
    )
    conn.execute(
        "INSERT INTO gcaagy_reports (case_id, status, narrative_text, event_class) "
        "VALUES (?,?,?,?)",
        ("scan", db.STATUS_PARSED, "z" * 40, "Accident"),
    )
    conn.commit()

    built = pipeline.build(conn)
    assert built == 1

    acc = conn.execute("SELECT * FROM gcaagy_accidents").fetchall()
    assert len(acc) == 1
    row = acc[0]
    assert row["case_id"] == "good"
    assert row["country"] == "GY"
    assert row["report_type"] == "aaiiu-3-1-33"   # AAIIU ref carried
    assert row["site_slug"].startswith("crash-")
    assert row["source_url"].endswith(".pdf")

    statuses = {r["case_id"]: r["status"] for r in conn.execute(
        "SELECT case_id, status FROM gcaagy_reports").fetchall()}
    assert statuses["good"] == db.STATUS_BUILT
    assert statuses["scan"] == db.STATUS_SKIPPED
