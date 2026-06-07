"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from aaiu_ingest import aaiu, db, pipeline


class FakeResp:
    def __init__(self, payload=None, text="", content=b""):
        self._payload = payload
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _post(wp_id, title, link, synopsis="S " * 300):
    return {"id": wp_id, "link": link,
            "title": {"rendered": title},
            "content": {"rendered": f"<p>{synopsis}</p>"}}


_PAGE1 = [
    _post(11, "Final Report: Accident involving a Cessna 172, registration "
              "EI-AAA, at Weston on 2 May 2023. Report 2023-005",
          "https://aaiu.ie/aaiu_report/r1/"),
    _post(12, "ACCIDENT Piper PA-28 EI-BBB Sligo 3 June 1999",
          "https://aaiu.ie/aaiu_report/r2/"),
]


class FakeClient:
    def __init__(self, pages=None, page_html=None, pdfs=None):
        self.pages = pages or {1: _PAGE1, 2: []}
        self.page_html = page_html or {}
        self.pdfs = pdfs or {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == aaiu.REST_URL:
            return FakeResp(payload=self.pages.get(params["page"], []))
        if url in self.page_html:
            return FakeResp(text=self.page_html[url])
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(aaiu, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiu_reports")}
    assert set(rows) == {"2023-005", "wp-12"}
    assert rows["2023-005"]["registration"] == "EI-AAA"
    assert rows["wp-12"]["registration"] == "EI-BBB"
    assert len(rows["2023-005"]["synopsis"]) > 300


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    pdf_url = "https://aaiu.ie/wp-content/uploads/2023/06/Report-2023-005.pdf"
    client = FakeClient(
        page_html={"https://aaiu.ie/aaiu_report/r1/":
                   f'<a href="{pdf_url}">DL</a>',
                   "https://aaiu.ie/aaiu_report/r2/": "<p>no pdf</p>"},
        pdfs={pdf_url: b"%PDF x"},
    )
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiu_reports")}
    assert rows["2023-005"]["source_tier"] == "pdf"
    assert len(rows["2023-005"]["narrative_text"]) == 9000
    # no PDF on the page → synopsis fallback, tier html
    assert rows["wp-12"]["source_tier"] == "html"
    assert len(rows["wp-12"]["narrative_text"]) > 300


def test_fetch_scanned_pdf_falls_back_to_synopsis(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    pdf_url = "https://aaiu.ie/wp-content/uploads/2023/06/Report-2023-005.pdf"
    client = FakeClient(
        page_html={"https://aaiu.ie/aaiu_report/r1/":
                   f'<a href="{pdf_url}">DL</a>',
                   "https://aaiu.ie/aaiu_report/r2/": ""},
        pdfs={pdf_url: b"%PDF scan"},
    )
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM aaiu_reports WHERE case_id='2023-005'").fetchone()
    assert row["source_tier"] == "scanned"
    assert len(row["narrative_text"]) > 300  # synopsis kept


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(
        page_html={"https://aaiu.ie/aaiu_report/r1/": "",
                   "https://aaiu.ie/aaiu_report/r2/": ""})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 2  # both synopsis-tier, >=300
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiu_accidents")}
    assert acc["2023-005"]["country"] == "IE"
    assert acc["2023-005"]["event_date"] == "2023-05-02"
    assert acc["2023-005"]["source_url"] == "https://aaiu.ie/aaiu_report/r1/"


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(page_html={"https://aaiu.ie/aaiu_report/r1/": "",
                                   "https://aaiu.ie/aaiu_report/r2/": ""})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE aaiu_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM aaiu_accidents").fetchone()["c"] == 2
