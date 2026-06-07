"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from sacaa_ingest import db, pipeline, sacaa


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


def _row(year, date, typ, loc, name, reg, fname):
    return (f'<tr><td>{year}</td><td>{date}</td><td>{typ}</td><td>{loc}</td>'
            f'<td>{name}</td><td>{reg}</td>'
            f'<td><a href="https://x.blob.core.windows.net/c/{fname}">D</a></td></tr>')


_MAIN = ("<table>"
         + _row("2020", "1 May", "C172", "Cape Town", "1234", "ZS-ABC", "1234.pdf")
         + _row("Preliminary Reports", "2 January 2023", "Sling 4", "Bass Lake",
                "ZU-PPA", "ZU-PPA", "ZU-PPA.pdf")
         + "</table>")
_ARCHIVE = ("<table>"
            + _row("1999", "3 April", "PA-28", "Durban", "5678", "ZS-XYZ", "5678.pdf")
            + "</table>")


class FakeClient:
    def __init__(self, pdfs=None):
        self.pdfs = pdfs or {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == sacaa.MAIN_URL:
            return FakeResp(text=_MAIN)
        if url == sacaa.ARCHIVE_URL:
            return FakeResp(text=_ARCHIVE)
        return FakeResp(content=self.pdfs.get(url, b"%PDF-1.4 x"))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sacaa, "DELAY", 0)


def test_discover_both_pages(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 3
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM sacaa_reports"))
    assert ids == ["1234", "5678", "zu-ppa-2023-01-02"]
    kinds = {r["case_id"]: r["report_kind"] for r in conn.execute(
        "SELECT case_id, report_kind FROM sacaa_reports")}
    assert kinds["zu-ppa-2023-01-02"] == "Preliminary"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_and_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    texts = {"1234": "N" * 5000, "5678": "", "zu-ppa-2023-01-02": "P" * 800}

    def fake_extract(p):
        for cid, t in texts.items():
            if cid in str(p):
                return t
        return ""

    monkeypatch.setattr(pipeline.pdf, "extract_text", fake_extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    tiers = {r["case_id"]: r["source_tier"] for r in conn.execute(
        "SELECT case_id, source_tier FROM sacaa_reports")}
    assert tiers["1234"] == "pdf"
    assert tiers["5678"] == "scanned"

    assert pipeline.build(conn) == 2  # 1234 + prelim; scan skipped
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM sacaa_accidents")}
    assert set(acc) == {"1234", "zu-ppa-2023-01-02"}
    assert acc["1234"]["country"] == "ZA"
    assert acc["1234"]["event_date"] == "2020-05-01"
    assert acc["1234"]["report_type"] == "Final"
    skipped = conn.execute(
        "SELECT status FROM sacaa_reports WHERE case_id='5678'").fetchone()
    assert skipped["status"] == "skipped"


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient())

    class Boom(FakeClient):
        def get(self, url, params=None):
            if url.endswith(".pdf"):
                raise RuntimeError("boom")
            return super().get(url, params)

    pipeline.fetch(conn, Boom(), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM sacaa_reports WHERE status='new'"
    ).fetchone()["c"] == 3


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "X" * 5000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE sacaa_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM sacaa_accidents").fetchone()["c"] == 3
