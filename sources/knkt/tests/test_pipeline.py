"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import json
import pathlib

import pytest

from knkt_ingest import db, knkt, pipeline

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, rows, pdfs=None):
        self.rows = rows
        self.pdfs = pdfs if pdfs is not None else {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == knkt.LISTING_URL:
            return FakeResp(payload={"Error": False, "Message": self.rows})
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        raise RuntimeError("404")


_ROWS = [
    {"Tanggal": "2007-01-01", "Keterangan":
        "Loss of Control-Inflight, Adam Air (Boeing 737-400/PK-KKW); "
        "Selat Makassar / KNKT.07.01.01.04",
     "Final_Report": "PK-KKW Final Report.pdf"},
    {"Tanggal": "2008-03-10", "Keterangan":
        "Hard Landing, X Air (ATR 72/PK-XYZ); Somewhere / KNKT.22.07.11.04",
     "Preliminary_Report": "KNKT.22.07.11.04-Preliminary-Report.pdf"},
    {"Tanggal": "2009-05-05", "Keterangan": "Stub, no report (C172/PK-STB); Nowhere"},
]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(knkt, "DELAY", 0)


def test_discover_keeps_report_rows_only(conn):
    n = pipeline.discover(conn, FakeClient(_ROWS))
    assert n == 2
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM knkt_reports"))
    assert ids == ["KNKT.07.01.01.04", "KNKT.22.07.11.04"]
    row = conn.execute(
        "SELECT * FROM knkt_reports WHERE case_id='KNKT.07.01.01.04'").fetchone()
    assert row["operator"] == "Adam Air"
    assert row["registration"] == "PK-KKW"
    assert row["report_kind"] == "Final"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient(_ROWS)) == 2
    assert pipeline.discover(conn, FakeClient(_ROWS)) == 0


def test_fetch_year_fallback(conn, tmp_path, monkeypatch):
    """The 2008-occurrence row 404s under /2008/ and succeeds under /2022/."""
    pipeline.discover(conn, FakeClient(_ROWS))
    good = ("https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2022/"
            "KNKT.22.07.11.04-Preliminary-Report.pdf")
    pdfs = {
        "https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2007/"
        "PK-KKW%20Final%20Report.pdf": b"%PDF a",
        good: b"%PDF b",
    }
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 5000)
    client = FakeClient(_ROWS, pdfs=pdfs)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT pdf_url, status FROM knkt_reports WHERE case_id='KNKT.22.07.11.04'"
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["pdf_url"] == good
    # the failed /2008/ candidate was attempted first
    assert any("/2008/" in u for u in client.requested)


def test_fetch_all_candidates_fail_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient(_ROWS))
    pipeline.fetch(conn, FakeClient(_ROWS, pdfs={}), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM knkt_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(_ROWS))
    pdfs = {
        "https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2007/"
        "PK-KKW%20Final%20Report.pdf": b"%PDF a",
        "https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2022/"
        "KNKT.22.07.11.04-Preliminary-Report.pdf": b"%PDF b",
    }
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 5000)
    pipeline.fetch(conn, FakeClient(_ROWS, pdfs=pdfs), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM knkt_accidents")}
    assert acc["KNKT.07.01.01.04"]["country"] == "ID"
    assert acc["KNKT.07.01.01.04"]["event_date"] == "2007-01-01"
    assert acc["KNKT.07.01.01.04"]["operator"] == "Adam Air"
    assert acc["KNKT.07.01.01.04"]["source_url"].endswith("PK-KKW%20Final%20Report.pdf")


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(_ROWS))
    pdfs = {
        "https://knkt.go.id/Repo/Files/Laporan/Penerbangan/2007/"
        "PK-KKW%20Final%20Report.pdf": b"%PDF a",
    }
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 5000)
    pipeline.fetch(conn, FakeClient(_ROWS, pdfs=pdfs), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE knkt_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM knkt_accidents").fetchone()["c"] == 1
