"""Pipeline state-machine tests with a fake HTTP client (no network)."""
import pytest

from araib_ingest import araib, pipeline
from tests.fixtures.synopsis_samples import (
    SYNOPSIS_HL8088, SYNOPSIS_AAR2203, SYNOPSIS_AIR1906,
)

# DWN PDF urls the DTL fixtures point at.
_PDF_262906 = ("https://araib.molit.go.kr/LCMS/DWN.jsp?fold=/eaib0401/"
               "&fileName=HL8088+Preliminary+Report_English.pdf")
_PDF_247386 = ("https://araib.molit.go.kr/LCMS/DWN.jsp?fold=/eaib0401/"
               "&fileName=%28AIR1906%29_Aircraft_Serious_Incident_Report"
               "_29_October_2019.pdf")


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    """
    Serves: listing pages by &lcmspage=N (page 3 is empty → walk stops),
    DTL pages by idx, and PDFs by DWN url. Anything else → empty text.
    """
    def __init__(self, fixtures, pdfs=None, fail=None):
        self.fixtures = fixtures
        self.pdfs = pdfs or {}
        self.fail = fail or set()
        self.requested = []

    def get(self, url, params=None):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("connection reset by peer")
        for key, html in self.fixtures.items():
            if key in url:
                return FakeResp(text=html)
        if url in self.pdfs:
            return FakeResp(content=self.pdfs[url])
        return FakeResp(text="")  # empty listing page → page walk stops


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(araib, "DELAY", 0)
    monkeypatch.setattr(araib, "BACKOFF", 0)
    # The empty-page sentinel is shorter than TINY_STUB_BYTES; lower the bar so
    # the fake's small fixtures aren't treated as stubs during the walk.
    monkeypatch.setattr(araib, "TINY_STUB_BYTES", 5)


def _listing_client(listing1, listing2):
    return FakeClient({
        "lcmspage=1": listing1,
        "lcmspage=2": listing2,
        # page 3+ not present → FakeClient returns empty → walk stops.
    })


# ── discover: paginated walk, stop on no-new-rows ───────────────────────────


def test_discover_walks_until_empty(conn, listing1_html, listing2_html):
    client = _listing_client(listing1_html, listing2_html)
    n = pipeline.discover(conn, client)
    # 5 on page1 + 2 on page2 = 7; page3 empty → stop.
    assert n == 7
    idxs = {r["idx"] for r in conn.execute("SELECT idx FROM araib_reports")}
    assert "262906" in idxs and "247385" in idxs
    # page 3 WAS requested (to confirm no more rows), page 4 was not.
    assert any("lcmspage=3" in u for u in client.requested)
    assert not any("lcmspage=4" in u for u in client.requested)


def test_discover_stops_when_page_only_repeats(conn, listing1_html):
    # Page 2 == page 1 (paginator over-advertises) → no NEW idx → stop after p2.
    client = FakeClient({"lcmspage=1": listing1_html,
                         "lcmspage=2": listing1_html})
    n = pipeline.discover(conn, client)
    assert n == 5  # only page1's rows; page2 added nothing → walk stops
    assert not any("lcmspage=3" in u for u in client.requested)


def test_discover_idempotent(conn, listing1_html, listing2_html):
    assert pipeline.discover(
        conn, _listing_client(listing1_html, listing2_html)) == 7
    assert pipeline.discover(
        conn, _listing_client(listing1_html, listing2_html)) == 0


def test_discover_rows_status_new(conn, listing1_html, listing2_html):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    statuses = {r["status"] for r in conn.execute(
        "SELECT status FROM araib_reports")}
    assert statuses == {"new"}


# ── fetch: DTL → PDF → synopsis (case_id, reg, event_date) ──────────────────


def _full_client(listing1, listing2, dtl262, dtl247):
    return FakeClient(
        fixtures={
            "lcmspage=1": listing1,
            "lcmspage=2": listing2,
            "idx=262906": dtl262,
            "idx=247386": dtl247,
        },
        pdfs={_PDF_262906: b"%PDF jeju", _PDF_247386: b"%PDF air1906"},
    )


def test_fetch_assigns_case_id_and_synopsis(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)

    texts = {_PDF_262906: SYNOPSIS_HL8088, _PDF_247386: SYNOPSIS_AIR1906}

    def _extract(path):
        # path is pdfs/{idx}.pdf — map idx back to its synopsis.
        if "262906" in str(path):
            return SYNOPSIS_HL8088
        if "247386" in str(path):
            return SYNOPSIS_AIR1906
        return "N" * 4000  # other rows: generic long body

    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))

    jeju = conn.execute(
        "SELECT * FROM araib_reports WHERE idx='262906'").fetchone()
    assert jeju["case_id"] == "aar2404"           # canonical from synopsis
    assert jeju["registration"] == "HL8088"
    assert jeju["event_date"] == "2024-12-29"     # occurrence, NOT 2025-01-31
    assert jeju["status"] == "parsed"
    assert jeju["source_tier"] == "pdf"
    assert jeju["pdf_url"] == _PDF_262906

    air = conn.execute(
        "SELECT * FROM araib_reports WHERE idx='247386'").fetchone()
    assert air["case_id"] == "air1906"
    assert air["registration"] == "HL8071"
    assert air["event_date"] == "2019-10-29"


def test_fetch_case_id_fallback_when_no_case_number(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)
    # No case number in any text → fallback 'araib-{idx}'.
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 4000)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))
    row = conn.execute(
        "SELECT case_id FROM araib_reports WHERE idx='262906'").fetchone()
    assert row["case_id"] == "araib-262906"


def test_fetch_scanned_tier(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "")
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))
    row = conn.execute(
        "SELECT source_tier, status FROM araib_reports WHERE idx='262906'"
    ).fetchone()
    assert row["source_tier"] == "scanned"
    assert row["status"] == "parsed"


def test_fetch_dtl_failure_stays_new(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)
    client.fail.add(araib.dtl_url("262906"))
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "N" * 4000)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))
    row = conn.execute(
        "SELECT status FROM araib_reports WHERE idx='262906'").fetchone()
    assert row["status"] == "new"  # retried next cycle


# ── build: floor + accidents ────────────────────────────────────────────────


def test_build_floor_and_accidents(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)

    def _extract(path):
        if "262906" in str(path):
            return SYNOPSIS_HL8088   # long → built
        if "247386" in str(path):
            return "tiny"            # below floor → skipped
        return "tiny"
    monkeypatch.setattr(pipeline.pdf, "extract_text", _extract)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))

    built = pipeline.build(conn)
    assert built == 1
    acc = {r["case_id"]: r for r in conn.execute(
        "SELECT * FROM araib_accidents")}
    assert set(acc) == {"aar2404"}
    a = acc["aar2404"]
    assert a["country"] == "KR"
    assert a["lang"] == "en"
    assert a["event_date"] == "2024-12-29"
    assert a["registration"] == "HL8088"
    assert a["report_type"] == "Preliminary"
    assert a["source_url"].endswith("idx=262906") or "DTL.jsp" in a["source_url"]
    # The below-floor row was tiered scanned/skipped, not built.
    assert conn.execute(
        "SELECT status FROM araib_reports WHERE idx='247386'"
    ).fetchone()["status"] == "skipped"


def test_build_event_date_falls_back_to_publish(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)
    # Long body with NO parseable occurrence date → build uses publish_date.
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: "Accident Number: AAR2404\n" + "N" * 4000)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))
    pipeline.build(conn)
    a = conn.execute(
        "SELECT event_date FROM araib_accidents WHERE case_id='aar2404'"
    ).fetchone()
    # synopsis had no date → falls back to the listing publish date 2025-01-31.
    assert a["event_date"] == "2025-01-31"


def test_build_idempotent(
        conn, listing1_html, listing2_html, dtl_262906_html, dtl_247386_html,
        monkeypatch):
    pipeline.discover(conn, _listing_client(listing1_html, listing2_html))
    client = _full_client(listing1_html, listing2_html,
                          dtl_262906_html, dtl_247386_html)
    monkeypatch.setattr(pipeline.pdf, "extract_text",
                        lambda p: SYNOPSIS_HL8088 if "262906" in str(p)
                        else "N" * 4000)
    pipeline.fetch(conn, client, pdf_dir=str(__import__("tempfile").mkdtemp()))
    pipeline.build(conn)
    n_acc = conn.execute(
        "SELECT COUNT(*) c FROM araib_accidents").fetchone()["c"]
    # Re-run build over the same parsed rows → INSERT OR REPLACE, no duplicates.
    conn.execute("UPDATE araib_reports SET status='parsed'")
    conn.commit()
    pipeline.build(conn)
    assert conn.execute(
        "SELECT COUNT(*) c FROM araib_accidents").fetchone()["c"] == n_acc
