"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from ueim_ingest import ueim, pipeline

_U = "https://ulasimemniyeti.uab.gov.tr/uploads/pages/hava-araci/"


def _row(date, reg, loc, kind, slug):
    return (
        f'<tr><td aria-label="KAZA TARİHİ">{date}</td>'
        f'<td aria-label="TESCİL İŞARETİ">{reg}</td>'
        f'<td aria-label="KAZA YERİ">{loc}</td>'
        f'<td aria-label="KAZA TÜRÜ">{kind}</td>'
        f'<td aria-label="RAPOR TARİHİ">'
        f'<a href="{_U}{slug}.pdf">indir</a></td></tr>'
    )


# TR page: a final (tc-ajc), a foreign preliminary (9h-dfs), and a no-PDF row.
_TR = (
    '<table><thead><tr><th>KAZA TARİHİ</th></tr></thead><tbody>'
    + _row("21.05.2022", "TC-AJC", "İSTANBUL HEZARFEN", "KAZA",
           "tc-ajc-hava-araci-kazasi-nihai-raporuu")
    + _row("01.06.2024", "9H-DFS", "ANTALYA", "OLAY", "9h-dfs-on-rapor")
    + '<tr><td aria-label="KAZA TARİHİ">01.01.2020</td>'
      '<td aria-label="TESCİL İŞARETİ">TC-XXX</td>'
      '<td aria-label="KAZA YERİ">ANKARA</td>'
      '<td aria-label="KAZA TÜRÜ">OLAY</td>'
      '<td aria-label="RAPOR TARİHİ">Raporu bekleniyor</td></tr>'
    + '</tbody></table>'
)
# EN page: re-lists tc-ajc (dupe, must be ignored) + adds a NEW en-only PDF.
_EN = (
    '<table><tbody>'
    + _row("21.05.2022", "TC-AJC", "ISTANBUL HEZARFEN", "ACCIDENT",
           "tc-ajc-hava-araci-kazasi-nihai-raporuu")
    + _row("10.10.2019", "TC-ENX", "IZMIR", "INCIDENT", "tc-enx-nihai-rapor")
    + '</tbody></table>'
)

_PDF_AJC = f"{_U}tc-ajc-hava-araci-kazasi-nihai-raporuu.pdf"
_PDF_DFS = f"{_U}9h-dfs-on-rapor.pdf"
_PDF_ENX = f"{_U}tc-enx-nihai-rapor.pdf"


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, pages=None, pdfs=None, fail_pdf=None):
        self.pages = pages if pages is not None else {
            ueim.TR_LISTING: _TR,
            ueim.EN_LISTING: _EN,
        }
        self.pdfs = pdfs if pdfs is not None else {
            _PDF_AJC: b"%PDF ajc", _PDF_DFS: b"%PDF dfs", _PDF_ENX: b"%PDF enx",
        }
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
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ueim, "DELAY", 0)


def test_discover(conn):
    n = pipeline.discover(conn, FakeClient())
    # tc-ajc + 9h-dfs (TR) + tc-enx (EN add-only) = 3; TC-XXX no-pdf dropped,
    # EN re-listing of tc-ajc deduped.
    assert n == 3
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM ueim_reports")}
    assert set(rows) == {
        "tc-ajc-hava-araci-kazasi-nihai-raporuu",
        "9h-dfs-on-rapor",
        "tc-enx-nihai-rapor",
    }
    ajc = rows["tc-ajc-hava-araci-kazasi-nihai-raporuu"]
    assert ajc["report_type"] == "final"
    assert ajc["registration"] == "TC-AJC"
    assert ajc["date_of_occurrence"] == "2022-05-21"
    assert ajc["lang"] == "tr"
    assert rows["9h-dfs-on-rapor"]["report_type"] == "preliminary"
    # The EN-only PDF is tagged lang='en'.
    assert rows["tc-enx-nihai-rapor"]["lang"] == "en"


def test_discover_walks_tr_then_en(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    assert client.requested[0] == ueim.TR_LISTING
    assert ueim.EN_LISTING in client.requested


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 3
    assert pipeline.discover(conn, FakeClient()) == 0


def test_fetch_pdf_tier_and_registration(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "Kaza raporu " + "N" * 9000 + " TC-AJC tescilli.")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM ueim_reports")}
    ajc = rows["tc-ajc-hava-araci-kazasi-nihai-raporuu"]
    assert ajc["source_tier"] == "pdf"
    assert ajc["status"] == "parsed"
    assert ajc["registration"] == "TC-AJC"


def test_fetch_registration_recovered_from_text(conn, tmp_path, monkeypatch):
    # Wipe the listing-derived reg so text recovery is exercised.
    pipeline.discover(conn, FakeClient())
    conn.execute("UPDATE ueim_reports SET registration=NULL")
    conn.commit()
    monkeypatch.setattr(
        pipeline.pdf, "extract_text",
        lambda p: "N" * 9000 + " ... TC-AJC ...")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT registration FROM ueim_reports "
        "WHERE case_id='tc-ajc-hava-araci-kazasi-nihai-raporuu'").fetchone()
    assert row["registration"] == "TC-AJC"


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM ueim_reports "
        "WHERE case_id='tc-ajc-hava-araci-kazasi-nihai-raporuu'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_PDF_AJC})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM ueim_reports "
        "WHERE case_id='tc-ajc-hava-araci-kazasi-nihai-raporuu'").fetchone()
    assert row["status"] == "new"  # retried next cycle


def test_build_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(p):
        return "N" * 9000 if "tc-ajc" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM ueim_accidents")}
    assert set(acc) == {"tc-ajc-hava-araci-kazasi-nihai-raporuu"}
    a = acc["tc-ajc-hava-araci-kazasi-nihai-raporuu"]
    assert a["country"] == "TR"
    assert a["report_type"] == "final"
    assert a["event_date"] == "2022-05-21"
    assert a["lang"] == "tr"
    assert a["source_url"].endswith(".pdf")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM ueim_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 2


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE ueim_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM ueim_accidents").fetchone()["c"] == 3
