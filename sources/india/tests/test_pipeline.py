"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from india_ingest import db, india, pipeline


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, index_html="", pdfs=None):
        self.index_html = index_html
        self.pdfs = pdfs or {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == india.INDEX_URL:
            return FakeResp(text=self.index_html)
        return FakeResp(content=self.pdfs.get(url, b"%PDF-1.4 x"))


_INDEX = '''
<a href="Reports/2022/Accident/Final report VT-AMU 11.10.23.pdf">x</a>
<a href="Reports/2022/Accident/Preliminary Report VT-PHY.pdf">x</a>
<a href="Reports/2019/SeriousIncident/Accepted Report VT-TEH.pdf">x</a>
'''


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(india, "DELAY", 0)


def test_discover_inserts_non_prelim_with_case_ids(conn):
    n = pipeline.discover(conn, FakeClient(index_html=_INDEX))
    assert n == 2
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM india_reports")}
    assert set(rows) == {"2022_VT-AMU", "2019_VT-TEH"}
    assert rows["2019_VT-TEH"]["report_kind"] == "Serious Incident"


def test_discover_idempotent(conn):
    c = FakeClient(index_html=_INDEX)
    assert pipeline.discover(conn, c) == 2
    assert pipeline.discover(conn, c) == 0


def test_fetch_parses_pdf_and_meta(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html=_INDEX))
    text = ("Final Investigation Report on Accident involving Spice Jet's "
            "B-737-800 aircraft bearing registration VT AMU at Mumbai "
            "on 01 May 2022\n" + "N" * 2000)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    pipeline.fetch(conn, FakeClient(index_html=_INDEX), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM india_reports WHERE case_id='2022_VT-AMU'"
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "pdf"
    assert row["registration"] == "VT-AMU"
    assert row["date_of_occurrence"] == "2022-05-01"
    assert row["operator"] == "Spice Jet"
    assert (tmp_path / "2022_VT-AMU.pdf").exists()


def test_fetch_scan_marked(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html=_INDEX))
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(index_html=_INDEX), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, status FROM india_reports WHERE case_id='2022_VT-AMU'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient(index_html=_INDEX))

    class Boom(FakeClient):
        def get(self, url, params=None):
            if url.endswith(".pdf"):
                raise RuntimeError("boom")
            return super().get(url, params)

    pipeline.fetch(conn, Boom(index_html=_INDEX), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM india_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_build_promotes_and_skips(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html=_INDEX))
    long_text = "X" * 5000
    texts = {"2022_VT-AMU": long_text, "2019_VT-TEH": "short"}

    def fake_extract(p):
        for cid, t in texts.items():
            if cid in str(p):
                return t
        return ""

    monkeypatch.setattr(pipeline.pdf, "extract_text", fake_extract)
    pipeline.fetch(conn, FakeClient(index_html=_INDEX), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM india_accidents").fetchall()
    assert len(acc) == 1
    assert acc[0]["case_id"] == "2022_VT-AMU"
    assert acc[0]["country"] == "IN"
    assert acc[0]["source_url"].startswith("https://aaib.gov.in/Reports/")
    skipped = conn.execute(
        "SELECT status FROM india_reports WHERE case_id='2019_VT-TEH'"
    ).fetchone()
    assert skipped["status"] == "skipped"


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(index_html=_INDEX))
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "X" * 5000)
    pipeline.fetch(conn, FakeClient(index_html=_INDEX), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE india_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM india_accidents").fetchone()["c"] == 2
