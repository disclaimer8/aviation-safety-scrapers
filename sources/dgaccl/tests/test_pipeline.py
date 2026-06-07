"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from dgaccl_ingest import dgaccl, pipeline

# A tiny 2024 page: one Final (2044), one Preliminar-only (2048), and a case
# (2071) published twice across rows — 12-meses then 36-meses — to exercise the
# cross-row staged-preference merge. Plus a site-chrome PDF that must be dropped.
_Y2024 = (
    '<table class="table"><tbody>'
    '<tr><td><strong>Suceso</strong></td><td><strong>Fecha</strong></td>'
    '<td><strong>Tipo aeronave</strong></td><td><strong>Lugar</strong></td>'
    '<td><strong>Estado</strong></td></tr>'
    '<tr><td>2044</td><td>15 ENE 2024</td><td>THRUSH S2R</td><td>TALCA</td>'
    '<td><a href="https://www.dgac.gob.cl/wp-content/uploads/2026/01/'
    'Informe-final-2044-24.pdf">pdf</a></td></tr>'
    '<tr><td>2048</td><td>15 FEB 2024</td><td>CA 10</td><td>VILLARRICA</td>'
    '<td><a href="https://www.dgac.gob.cl/wp-content/uploads/2026/03/'
    'Informe-preliminar-24-meses-2048-24.pdf">pdf</a></td></tr>'
    '<tr><td>2071</td><td>10 MAR 2024</td><td>R44</td><td>SUR</td>'
    '<td><a href="https://www.dgac.gob.cl/wp-content/uploads/2025/05/'
    'Informe-preliminar-12-meses-2071-24.pdf">pdf</a></td></tr>'
    '<tr><td>2071</td><td>10 MAR 2024</td><td>R44</td><td>SUR</td>'
    '<td><a href="https://www.dgac.gob.cl/wp-content/uploads/2026/05/'
    'Informe-preliminar-36-meses-2071-24.pdf">pdf</a></td></tr>'
    '<tr><td>9999</td><td>no fecha</td><td>chrome</td><td>x</td>'
    '<td><a href="https://www.dgac.gob.cl/wp-content/uploads/2018/01/'
    'Presupuesto_2018.pdf">chrome</a></td></tr>'
    '</tbody></table>'
)

_PDF_2044 = ("https://www.dgac.gob.cl/wp-content/uploads/2026/01/"
             "Informe-final-2044-24.pdf")
_PDF_2048 = ("https://www.dgac.gob.cl/wp-content/uploads/2026/03/"
             "Informe-preliminar-24-meses-2048-24.pdf")
_PDF_2071 = ("https://www.dgac.gob.cl/wp-content/uploads/2026/05/"
             "Informe-preliminar-36-meses-2071-24.pdf")


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages=None, pdfs=None, fail_pdf=None):
        # Only the 2024 page is non-empty; the other 6 hardcoded years are blank.
        self.pages = pages if pages is not None else {
            "https://www.dgac.gob.cl/informes-2024/": _Y2024,
        }
        self.pdfs = pdfs if pdfs is not None else {
            _PDF_2044: b"%PDF 2044", _PDF_2048: b"%PDF 2048",
            _PDF_2071: b"%PDF 2071"}
        self.fail_pdf = fail_pdf or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.pages:
            return FakeResp(text=self.pages[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")  # blank year pages


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(dgaccl, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    # 2044 (final) + 2048 (prelim) + 2071 (merged) = 3; chrome 9999 dropped.
    assert n == 3
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM dgaccl_reports")}
    assert set(rows) == {"2044-24", "2048-24", "2071-24"}
    assert rows["2044-24"]["report_kind"] == "Final"
    assert rows["2044-24"]["date_of_occurrence"] == "2024-01-15"
    assert rows["2044-24"]["year"] == "2024"
    assert "THRUSH" in rows["2044-24"]["aircraft"].upper()
    assert rows["2048-24"]["report_kind"] == "Preliminar"


def test_discover_walks_all_seven_years(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    # All 7 hardcoded year pages are GET'd (6 blank + 2024).
    assert set(client.requested) == set(dgaccl.YEAR_PAGES)


def test_discover_staged_preference_picks_latest(conn):
    pipeline.discover(conn, FakeClient())
    row = conn.execute(
        "SELECT pdf_url FROM dgaccl_reports WHERE case_id='2071-24'").fetchone()
    # 36-meses must win over the earlier 12-meses stage for case 2071.
    assert "36-meses" in row["pdf_url"]


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier_and_registration(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Relato " + "N" * 9000 + " matrícula CC-PHQ del piloto.")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM dgaccl_reports")}
    assert rows["2044-24"]["source_tier"] == "pdf"
    assert rows["2044-24"]["status"] == "parsed"
    assert rows["2044-24"]["registration"] == "CC-PHQ"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM dgaccl_reports WHERE case_id='2044-24'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF_2044})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM dgaccl_reports WHERE case_id='2044-24'").fetchone()
    assert row["status"] == "new"  # retried next cycle


def test_build_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(p):
        return "N" * 9000 if "2044-24" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM dgaccl_accidents")}
    assert set(acc) == {"2044-24"}
    assert acc["2044-24"]["country"] == "CL"
    assert acc["2044-24"]["report_type"] == "Final"
    assert acc["2044-24"]["event_date"] == "2024-01-15"
    assert acc["2044-24"]["source_url"].endswith("/informes-2024/")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM dgaccl_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE dgaccl_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM dgaccl_accidents").fetchone()["c"] == 3
