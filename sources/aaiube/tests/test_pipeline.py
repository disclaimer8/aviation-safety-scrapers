"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from aaiube_ingest import aaiube, db, pipeline

_LISTING = """
<h3 class='accordion__title'>Reports occurrences 2022</h3>
<table>
<thead><tr><th>Date</th><th>Type</th><th>Casualties</th><th>Location</th><th>Status</th></tr></thead>
<tbody>
<tr>
<td>12/09/2022</td><td>Grumman AA-5B</td><td>None</td><td>Brussels FIR</td>
<td><a href="/sites/default/files/documents/publications/2024/AAIU-2022-09-12-01-final.pdf">Final</a></td>
</tr>
</tbody>
</table>
<h3 class='accordion__title'>Reports occurrences 2009</h3>
<table>
<tbody>
<tr>
<td>05/03/2009</td><td>Cessna 172</td><td>1 fatal</td><td>Antwerp</td>
<td><a href="/sites/default/files/documents/publications/2009/2009_01.pdf">Final report</a></td>
</tr>
<tr>
<td>20/07/2009</td><td>Beechcraft</td><td>None</td><td>Liege</td>
<td><p>In progress</p></td>
</tr>
</tbody>
</table>
""".strip()

_MODERN_PDF = (
    "https://mobilit.belgium.be/sites/default/files/documents/publications/"
    "2024/AAIU-2022-09-12-01-final.pdf"
)
_LEGACY_PDF = (
    "https://mobilit.belgium.be/sites/default/files/documents/publications/"
    "2009/2009_01.pdf"
)


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, listing=_LISTING, pdfs=None):
        self.listing = listing
        self.pdfs = pdfs or {}
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url == aaiube.LISTING_URL:
            return FakeResp(text=self.listing)
        return FakeResp(content=self.pdfs.get(url, b""))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(aaiube, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    assert n == 2  # two PDF rows; the In-progress row is dropped
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiube_reports")}
    assert set(rows) == {"aaiu-2022-09-12-01", "be-2009-2009-01"}
    assert rows["aaiu-2022-09-12-01"]["date_of_occurrence"] == "2022-09-12"
    assert rows["aaiu-2022-09-12-01"]["aircraft"] == "Grumman AA-5B"
    assert rows["be-2009-2009-01"]["casualties"] == "1 fatal"


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(pdfs={_MODERN_PDF: b"%PDF a", _LEGACY_PDF: b"%PDF b"})
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiube_reports")}
    assert rows["aaiu-2022-09-12-01"]["source_tier"] == "pdf"
    assert len(rows["aaiu-2022-09-12-01"]["narrative_text"]) == 9000
    assert rows["aaiu-2022-09-12-01"]["proc_status"] == db.STATUS_PARSED


def test_fetch_scanned_pdf_below_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(pdfs={_MODERN_PDF: b"%PDF a", _LEGACY_PDF: b"%PDF b"})
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM aaiube_reports WHERE case_id='aaiu-2022-09-12-01'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert (row["narrative_text"] or "") == ""


def test_build(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(pdfs={_MODERN_PDF: b"%PDF a", _LEGACY_PDF: b"%PDF b"})
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 2
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM aaiube_accidents")}
    assert acc["aaiu-2022-09-12-01"]["country"] == "BE"
    assert acc["aaiu-2022-09-12-01"]["event_date"] == "2022-09-12"
    assert acc["aaiu-2022-09-12-01"]["source_url"] == _MODERN_PDF
    assert acc["aaiu-2022-09-12-01"]["site_slug"].startswith("crash-")


def test_build_skips_below_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(pdfs={_MODERN_PDF: b"%PDF a", _LEGACY_PDF: b"%PDF b"})
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM aaiube_accidents"
    ).fetchone()["c"] == 0
    statuses = {r["proc_status"] for r in conn.execute(
        "SELECT proc_status FROM aaiube_reports")}
    assert statuses == {db.STATUS_SKIPPED}


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    client = FakeClient(pdfs={_MODERN_PDF: b"%PDF a", _LEGACY_PDF: b"%PDF b"})
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE aaiube_reports SET proc_status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM aaiube_accidents"
    ).fetchone()["c"] == 2
