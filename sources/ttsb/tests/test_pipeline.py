"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pathlib

import pytest

from ttsb_ingest import ttsb, pipeline

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
_EN = (FIXTURES / "en_list.html").read_text(encoding="utf-8")
_ZH = (FIXTURES / "zh_list.html").read_text(encoding="utf-8")
_DETAIL = (FIXTURES / "en_detail.html").read_text(encoding="utf-8")

# PDF URLs harvested from the fixtures.
_EN_B86 = "https://www.ttsb.gov.tw/media/9314/b-86002_executivesummary.pdf"
_ZH_B86 = "https://www.ttsb.gov.tw/media/9234/%E5%AE%89%E6%8D%B7b-86002%E8%AA%BF%E6%9F%A5%E5%A0%B1%E5%91%8A.pdf"


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    """
    Serves EN page 1 (the 3-row fixture), ZH page 1 (the 2-row fixture), all
    other pages blank, the EN detail page (for the More-Reports harvest path),
    and arbitrary /media PDFs as bytes. PDF text is supplied by monkeypatching
    pdf.extract_text in the tests.
    """

    def __init__(self, fail=None):
        self.pages = {ttsb.en_list_url(1): _EN, ttsb.zh_list_url(1): _ZH}
        self.fail = fail or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("502")
        if url in self.pages:
            return FakeResp(text=self.pages[url])
        if "Lpsimplelist" in url:
            return FakeResp(text="")  # blank trailing pages
        if url.endswith("/post"):
            return FakeResp(text=_DETAIL)  # any detail page
        if "/media/" in url:
            return FakeResp(content=b"%PDF-1.4 fake")
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ttsb, "DELAY", 0)


def test_discover_inserts_three_reports(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 3
    rows = {r["detail_id"]: r for r in conn.execute(
        "SELECT * FROM ttsb_reports")}
    assert set(rows) == {"44578", "44273", "34932"}
    # B-86002 got both EN and ZH PDF URLs wired (matched pair).
    b86 = rows["44273"]
    assert b86["en_pdf_url"].endswith("b-86002_executivesummary.pdf")
    assert b86["zh_pdf_url"] is not None
    assert b86["registration"] == "B-86002"
    assert b86["date_of_occurrence"] == "2024-11-04"
    # case_id seeded from the media slug.
    assert b86["case_id"] == "b-86002"


def test_discover_walks_all_pages_both_langs(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    for p in range(1, ttsb.NUM_PAGES + 1):
        assert ttsb.en_list_url(p) in client.requested
        assert ttsb.zh_list_url(p) in client.requested


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_discover_drone_row(conn):
    pipeline.discover(conn, FakeClient())
    drone = conn.execute(
        "SELECT * FROM ttsb_reports WHERE detail_id='34932'").fetchone()
    assert drone["registration"] == "B-AAA01397"
    assert ttsb.is_drone(drone["registration"])


def test_fetch_prefers_zh_full_when_en_is_stub(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(path):
        p = str(path)
        if p.endswith(".zh.pdf"):
            return "Z" * 90000          # ZH full report
        return "Relato " + "E" * 5000   # EN exec summary (< 15K threshold)
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))

    b86 = conn.execute(
        "SELECT * FROM ttsb_reports WHERE detail_id='44273'").fetchone()
    assert b86["lang"] == "zh"
    assert len(b86["narrative_text"]) == 90000
    assert b86["en_summary_text"].startswith("Relato")
    assert b86["source_tier"] == "pdf"
    assert b86["status"] == "parsed"


def test_fetch_keeps_en_when_full(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: "E" * 40000)  # EN already full
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    b86 = conn.execute(
        "SELECT * FROM ttsb_reports WHERE detail_id='44273'").fetchone()
    assert b86["lang"] == "en"
    assert b86["en_summary_text"] is None
    assert len(b86["narrative_text"]) == 40000


def test_fetch_upgrades_case_id_from_report_number(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(path):
        base = "E" * 40000
        # Only the B-86002 EN report carries a TTSB report number.
        if "b-86002.en" in str(path):
            return base + " Report No. TTSB-AOR-25-11-001 "
        return base
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    # detail 44273 upgrades from the media-slug seed to its report number.
    upgraded = conn.execute(
        "SELECT case_id FROM ttsb_reports WHERE detail_id='44273'").fetchone()
    assert upgraded["case_id"] == "TTSB-AOR-25-11-001"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    b86 = conn.execute(
        "SELECT * FROM ttsb_reports WHERE detail_id='44273'").fetchone()
    assert b86["source_tier"] == "scanned"
    assert b86["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "E" * 40000)
    client = FakeClient(fail={_EN_B86})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    b86 = conn.execute(
        "SELECT * FROM ttsb_reports WHERE detail_id='44273'").fetchone()
    assert b86["status"] == "new"  # retried next cycle


def test_build_floor_and_country(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(path):
        # Only B-86002 (.en.pdf) yields a long EN narrative; others tiny.
        return "E" * 40000 if "b-86002.en" in str(path) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute(
        "SELECT * FROM ttsb_accidents")}
    assert set(acc) == {"b-86002"}
    a = acc["b-86002"]
    assert a["country"] == "TW"
    assert a["lang"] == "en"
    assert a["report_type"] == "Executive Summary"
    assert a["event_date"] == "2024-11-04"
    assert a["source_url"].endswith("/post")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM ttsb_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 2


def test_build_zh_lang_propagates(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(path):
        if str(path).endswith(".zh.pdf"):
            return "Z" * 90000
        return "E" * 5000
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    a = conn.execute(
        "SELECT * FROM ttsb_accidents WHERE case_id='b-86002'").fetchone()
    assert a["lang"] == "zh"
    assert a["en_summary_text"].startswith("E")
    assert len(a["narrative_text"]) == 90000


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "E" * 40000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE ttsb_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM ttsb_accidents").fetchone()["c"] == 3
