"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pathlib

import pytest

from pkbwl_ingest import pkbwl, pipeline

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fx(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# A tiny 2-slug listing page: one report with RK+EN (2022-2456), one PL-only
# resolution (2015-1098). Page 2 is 404 (past-the-end clean stop).
_LISTING_P1 = (
    '<ul>'
    '<li><a href="/raporty/2022-2456/">2022-2456</a></li>'
    '<li><a href="/raporty/2015-1098/">2015-1098</a></li>'
    '<li><a href="/raporty/2026-0040/">2026-0040</a></li>'  # no-PDF → skipped
    '</ul>'
)

_DETAILS = {
    "https://pkbwl.gov.pl/raporty/2022-2456/": _fx("detail_2022-2456.html"),
    "https://pkbwl.gov.pl/raporty/2015-1098/": _fx("detail_2015-1098.html"),
    "https://pkbwl.gov.pl/raporty/2026-0040/": _fx("detail_2026-0040_nopdf.html"),
}

_RK_EN = "https://pkbwl.gov.pl/wp-content/uploads/2023/01/2022-2456_RK_ENG.pdf"
_RK_PL = "https://pkbwl.gov.pl/wp-content/uploads/2023/01/2022-2456_RK.pdf"
_U_PL = "https://pkbwl.gov.pl/wp-content/uploads/2024/04/2015_1098_U.pdf"


class FakeResp:
    def __init__(self, text="", content=b"", status_code=200):
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, listings=None, details=None, pdfs=None, fail_pdf=None):
        # Page 1 served; every other listing page 404s (clean stop).
        self.listings = listings if listings is not None else {
            pkbwl.listing_url(1): _LISTING_P1,
        }
        self.details = details if details is not None else dict(_DETAILS)
        self.pdfs = pdfs if pdfs is not None else {
            _RK_EN: b"%PDF EN", _RK_PL: b"%PDF PL", _U_PL: b"%PDF U"}
        self.fail_pdf = fail_pdf or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.listings:
            return FakeResp(text=self.listings[url])
        if url.startswith(pkbwl.LISTING) and "page/" in url:
            return FakeResp(status_code=404)  # past-the-end
        if url in self.details:
            return FakeResp(text=self.details[url])
        if url in self.fail_pdf:
            raise RuntimeError("pdf 502")
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(pkbwl, "DELAY", 0)


# ── discover ───────────────────────────────────────────────────────────────


def test_discover_inserts_reports_with_pdfs(conn):
    n = pipeline.discover(conn, FakeClient())
    # 2022-2456 (RK+EN) + 2015-1098 (U PL) = 2; the no-PDF 2026-0040 dropped.
    assert n == 2
    rows = {r["case_id"]: r for r in conn.execute("SELECT * FROM pkbwl_reports")}
    assert set(rows) == {"2022-2456", "2015-1098"}
    assert rows["2022-2456"]["report_type"] == "Final"
    assert rows["2022-2456"]["lang"] == "en"
    assert rows["2022-2456"]["registration"] == "SP-MMB"
    assert rows["2022-2456"]["date_of_occurrence"] == "2022-05-23"
    assert rows["2015-1098"]["report_type"] == "Resolution"
    assert rows["2015-1098"]["lang"] == "pl"


def test_discover_stops_on_404(conn):
    client = FakeClient()
    pipeline.discover(conn, client)
    # Page 1 listing requested, page 2 requested (got 404), then stop.
    listing_hits = [u for u in client.requested if "/raporty/" in u
                    and "uploads" not in u and "raporty/page" in u]
    assert pkbwl.listing_url(2) in client.requested
    assert pkbwl.listing_url(3) not in client.requested


def test_discover_idempotent(conn):
    assert pipeline.discover(conn, FakeClient()) == 2
    assert pipeline.discover(conn, FakeClient()) == 0


def test_discover_skips_nopdf_report(conn):
    pipeline.discover(conn, FakeClient())
    assert conn.execute(
        "SELECT 1 FROM pkbwl_reports WHERE case_id='2026-0040'"
    ).fetchone() is None


# ── fetch: tiers, EN→PL spaced-letter fallback ─────────────────────────────


def test_fetch_pdf_tier_clean_en(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: "Clean English narrative. " * 50)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM pkbwl_reports WHERE case_id='2022-2456'").fetchone()
    assert row["source_tier"] == "pdf"
    assert row["status"] == "parsed"
    assert row["lang"] == "en"


def test_fetch_spaced_en_falls_back_to_pl(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    # EN file extracts as letter-spaced garbage; PL file is clean.
    spaced = "P R E L I M I N A R Y " * 60

    def _extract(path):
        # The same on-disk path is reused; distinguish by which URL was last
        # downloaded via a side channel on the client. Simpler: key on a flag.
        return _extract.queue.pop(0)
    _extract.queue = [spaced, "Czysty polski tekst raportu. " * 60]
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM pkbwl_reports WHERE case_id='2022-2456'").fetchone()
    assert row["status"] == "parsed"
    assert row["source_tier"] == "pdf"
    assert row["lang"] == "pl"                 # switched to Polish
    assert row["pdf_url"].endswith("2022-2456_RK.pdf")


def test_fetch_scanned_tier(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM pkbwl_reports WHERE case_id='2015-1098'").fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_failure_stays_new(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    client = FakeClient(fail_pdf={_RK_EN, _U_PL})
    pipeline.fetch(conn, client, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT * FROM pkbwl_reports WHERE case_id='2022-2456'").fetchone()
    assert row["status"] == "new"              # retried next cycle


# ── build floor + country + idempotency ────────────────────────────────────


def test_build_floor(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())

    def _extract(p):
        return "N" * 9000 if "2022-2456" in str(p) else "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    assert pipeline.build(conn) == 1
    acc = {r["case_id"]: r for r in conn.execute("SELECT * FROM pkbwl_accidents")}
    assert set(acc) == {"2022-2456"}
    assert acc["2022-2456"]["country"] == "PL"
    assert acc["2022-2456"]["report_type"] == "Final"
    assert acc["2022-2456"]["event_date"] == "2022-05-23"
    assert acc["2022-2456"]["registration"] == "SP-MMB"
    assert acc["2022-2456"]["source_url"].endswith("/raporty/2022-2456/")
    skipped = conn.execute(
        "SELECT COUNT(*) c FROM pkbwl_reports WHERE status='skipped'"
    ).fetchone()["c"]
    assert skipped == 1                        # 2015-1098 fell below floor


def test_build_idempotent(conn, tmp_path, monkeypatch):
    pipeline.discover(conn, FakeClient())
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 9000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    pipeline.build(conn)
    conn.execute("UPDATE pkbwl_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM pkbwl_accidents").fetchone()["c"] == 2
