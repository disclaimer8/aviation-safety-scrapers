"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from aibdk_ingest import aibdk, db, pipeline


class FakeResp:
    def __init__(self, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


_YEARPAGE = ('<label for="g1">0510-2020-100</label>'
             '<label for="g2">0510-2018-401</label>')
_PDF_URL = "https://cdn.havarikommissionen.dk/x/Media/1/r.pdf"
_CASE = (f'<title>Accident to OY-AAA in Roskilde (EKRK) on 5-6-2020</title>'
         f'<a href="{_PDF_URL}">2020-100 (Danish)</a>')
_CASE_NOPDF = '<title>Accident to OY-BBB in X on 1-1-2018</title>'


class FakeClient:
    """2020-100 resolves at /2020/; 2018-401 only at /2015/ (the trap)."""

    def __init__(self):
        self.urls = {
            aibdk.detail_url("2020", "2020-100"): _CASE,
            aibdk.detail_url("2015", "2018-401"): _CASE_NOPDF,
            _PDF_URL: b"%PDF dk",
        }
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if "/search-aviation/2023" == url[-22:][-5:] or url.endswith("search-aviation/2023"):
            return FakeResp(text=_YEARPAGE)
        val = self.urls.get(url)
        if val is None:
            return FakeResp(text="not found", status_code=404)
        if isinstance(val, bytes):
            return FakeResp(content=val)
        return FakeResp(text=val)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(aibdk, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM aibdk_reports"))
    assert ids == ["2018-401", "2020-100"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_year_cascade_and_pdf(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "D" * 8000)
    client = FakeClient()
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aibdk_reports")}
    done = rows["2020-100"]
    assert done["status"] == "parsed"
    assert done["lang"] == "da"
    assert done["registration"] == "OY-AAA"
    assert done["date_of_occurrence"] == "2020-06-05"
    # the trap case resolved via sweep to /2015/ but has no PDF → stays new
    trap = rows["2018-401"]
    assert trap["status"] == "new"
    assert trap["detail_url"] and "/2015/" in trap["detail_url"]
    assert any("/2015/2018-401" in u for u in client.requested)


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "D" * 8000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM aibdk_accidents").fetchone()
    assert acc["case_id"] == "2020-100"
    assert acc["country"] == "DK"
    assert "Roskilde" in acc["location"]


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "D" * 8000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE aibdk_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM aibdk_accidents").fetchone()["c"] == 1


def test_fetch_unresolved_marked_missing(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.aibdk, "resolve_detail",
                        lambda client, cid, max_tries=28: (None, None))

    class NoCase(FakeClient):
        def get(self, url, params=None):
            if url.endswith("search-aviation/2023"):
                return super().get(url, params)
            return FakeResp(text="x", status_code=404)

    pipeline.fetch(conn, NoCase(), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM aibdk_reports WHERE status='missing'"
    ).fetchone()["c"] == 2
