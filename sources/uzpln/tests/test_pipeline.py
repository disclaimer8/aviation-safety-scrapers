"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from uzpln_ingest import uzpln, pipeline

# Two listing pages then the stop page. Page 0 has incidents 830 (recent, has
# CZ- number + spaces/diacritics PDF) and 824 (LKNM). Page 1 has incident 2
# (old, blank CZ- number → surrogate, hash PDF). Page 2 is the stop signal.
_LIST0 = (
    '<table class="table table-striped ">'
    '<tr><th>Vydavatel</th><th>Datum události</th><th>Číslo zprávy</th>'
    '<th>Druh zprávy</th><th>Místo události</th><th>Druh provozu</th>'
    '<th>Druh události</th><th></th></tr>'
    '<tr><td>UZPLN</td><td>2025-07-29</td><td>CZ-25-1428</td>'
    '<td>Závěrečná zpráva</td><td>Ranská hora</td><td>Rekreační</td>'
    '<td>Letecká nehoda</td>'
    '<th><a href="/incident/830"><img src="/images/next.png"/></a></th></tr>'
    '<tr><td>UZPLN</td><td>2025-07-26</td><td>CZ-25-1379</td>'
    '<td>Závěrečná zpráva</td><td>LKNM</td><td>Rekreační</td>'
    '<td>Vážný incident</td>'
    '<th><a href="/incident/824"><img src="/images/next.png"/></a></th></tr>'
    '</table>'
)
_LIST1 = (
    '<table class="table table-striped ">'
    '<tr><th>Vydavatel</th><th>Datum události</th><th>Číslo zprávy</th>'
    '<th>Druh zprávy</th><th>Místo události</th><th>Druh provozu</th>'
    '<th>Druh události</th><th></th></tr>'
    '<tr><td>UZPLN</td><td>2003-02-22</td><td>  </td>'
    '<td>Závěrečná zpráva</td><td>Radotín</td><td>Rekreační</td>'
    '<td>Letecká nehoda</td>'
    '<th><a href="/incident/2"><img src="/images/next.png"/></a></th></tr>'
    '</table>'
)
# Stop page: NO /incident/ links.
_LIST_STOP = '<table class="table table-striped "><tr><th>Vydavatel</th></tr></table>'

_DETAIL_830 = (
    '<table class="table table-striped">'
    '<tr><th><b>Datum události:</b></th><td>2025.07.29</td></tr>'
    '<tr><th><b>Číslo zprávy:</b></th><td>CZ-25-1428</td></tr>'
    '<tr><th><b>Druh zprávy:</b></th><td>Závěrečná zpráva</td></tr>'
    '<tr><th><b>Místo události:</b></th><td>Ranská hora</td></tr>'
    '<tr><th><b>Druh události:</b></th><td>Letecká nehoda</td></tr>'
    '<tr><th><b>Druh provozu:</b></th><td>Rekreační</td></tr>'
    '<tr><th><b>Typ letadla / SLZ:</b></th><td>MAGIC M</td></tr>'
    '<tr><th><b>PDF dokument:</b></th>'
    '<td><a href="/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf">x</a></td></tr>'
    '</table>'
)
_DETAIL_824 = (
    '<table class="table table-striped">'
    '<tr><th><b>Datum události:</b></th><td>2025.07.26</td></tr>'
    '<tr><th><b>Číslo zprávy:</b></th><td>CZ-25-1379</td></tr>'
    '<tr><th><b>Druh zprávy:</b></th><td>Závěrečná zpráva</td></tr>'
    '<tr><th><b>Místo události:</b></th><td>LKNM</td></tr>'
    '<tr><th><b>Typ letadla / SLZ:</b></th><td>CESSNA 172</td></tr>'
    '<tr><th><b>PDF dokument:</b></th>'
    '<td><a href="/pdf/abcd1234.pdf">x</a></td></tr>'
    '</table>'
)
_DETAIL_2 = (
    '<table class="table table-striped">'
    '<tr><th><b>Datum události:</b></th><td>2003.02.22</td></tr>'
    '<tr><th><b>Číslo zprávy:</b></th><td></td></tr>'
    '<tr><th><b>Druh zprávy:</b></th><td>Závěrečná zpráva</td></tr>'
    '<tr><th><b>Místo události:</b></th><td>Radotín</td></tr>'
    '<tr><th><b>Typ letadla / SLZ:</b></th><td>MAGGIC 165.</td></tr>'
    '<tr><th><b>PDF dokument:</b></th>'
    '<td><a href="/pdf/ecrSLXV8.pdf">x</a></td></tr>'
    '</table>'
)

B = uzpln.BASE
_PAGES = {
    B + "/zpravy-ln?page=0": _LIST0,
    B + "/zpravy-ln?page=1": _LIST1,
    B + "/zpravy-ln?page=2": _LIST_STOP,
    B + "/incident/830": _DETAIL_830,
    B + "/incident/824": _DETAIL_824,
    B + "/incident/2": _DETAIL_2,
}
_PDF_830 = (B + "/pdf/202601121455-ZZ%20CZ-25-1428%20Origin%C3%A1l%20PK.pdf")
_PDF_824 = B + "/pdf/abcd1234.pdf"
_PDF_2 = B + "/pdf/ecrSLXV8.pdf"


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages=None, pdfs=None, fail_pdf=None, recover=None):
        self.pages = pages if pages is not None else dict(_PAGES)
        self.pdfs = pdfs if pdfs is not None else {
            _PDF_830: b"%PDF 830", _PDF_824: b"%PDF 824", _PDF_2: b"%PDF 2"}
        self.fail_pdf = fail_pdf or set()
        # recover: {url: html} — first GET returns the stop page (transient
        # rate-limit blank), the retry GET returns the real data html.
        self.recover = dict(recover or {})
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.recover:
            return FakeResp(text=self.recover.pop(url))  # recovers on retry
        if url in self.pages:
            return FakeResp(text=self.pages[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        # Any further listing page beyond the stop signal: empty (no links).
        if "/zpravy-ln" in url:
            return FakeResp(text=_LIST_STOP)
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(uzpln, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 3
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM uzpln_reports")}
    assert set(rows) == {"CZ-25-1428", "CZ-25-1379", "uzpln-2"}
    assert rows["CZ-25-1428"]["incident_id"] == "830"
    assert rows["CZ-25-1428"]["date_of_occurrence"] == "2025-07-29"
    assert rows["CZ-25-1428"]["aircraft"] == "MAGIC M"
    assert rows["CZ-25-1428"]["event_kind"] == "Letecká nehoda"
    # spaces/diacritics PDF href URL-encoded at discover time
    assert rows["CZ-25-1428"]["pdf_url"] == _PDF_830
    # old report: surrogate case_id, hash PDF
    assert rows["uzpln-2"]["pdf_url"] == _PDF_2
    assert rows["uzpln-2"]["report_number"] is None


def test_discover_stops_at_signal(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    # Pages 2,3,4 are link-less; the walk halts after EMPTY_STREAK_STOP (3)
    # consecutive blanks → page 4 requested, page 5 never reached.
    assert (B + "/zpravy-ln?page=4") in client.requested
    assert (B + "/zpravy-ln?page=5") not in client.requested


def test_discover_tolerates_transient_blank(conn):
    # A link-less page that RECOVERS on retry is a rate-limit blank, not the
    # stop page: the walk must re-fetch it and keep going.
    pages = dict(_PAGES)
    pages[B + "/zpravy-ln?page=1"] = _LIST_STOP   # first GET blank (transient)
    n = pipeline.discover(
        conn,
        FakeClient(pages=pages, recover={B + "/zpravy-ln?page=1": _LIST1}),
    )
    # 830 + 824 (page0) + 2 (page1 recovered) all ingested despite the blank.
    assert n == 3
    cids = {r["case_id"] for r in conn.execute(
        "SELECT case_id FROM uzpln_reports")}
    assert "uzpln-2" in cids


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    # Second run: incident_id de-dupe skips all; no detail GETs either.
    client = FakeClient()
    assert pipeline.discover(conn, client) == 0
    assert (B + "/incident/830") not in client.requested


def test_fetch_pdf_tier_and_registration(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Zpráva " + "N" * 9000 + " značky OK-PHQ pilota.")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM uzpln_reports")}
    assert rows["CZ-25-1428"]["source_tier"] == "pdf"
    assert rows["CZ-25-1428"]["status"] == "parsed"
    assert rows["CZ-25-1428"]["registration"] == "OK-PHQ"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM uzpln_reports WHERE case_id='CZ-25-1428'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF_830})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM uzpln_reports WHERE case_id='CZ-25-1428'").fetchone()
    assert row["status"] == "new"  # retried next cycle


def test_build_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(p):
        return "N" * 9000 if "CZ-25-1428" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM uzpln_accidents")}
    assert set(acc) == {"CZ-25-1428"}
    assert acc["CZ-25-1428"]["country"] == "CZ"
    assert acc["CZ-25-1428"]["lang"] == "cs"
    assert acc["CZ-25-1428"]["report_type"] == "Závěrečná zpráva"
    assert acc["CZ-25-1428"]["event_date"] == "2025-07-29"
    assert acc["CZ-25-1428"]["source_url"].endswith("/incident/830")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM uzpln_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE uzpln_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM uzpln_accidents").fetchone()["c"] == 3
