import pathlib

import pytest

from beacg_ingest import db, pipeline, beacg

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
    return (FIXTURES / "beacg_index.html").read_text(encoding="utf-8", errors="replace")


# -- discover -----------------------------------------------------------------

def test_discover_inserts_all(conn, index_html):
    n = pipeline.discover(conn, FakeClient(index_html))
    assert n == 18
    rows = conn.execute("SELECT case_id, lang, status FROM beacg_reports").fetchall()
    assert len(rows) == 18
    assert all(r["lang"] == "fr" for r in rows)
    assert all(r["status"] == db.STATUS_NEW for r in rows)


def test_discover_carries_listing_fields(conn, index_html):
    pipeline.discover(conn, FakeClient(index_html))
    row = conn.execute(
        "SELECT report_url, date_of_occurrence, aircraft, location, event_class "
        "FROM beacg_reports WHERE case_id=?",
        ("beacg-rapport-final-bea-03-2023-incid-boeing-737-36n-tn-akc-1",),
    ).fetchone()
    assert row["report_url"] == "bea-03-2023"
    assert row["date_of_occurrence"] == "2023-12-17"
    assert row["aircraft"] == "B737-36N"
    assert row["location"] == "Brazzaville"
    assert row["event_class"] == "Incident"


def test_discover_idempotent(conn, index_html):
    pipeline.discover(conn, FakeClient(index_html))
    second = pipeline.discover(conn, FakeClient(index_html))
    assert second == 0
    assert conn.execute("SELECT COUNT(*) FROM beacg_reports").fetchone()[0] == 18


# -- fetch (monkeypatched download + no sleep) --------------------------------

def test_fetch_downloads_and_advances(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)

    written = []
    def fake_download(client, url, dest):
        pathlib.Path(dest).write_bytes(b"%PDF stub")
        written.append((url, dest))
    monkeypatch.setattr(beacg, "download", fake_download)

    n = pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    assert n == 18
    assert len(written) == 18
    fetched = conn.execute(
        "SELECT COUNT(*) FROM beacg_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchone()[0]
    assert fetched == 18


def test_fetch_download_failure_stays_new(conn, index_html, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html))
    monkeypatch.setattr(pipeline.time, "sleep", lambda *_: None)
    monkeypatch.setattr(beacg, "download",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline.fetch(conn, FakeClient(index_html), str(tmp_path))
    still_new = conn.execute(
        "SELECT COUNT(*) FROM beacg_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchone()[0]
    assert still_new == 18  # none advanced


# -- parse (scanned-aware) ----------------------------------------------------

def _seed_fetched(conn, case_id, pdf_path):
    conn.execute(
        "INSERT INTO beacg_reports (case_id, pdf_path, status, lang) VALUES (?,?,?,?)",
        (case_id, pdf_path, db.STATUS_FETCHED, "fr"),
    )
    conn.commit()


def test_parse_tiers(conn, tmp_path, monkeypatch):
    long_txt = (
        "Incident survenu le 17 decembre 2023. immatricule TN-AKC. "
        "Enquete BEA-03-2023. " + "x" * 800
    )
    short_txt = "y" * 550
    scanned_txt = "z" * 100
    texts = {"p_long": long_txt, "p_short": short_txt, "p_scan": scanned_txt, "p_none": ""}

    for cid in texts:
        _seed_fetched(conn, cid, f"/tmp/{cid}.pdf")
    monkeypatch.setattr(pipeline, "extract_text", lambda p: texts[pathlib.Path(p).stem])

    pipeline.parse(conn)
    rows = {r["case_id"]: r for r in conn.execute(
        "SELECT case_id, source_tier, registration, report_url, date_of_occurrence "
        "FROM beacg_reports"
    ).fetchall()}
    assert rows["p_long"]["source_tier"] == "pdf"
    assert rows["p_short"]["source_tier"] == "short"
    assert rows["p_scan"]["source_tier"] == "scanned"
    assert rows["p_none"]["source_tier"] == "none"
    # BEA ref + registration + event date backfilled from long text
    assert rows["p_long"]["report_url"] == "bea-03-2023"
    assert rows["p_long"]["registration"] == "TN-AKC"
    assert rows["p_long"]["date_of_occurrence"] == "2023-12-17"


def test_parse_does_not_overwrite_listing_fields(conn, monkeypatch):
    # listing already supplied date/ref; PDF text must not clobber them.
    conn.execute(
        "INSERT INTO beacg_reports (case_id, pdf_path, status, lang, "
        "report_url, date_of_occurrence, aircraft, location) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("p1", "/tmp/p1.pdf", db.STATUS_FETCHED, "fr",
         "bea-03-2023", "2023-12-17", "B737-36N", "Brazzaville"),
    )
    conn.commit()
    monkeypatch.setattr(
        pipeline, "extract_text",
        lambda p: "survenu le 1 janvier 2099 immatricule TN-ZZZ " + "x" * 700,
    )
    pipeline.parse(conn)
    row = conn.execute(
        "SELECT report_url, date_of_occurrence FROM beacg_reports WHERE case_id='p1'"
    ).fetchone()
    assert row["report_url"] == "bea-03-2023"
    assert row["date_of_occurrence"] == "2023-12-17"


# -- build (scanned/short skipped) --------------------------------------------

def test_build_emits_and_skips(conn):
    # one good (pdf), one scanned -> skipped
    conn.execute(
        "INSERT INTO beacg_reports (case_id, status, narrative_text, registration, "
        "location, event_class, pdf_url, report_url, date_of_occurrence) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        ("good", db.STATUS_PARSED, "n" * 900, "TN-AKC", "Brazzaville", "Incident",
         "https://www.bea.cg/wp-content/uploads/2025/10/x.pdf", "bea-03-2023",
         "2023-12-17"),
    )
    conn.execute(
        "INSERT INTO beacg_reports (case_id, status, narrative_text, event_class) "
        "VALUES (?,?,?,?)",
        ("scan", db.STATUS_PARSED, "z" * 40, "Accident"),
    )
    conn.commit()

    built = pipeline.build(conn)
    assert built == 1

    acc = conn.execute("SELECT * FROM beacg_accidents").fetchall()
    assert len(acc) == 1
    row = acc[0]
    assert row["case_id"] == "good"
    assert row["country"] == "CG"
    assert row["report_type"] == "bea-03-2023"     # BEA ref carried
    assert row["event_date"] == "2023-12-17"
    assert row["site_slug"].startswith("crash-")
    assert row["source_url"].endswith(".pdf")

    statuses = {r["case_id"]: r["status"] for r in conn.execute(
        "SELECT case_id, status FROM beacg_reports").fetchall()}
    assert statuses["good"] == db.STATUS_BUILT
    assert statuses["scan"] == db.STATUS_SKIPPED
