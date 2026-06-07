"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from ovv_ingest import db, ovv, pipeline


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


_D1 = "https://onderzoeksraad.nl/en/onderzoek/crash-ph-abc-somewhere/"
_D2 = "https://onderzoeksraad.nl/en/onderzoek/ongoing-thing/"
_LISTING = (f'<a href="{_D1}">x</a><a href="{_D2}">y</a>')
_DOC_MAIN = "https://onderzoeksraad.nl/ab12cd34ef56report_crash_en-pdf/"
_DOC_APP = "https://onderzoeksraad.nl/ab12cd34ef99rapport_crash_appendix-pdf/"
_DETAIL1 = (f'<h1>Crash PH-ABC at Somewhere on 6 June 2021</h1>'
            f'<p>{"S" * 200}</p>'
            f'<a href="{_DOC_APP}">app</a><a href="{_DOC_MAIN}">main</a>')
_DETAIL2 = f'<h1>Ongoing</h1><p>{"T" * 200}</p>'


class FakeClient:
    def __init__(self, pages=None, urls=None):
        self.pages = pages if pages is not None else {1: _LISTING, 2: ""}
        self.urls = urls if urls is not None else {
            _D1: _DETAIL1, _D2: _DETAIL2,
            _DOC_MAIN: b"%PDF main", _DOC_APP: b"%PDF app"}
        self.requested = []

    def get(self, url, params=None):
        if params is not None and "_page" in params:
            self.requested.append(f"page={params['_page']}")
            return FakeResp(text=self.pages.get(params["_page"], ""))
        self.requested.append(url)
        val = self.urls.get(url)
        if val is None:
            raise RuntimeError("404")
        if isinstance(val, bytes):
            return FakeResp(content=val)
        return FakeResp(text=val)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ovv, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM ovv_reports"))
    assert ids == ["crash-ph-abc-somewhere", "ongoing-thing"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_picks_main_doc(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM ovv_reports WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["pdf_url"] == _DOC_MAIN  # EN main report outranks appendix
    assert row["lang"] == "en"
    assert row["registration"] == "PH-ABC"
    assert row["date_of_occurrence"] == "2021-06-06"
    # ongoing: metadata stored, stays new
    ongoing = conn.execute(
        "SELECT status, title FROM ovv_reports WHERE case_id='ongoing-thing'"
    ).fetchone()
    assert ongoing["status"] == "new"
    assert ongoing["title"] == "Ongoing"


def test_fetch_scan_falls_through_to_next_doc(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    texts = {_DOC_MAIN: "", _DOC_APP: "A" * 9000}
    current = {}

    def fake_dl(client, url, dest):
        current["url"] = url
        open(dest, "wb").write(b"x")
        return dest

    monkeypatch.setattr(pipeline.ovv, "download_pdf", fake_dl)
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: texts[current["url"]])
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT pdf_url, lang FROM ovv_reports WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert row["pdf_url"] == _DOC_APP  # main was a scan → appendix won
    assert row["lang"] == "nl"


def test_fetch_summary_fallback(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM ovv_reports "
        "WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert row["source_tier"] == "html"
    assert row["narrative_text"].startswith("S")


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = conn.execute("SELECT * FROM ovv_accidents").fetchone()
    assert acc["case_id"] == "crash-ph-abc-somewhere"
    assert acc["country"] == "NL"
    assert acc["source_url"] == _D1


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE ovv_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM ovv_accidents").fetchone()["c"] == 1
