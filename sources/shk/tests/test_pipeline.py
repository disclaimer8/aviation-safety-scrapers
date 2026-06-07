"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from shk_ingest import db, pipeline, shk


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


_U1 = "https://shk.se/x/search-investigation/aviation/2023-11-22-accident-se-aaa"
_U2 = "https://shk.se/x/search-investigation/aviation/2023-11-23-ongoing-se-bbb"

_SITEMAP = (f'<?xml version="1.0"?><urlset><loc>{_U1}</loc>'
            f'<loc>{_U2}</loc></urlset>')

_DONE = ('<h1>Accident with SE-AAA at X</h1>'
         '<div class="investigation-information"><p>Date of occurrence:</p>'
         '<time datetime="2010-05-31T22:00:00.000Z">1 June 2010</time>'
         '<p>L-10/10</p></div>'
         '<p>RL 2011:05</p>'
         '<a href="/download/a/1/rl2011_05e.pdf">Final report English</a>')
_ONGOING = '<h1>Ongoing with SE-BBB</h1>'


class FakeClient:
    def __init__(self, urls=None):
        self.urls = urls if urls is not None else {
            _U1: _DONE, _U2: _ONGOING}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == shk.SITEMAP_URL:
            return FakeResp(content=_SITEMAP.encode())
        if url in self.urls:
            return FakeResp(text=self.urls[url])
        if url.endswith(".pdf"):
            return FakeResp(content=b"%PDF x")
        raise RuntimeError("404")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(shk, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM shk_reports"))
    assert ids == ["accident-se-aaa", "ongoing-se-bbb"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_completed_and_ongoing(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 7000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM shk_reports")}
    done = rows["accident-se-aaa"]
    assert done["status"] == "parsed"
    assert done["lang"] == "en"
    assert done["rl_number"] == "RL 2011:05"
    assert done["date_of_occurrence"] == "2010-06-01"  # display text
    assert done["registration"] == "SE-AAA"
    # ongoing: metadata stored, stays 'new' (self-heal next cycle)
    ongoing = rows["ongoing-se-bbb"]
    assert ongoing["status"] == "new"
    assert ongoing["registration"] == "SE-BBB"


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 7000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM shk_accidents").fetchone()
    assert acc["case_id"] == "accident-se-aaa"
    assert acc["country"] == "SE"
    assert acc["event_date"] == "2010-06-01"
    assert acc["source_url"] == _U1


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient())

    class Boom(FakeClient):
        def get(self, url, params=None):
            if url == shk.SITEMAP_URL:
                return super().get(url, params)
            raise RuntimeError("boom")

    pipeline.fetch(conn, Boom(), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM shk_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 7000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE shk_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM shk_accidents").fetchone()["c"] == 1
