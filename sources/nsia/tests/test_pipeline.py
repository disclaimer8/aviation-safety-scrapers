"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from nsia_ingest import db, nsia, pipeline


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


def _row(case, aircraft, reg, date, loc, lang):
    href = f"/Aviation/Aviation/Published-reports/{case.replace('/', '-')}"
    return (f'<tr><td><a href="{href}">{case}</a></td>'
            f'<td><a href="{href}">{aircraft}</a></td>'
            f'<td><a href="{href}">{reg}</a></td>'
            f'<td><a href="{href}">{date}</a></td>'
            f'<td><a href="{href}">{loc}</a></td>'
            f'<td><a href="{href}">{lang}</a></td>'
            f'<td><a href="{href}">0</a></td></tr>')


_P0 = "<table>" + _row("2024/02", "Piper PA-28", "LN-NAS", "11.05.2021",
                       "Voss", "Norwegian") + \
      _row("2023/05", "Cessna 172", "LN-ABC", "01.02.2022", "Oslo",
           "English") + "</table>"

_DETAIL = ('<title>Report X | NSIA</title><table>'
           '<tr><td>Operator</td><td>Private</td></tr>'
           '<tr><td>Type of occurrence</td><td>Accident</td></tr></table>')


class FakeClient:
    def __init__(self, pages=None, urls=None):
        self.pages = pages if pages is not None else {1: _P0, 2: ""}
        self.urls = urls or {}
        self.requested = []

    def get(self, url, params=None):
        if params is not None and "page" in params:
            self.requested.append(f"page={params['page']}")
            return FakeResp(text=self.pages.get(params["page"], ""))
        self.requested.append(url)
        val = self.urls.get(url)
        if val is None:
            raise RuntimeError("404")
        if isinstance(val, bytes):
            return FakeResp(content=val)
        return FakeResp(text=val)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(nsia, "DELAY", 0)


def test_discover_stop_on_empty(conn):
    client = FakeClient()
    n = pipeline.discover(conn, client)
    assert n == 2
    assert "page=0" not in client.requested
    assert "page=3" not in client.requested
    row = conn.execute(
        "SELECT * FROM nsia_reports WHERE case_id='2024-02'").fetchone()
    assert row["lang"] == "Norwegian"
    assert row["date_of_occurrence"] == "2021-05-11"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def _detail_urls():
    d1 = "https://nsia.no/Aviation/Aviation/Published-reports/2024-02"
    d2 = "https://nsia.no/Aviation/Aviation/Published-reports/2023-05"
    return d1, d2


def test_fetch_and_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    d1, d2 = _detail_urls()
    urls = {d1: _DETAIL, d2: _DETAIL,
            nsia.pdf_url(d1): b"%PDF a", nsia.pdf_url(d2): b"%PDF b"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 8000)
    pipeline.fetch(conn, FakeClient(urls=urls), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM nsia_reports")}
    assert rows["2024-02"]["status"] == "parsed"
    assert rows["2024-02"]["operator"] == "Private"
    assert rows["2024-02"]["report_kind"] == "Accident"

    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM nsia_accidents")}
    assert acc["2024-02"]["country"] == "NO"
    assert acc["2024-02"]["event_date"] == "2021-05-11"
    assert acc["2024-02"]["source_url"] == d1


def test_fetch_scan_marked(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    d1, d2 = _detail_urls()
    urls = {d1: _DETAIL, d2: _DETAIL,
            nsia.pdf_url(d1): b"%PDF scan", nsia.pdf_url(d2): b"%PDF scan"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(urls=urls), pdf_dir=str(tmp_path))
    tiers = {r["case_id"]: r["source_tier"] for r in conn.execute(
        "SELECT case_id, source_tier FROM nsia_reports")}
    assert set(tiers.values()) == {"scanned"}
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM nsia_reports WHERE status='skipped'"
    ).fetchone()["c"] == 2


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient())
    pipeline.fetch(conn, FakeClient(urls={}), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM nsia_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    d1, d2 = _detail_urls()
    urls = {d1: _DETAIL, d2: _DETAIL,
            nsia.pdf_url(d1): b"%PDF a", nsia.pdf_url(d2): b"%PDF b"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 8000)
    pipeline.fetch(conn, FakeClient(urls=urls), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE nsia_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute("SELECT COUNT(*) c FROM nsia_accidents").fetchone()["c"] == 2
