import pathlib

import pytest

from aet_ingest import db, pipeline, aet

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
    """Serves the index page; download() is monkeypatched in tests."""
    def __init__(self, index_html):
        self._index = index_html

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
    return (FIXTURES / "aet_index.html").read_text(encoding="utf-8", errors="replace")


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_kept(conn, index_html):
    n = pipeline.discover(conn, FakeClient(index_html))
    assert n == 35
    rows = conn.execute(
        "SELECT case_id, lang, event_class, status FROM aet_reports"
    ).fetchall()
    assert len(rows) == 35
    assert all(r["status"] == db.STATUS_NEW for r in rows)
    # per-document languages -> more than one value present
    assert len({r["lang"] for r in rows}) >= 2
    # foreign-authority docs flagged via event_class
    foreign = [r for r in rows if r["event_class"] == "Foreign-authority report"]
    assert len(foreign) == 19


def test_discover_idempotent(conn, index_html):
    pipeline.discover(conn, FakeClient(index_html))
    second = pipeline.discover(conn, FakeClient(index_html))
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM aet_reports").fetchone()[0] == 35


# ── fetch (monkeypatched download + no sleep) ─────────────────────────────────

def test_fetch_downloads_and_advances(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    written = []

    def fake_download(client, url, dest):
        pathlib.Path(dest).write_bytes(b"%PDF stub")
        written.append((url, dest))
    monkeypatch.setattr(aet, "download", fake_download)

    n = pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    assert n == 35
    assert len(written) == 35
    fetched = conn.execute(
        "SELECT COUNT(*) FROM aet_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchone()[0]
    assert fetched == 35


def test_fetch_download_failure_stays_new(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    monkeypatch.setattr(aet, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    still_new = conn.execute(
        "SELECT COUNT(*) FROM aet_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchone()[0]
    assert still_new == 35


# ── parse (scanned/empty-aware) ───────────────────────────────────────────────

def _seed_fetched(conn, case_id, pdf_path, pdf_url, lang="fr"):
    conn.execute(
        "INSERT INTO aet_reports (case_id, pdf_path, pdf_url, status, lang) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_path, pdf_url, db.STATUS_FETCHED, lang),
    )
    conn.commit()


def test_parse_tiers(conn, tmp_path, monkeypatch):
    long_txt = "Aircraft LX-VCF accident. " + "x" * 800 + " end"
    short_txt = "y" * 550
    scanned_txt = "z" * 100
    texts = {"p_long": long_txt, "p_short": short_txt, "p_scan": scanned_txt, "p_none": ""}

    for cid in texts:
        _seed_fetched(conn, cid, f"/tmp/{cid}.pdf", f"//aet/x/{cid}.pdf")
    monkeypatch.setattr(pipeline, "extract_text",
                        lambda p: texts[pathlib.Path(p).stem])

    pipeline.parse(conn)
    rows = {r["case_id"]: r for r in conn.execute(
        "SELECT case_id, source_tier, registration FROM aet_reports"
    ).fetchall()}
    assert rows["p_long"]["source_tier"] == "pdf"
    assert rows["p_short"]["source_tier"] == "short"
    assert rows["p_scan"]["source_tier"] == "scanned"
    assert rows["p_none"]["source_tier"] == "none"
    assert rows["p_long"]["registration"] == "LX-VCF"


def test_parse_registration_from_filename_fallback(conn, monkeypatch):
    # no registration in text -> harvested from the filename
    _seed_fetched(conn, "p_fn", "/tmp/p_fn.pdf",
                  "//aet/x/bombardier-global-6000-LX-NST-04-23.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "x" * 900)
    pipeline.parse(conn)
    reg = conn.execute(
        "SELECT registration FROM aet_reports WHERE case_id='p_fn'"
    ).fetchone()["registration"]
    assert reg == "LX-NST"


# ── build (empty/scanned skipped; foreign flagged) ────────────────────────────

def test_build_emits_skips_and_flags_foreign(conn):
    conn.execute(
        "INSERT INTO aet_reports (case_id, status, narrative_text, registration, "
        "location, event_class, pdf_url) VALUES (?,?,?,?,?,?,?)",
        ("good", db.STATUS_PARSED, "n" * 900, "LX-VCF", "Prestwick", "Accident",
         "https://aet.gouvernement.lu/dam-assets/x.pdf"),
    )
    conn.execute(
        "INSERT INTO aet_reports (case_id, status, narrative_text, registration, "
        "location, event_class, pdf_url) VALUES (?,?,?,?,?,?,?)",
        ("foreign", db.STATUS_PARSED, "m" * 900, "OO-PMI", "Nancy",
         "Foreign-authority report", "https://aet.gouvernement.lu/dam-assets/f.pdf"),
    )
    conn.execute(
        "INSERT INTO aet_reports (case_id, status, narrative_text, event_class) "
        "VALUES (?,?,?,?)",
        ("empty", db.STATUS_PARSED, "z" * 40, "Accident"),
    )
    conn.commit()

    built = pipeline.build(conn)
    assert built == 2

    acc = {r["case_id"]: r for r in conn.execute(
        "SELECT * FROM aet_accidents"
    ).fetchall()}
    assert set(acc) == {"good", "foreign"}
    assert all(r["country"] == "LU" for r in acc.values())
    assert acc["good"]["report_type"] == "Accident"
    assert acc["foreign"]["report_type"] == aet.FOREIGN_AUTHORITY
    assert acc["good"]["site_slug"].startswith("crash-")

    statuses = {r["case_id"]: r["status"] for r in conn.execute(
        "SELECT case_id, status FROM aet_reports").fetchall()}
    assert statuses["good"] == db.STATUS_BUILT
    assert statuses["foreign"] == db.STATUS_BUILT
    assert statuses["empty"] == db.STATUS_SKIPPED
