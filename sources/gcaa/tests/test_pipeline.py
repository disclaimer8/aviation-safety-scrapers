"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from gcaa_ingest import gcaa, pipeline


class FakeResp:
    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    """Serves the items API (OData verbose) and PDF bytes by URL."""
    def __init__(self, items, pdfs=None):
        self.items = items
        self.pdfs = pdfs if pdfs is not None else {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == gcaa.ITEMS_URL:
            return FakeResp(payload={"d": {"results": self.items}})
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        raise RuntimeError("404")


def _item(item_id, ref, reg="A6-ABC", status="Final", attach=True):
    afs = []
    if attach:
        srv = (f"/en/departments/airaccidentinvestigation/Lists/"
               f"Incidents Investigation Reports/Attachments/{item_id}/"
               f"{status} Report {item_id}.pdf")
        afs = [{"FileName": f"{status} Report {item_id}.pdf",
                "ServerRelativeUrl": srv}]
    return {
        "Id": item_id, "ID": item_id, "Reference_x0020_No": ref,
        "Registration_x0020_No": reg, "Aircraft_x0020_Type": "Boeing 737",
        "Location": "Dubai International Airport", "Damage": "Minor",
        "Occurrence_x0020_Date": "2020-01-01T00:00:00Z",
        "Occurrence_x0020_Category": "Accident",
        "Report_x0020_Status": status, "OccurrenceYear": "2020",
        "AttachmentFiles": {"results": afs},
    }


# two attachment-bearing items + one stub (no attachment)
_ITEMS = [
    _item(1, "AIFN/0007/2013"),
    _item(2, "AIFN/0009/2020", reg="UP-A3003", status="Summary"),
    _item(136, "AIFN/0007/2021", attach=False),  # stub
]
_URL1 = gcaa.attachment_url(_ITEMS[0]["AttachmentFiles"]["results"][0]["ServerRelativeUrl"])
_URL2 = gcaa.attachment_url(_ITEMS[1]["AttachmentFiles"]["results"][0]["ServerRelativeUrl"])


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gcaa, "DELAY", 0)


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover_keeps_attachment_bearing_only(conn):
    n = pipeline.discover(conn, FakeClient(_ITEMS))
    assert n == 2  # stub Id 136 skipped
    ids = sorted(r["case_id"] for r in conn.execute("SELECT case_id FROM gcaa_reports"))
    assert ids == ["aifn-0007-2013", "aifn-0009-2020"]


def test_discover_stores_metadata_and_url(conn):
    pipeline.discover(conn, FakeClient(_ITEMS))
    row = conn.execute(
        "SELECT registration, aircraft, report_status, pdf_url, "
        "occurrence_category FROM gcaa_reports WHERE case_id='aifn-0007-2013'"
    ).fetchone()
    assert row["registration"] == "A6-ABC"
    assert row["report_status"] == "Final"
    assert row["occurrence_category"] == "Accident"
    assert row["pdf_url"].startswith("https://www.gcaa.gov.ae/")
    assert "%20" in row["pdf_url"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient(_ITEMS)) == 2
    assert pipeline.discover(conn, FakeClient(_ITEMS)) == 0


def test_discover_null_reference_fallback(conn):
    items = [_item(901, None, reg="A6-ZZZ")]
    pipeline.discover(conn, FakeClient(items))
    assert conn.execute(
        "SELECT case_id FROM gcaa_reports").fetchone()["case_id"] == "gcaa-901"


# ── fetch ─────────────────────────────────────────────────────────────────────

def test_fetch_success_parses(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(_ITEMS))
    pdfs = {_URL1: b"%PDF", _URL2: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 6000)
    pipeline.fetch(conn, FakeClient(_ITEMS, pdfs=pdfs), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT status, source_tier FROM gcaa_reports WHERE case_id='aifn-0007-2013'"
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "pdf"


def test_fetch_failure_stays_new(conn, tmp_path):
    pipeline.discover(conn, FakeClient(_ITEMS))
    pipeline.fetch(conn, FakeClient(_ITEMS, pdfs={}), pdf_dir=str(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) c FROM gcaa_reports WHERE status='new'"
    ).fetchone()["c"] == 2


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient(_ITEMS))
    pdfs = {_URL1: b"%PDF", _URL2: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    pipeline.fetch(conn, FakeClient(_ITEMS, pdfs=pdfs), pdf_dir=str(tmp_path))
    tiers = {r["source_tier"] for r in conn.execute(
        "SELECT source_tier FROM gcaa_reports")}
    assert tiers == {"scanned"}


# ── build ─────────────────────────────────────────────────────────────────────

def _discover_fetch(conn, tmp_path, monkeypatch, text="N" * 6000):
    pipeline.discover(conn, FakeClient(_ITEMS))
    pdfs = {_URL1: b"%PDF", _URL2: b"%PDF"}
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: text)
    pipeline.fetch(conn, FakeClient(_ITEMS, pdfs=pdfs), pdf_dir=str(tmp_path))


def test_build(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch)
    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM gcaa_accidents")}
    a = acc["aifn-0007-2013"]
    assert a["country"] == "AE"
    assert a["event_date"] == "2020-01-01"
    assert a["report_type"] == "Final"
    assert a["source_url"] == _URL1
    assert acc["aifn-0009-2020"]["report_type"] == "Summary"


def test_build_floor_skips_short(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch, text="tiny")
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM gcaa_reports WHERE status='skipped'"
    ).fetchone()["c"] == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    _discover_fetch(conn, tmp_path, monkeypatch)
    pipeline.build(conn)
    conn.execute("UPDATE gcaa_reports SET status='parsed' WHERE status='built'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM gcaa_accidents").fetchone()["c"] == 2
