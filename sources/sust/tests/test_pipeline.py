"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import json
import pathlib

import pytest

from sust_ingest import db, sust, pipeline

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SKELETON = (FIXTURES / "skeleton.html").read_text()
ENTRY_MULTI = json.loads((FIXTURES / "entry_multi.json").read_text())
ENTRY_DOCLESS = json.loads((FIXTURES / "entry_docless.json").read_text())


class FakeResp:
    def __init__(self, text="", payload=None, content=b""):
        self.text = text
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """
    Serves the skeleton on SKELETON_URL, getEntry JSON keyed by uid (extracted
    from the lazyload URL), and PDF bytes keyed by absolute PDF URL.
    Any unknown PDF URL raises (simulates a fetch failure).
    """
    def __init__(self, entries=None, pdfs=None, skeleton=SKELETON):
        self.entries = entries or {}     # uid:int -> json dict
        self.pdfs = pdfs if pdfs is not None else {}
        self.skeleton = skeleton
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == sust.SKELETON_URL:
            return FakeResp(text=self.skeleton)
        if "getEntry" in url or "tx_sustemas_listavexamination%5Bid%5D" in url:
            import re
            m = re.search(r"%5Bid%5D=(\d+)", url)
            uid = int(m.group(1))
            return FakeResp(payload=self.entries.get(uid, {"uid": uid,
                            "aircrafts": [], "documents": []}))
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        raise RuntimeError(f"404 {url}")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sust, "DELAY", 0)


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_all_rows(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 10
    row = conn.execute(
        "SELECT * FROM sust_reports WHERE case_id='3844'").fetchone()
    assert row["status"] == "new"
    assert "getEntry" in row["lazyload_url"]
    assert "&amp;" not in row["lazyload_url"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 10
    assert pipeline.discover(conn, FakeClient()) == 0


# ── fetch ─────────────────────────────────────────────────────────────────────

def _multi_pdf_url():
    return sust.absolute_url(ENTRY_MULTI["documents"][-1]["url"])  # FB_D


def test_fetch_parses_and_downloads(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    pdfs = {_multi_pdf_url(): b"%PDF final"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "S" * 6000)
    client = FakeClient(entries={3811: ENTRY_MULTI}, pdfs=pdfs)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM sust_reports WHERE case_id='3811'").fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "pdf"
    assert row["lang"] == "de"
    assert row["registration"] == "D-EXIK"
    assert row["pdf_url"].endswith("D-EXIK_FB_D.pdf")


def test_fetch_docless_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(entries={3751: ENTRY_DOCLESS})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM sust_reports WHERE case_id='3751'").fetchone()
    assert row["status"] == "new"          # self-heal next cycle
    assert row["date_of_occurrence"] == "2023-11-14"  # metadata still captured


def test_fetch_pdf_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient())
    # entry has a doc, but PDF URL not in pdfs -> download raises
    client = FakeClient(entries={3811: ENTRY_MULTI}, pdfs={})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM sust_reports WHERE case_id='3811'").fetchone()
    assert row["status"] == "new"
    assert row["doc_name"] == "Schlussbericht"  # doc choice persisted


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    pdfs = {_multi_pdf_url(): b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "tiny")
    client = FakeClient(entries={3811: ENTRY_MULTI}, pdfs=pdfs)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, status FROM sust_reports WHERE case_id='3811'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_max_rows_caps(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "S" * 6000)
    client = FakeClient()  # generic empty-doc entries -> all stay new
    processed = pipeline.fetch(conn, client, pdf_dir=str(tmp_path), max_rows=3)
    assert processed == 3


# ── build ─────────────────────────────────────────────────────────────────────

def _parsed_3811(conn, tmp_path, monkeypatch, text="S" * 6000):
    pipeline.discover(conn, FakeClient())
    pdfs = {_multi_pdf_url(): b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    pipeline.fetch(conn, FakeClient(entries={3811: ENTRY_MULTI}, pdfs=pdfs),
                   pdf_dir=str(tmp_path))


def test_build_floor_and_country(conn, tmp_path, monkeypatch):
    _parsed_3811(conn, tmp_path, monkeypatch)
    assert pipeline.build(conn) == 1
    acc = conn.execute(
        "SELECT * FROM sust_accidents WHERE case_id='3811'").fetchone()
    assert acc["country"] == "CH"
    assert acc["event_date"] == "2025-03-17"
    assert acc["registration"] == "D-EXIK"
    assert acc["aircraft"] == "EXTRA AIRCRAFT EA400"
    assert acc["report_type"] == "Schlussbericht"
    assert acc["lang"] == "de"
    assert acc["source_url"].endswith("D-EXIK_FB_D.pdf")


def test_build_below_floor_skipped(conn, tmp_path, monkeypatch):
    _parsed_3811(conn, tmp_path, monkeypatch, text="short")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT status FROM sust_reports WHERE case_id='3811'"
    ).fetchone()["status"] == "skipped"


def test_build_idempotent(conn, tmp_path, monkeypatch):
    _parsed_3811(conn, tmp_path, monkeypatch)
    pipeline.build(conn)
    conn.execute("UPDATE sust_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM sust_accidents").fetchone()["c"] == 1
