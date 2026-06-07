# tests/test_pipeline.py
"""
Pipeline tests for CENIPA discover / fetch / parse / build.

All browser/PDF calls are monkeypatched — no live Playwright or pdftotext needed.
"""
import os
import tempfile

import pytest

from cenipa_ingest import cenipa, db
from cenipa_ingest.pipeline import discover, fetch, parse, build


# ── helpers ───────────────────────────────────────────────────────────────────

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── fixtures ──────────────────────────────────────────────────────────────────

# A minimal listing table with two data rows.
# Row 1 has both PT and EN pdf links; row 2 has only PT.
_LISTING_HTML_PAGE1 = """\
<html><body>
<table>
<thead><tr><th>NÚMERO</th><th>DATA</th><th>MATRÍCULA</th><th>CLASSE</th>
  <th>TIPO</th><th>CIDADE</th><th>ESTADO</th><th>RELATÓRIO</th></tr></thead>
<tbody>
<tr>
  <td>A-076/CENIPA/2023</td>
  <td>15/04/2026</td>
  <td>PUEPF</td>
  <td>ACIDENTE</td>
  <td>[LOC-I]</td>
  <td>BOA VISTA</td>
  <td>RR</td>
  <td>
    <a href="rf/pt/A-076-2023-pt.pdf">PT</a>
    <a href="rf/en/A-076-2023-en.pdf">EN</a>
  </td>
</tr>
<tr>
  <td>A-077/CENIPA/2023</td>
  <td>20/04/2026</td>
  <td>PRATB</td>
  <td>INCIDENTE GRAVE</td>
  <td>[FUEL]</td>
  <td>MANAUS</td>
  <td>AM</td>
  <td>
    <a href="rf/pt/A-077-2023-pt.pdf">PT</a>
  </td>
</tr>
</tbody>
</table>
</body></html>
"""

# An empty page (no data rows) — simulates going past the last real page.
_LISTING_HTML_EMPTY = "<html><body><table><thead></thead><tbody></tbody></table></body></html>"

_PDF_BYTES = b"%PDF-1.4 fake content"


class FakeBrowser:
    """Minimal stand-in for CenipaBrowser (no Playwright)."""

    def __init__(self, pages=None, pdf_bytes=_PDF_BYTES):
        # pages: dict mapping page number to HTML string.
        # Defaults: page 1 → fixture; anything else → empty.
        self._pages = pages or {1: _LISTING_HTML_PAGE1}
        self._pdf_bytes = pdf_bytes
        self.download_calls = []

    # Context-manager support (mirrors CenipaBrowser)
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get_listing_html(self, n: int) -> str:
        return self._pages.get(n, _LISTING_HTML_EMPTY)

    def download_pdf(self, url: str, dest: str) -> None:
        self.download_calls.append((url, dest))
        with open(dest, "wb") as fh:
            fh.write(self._pdf_bytes)


# ── discover ──────────────────────────────────────────────────────────────────

def test_discover_inserts_rows(monkeypatch):
    """discover with page 1 fixture → 2 rows inserted with correct metadata."""
    # Force max_pages=2; page 2 will be empty → stop-on-empty kicks in after page 1
    monkeypatch.setattr(cenipa, "DELAY", 0)

    conn = _conn()
    browser = FakeBrowser()
    n = discover(conn, browser, max_pages=2)

    assert n == 2, f"Expected 2 inserted, got {n}"

    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row is not None
    assert row["date_of_occurrence"] == "2026-04-15"
    assert row["registration"] == "PUEPF"
    assert row["classificacao"] == "ACIDENTE"
    assert row["location"] == "BOA VISTA, RR"
    assert row["status"] == db.STATUS_NEW

    # EN-preferred pdf_url
    assert row["pdf_url"] is not None
    assert "/rf/en/" in row["pdf_url"]
    assert row["lang"] == "en"

    # Both PT and EN stored
    assert row["pdf_url_en"] is not None
    assert row["pdf_url_pt"] is not None


def test_discover_pt_fallback(monkeypatch):
    """Row with only PT pdf → pdf_url points to PT, lang='pt'."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    conn = _conn()
    browser = FakeBrowser()
    discover(conn, browser, max_pages=1)

    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-077/CENIPA/2023'"
    ).fetchone()
    assert row is not None
    assert row["lang"] == "pt"
    assert "/rf/pt/" in row["pdf_url"]
    assert row["pdf_url_en"] is None


def test_discover_stops_on_empty_page(monkeypatch):
    """discover stops early when a page returns 0 rows."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    # max_pages=5 but page 2 is empty → should stop after page 1
    conn = _conn()
    browser = FakeBrowser()
    n = discover(conn, browser, max_pages=5)

    assert n == 2  # only page 1 rows inserted


def test_discover_idempotent(monkeypatch):
    """Running discover twice inserts rows only once."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    conn = _conn()
    browser = FakeBrowser()
    n1 = discover(conn, browser, max_pages=1)
    n2 = discover(conn, browser, max_pages=1)

    assert n1 == 2
    assert n2 == 0
    total = conn.execute("SELECT COUNT(*) FROM cenipa_reports").fetchone()[0]
    assert total == 2


def test_discover_multi_page(monkeypatch):
    """discover walks multiple pages until empty."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    # Provide two real pages; page 3 is missing (→ empty → stop)
    pages = {
        1: _LISTING_HTML_PAGE1,
        2: _LISTING_HTML_PAGE1.replace(
            "A-076/CENIPA/2023", "A-100/CENIPA/2023"
        ).replace(
            "A-077/CENIPA/2023", "A-101/CENIPA/2023"
        ),
    }
    conn = _conn()
    browser = FakeBrowser(pages=pages)
    n = discover(conn, browser, max_pages=3)

    assert n == 4  # 2 rows × 2 real pages


# ── fetch ─────────────────────────────────────────────────────────────────────

PDF_URL_EN = (
    "https://sistema.cenipa.fab.mil.br"
    "/cenipa/paginas/relatorios/rf/en/A-076-2023-en.pdf"
)


def _insert_new(conn, case_id="A-076/CENIPA/2023", pdf_url=PDF_URL_EN):
    conn.execute(
        "INSERT INTO cenipa_reports "
        "(case_id, pdf_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, pdf_url, db.STATUS_NEW, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_fetch_downloads_pdf_and_advances(monkeypatch, tmp_path):
    monkeypatch.setattr(cenipa, "DELAY", 0)

    conn = _conn()
    _insert_new(conn)

    browser = FakeBrowser()
    n = fetch(conn, browser, str(tmp_path))

    assert n == 1
    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is not None
    assert row["pdf_path"].endswith(".pdf")
    # Slash in case_id must be sanitised
    assert "/" not in os.path.basename(row["pdf_path"])


def test_fetch_no_pdf_url_advances_with_none(monkeypatch, tmp_path):
    """A row without pdf_url → status='fetched', pdf_path=None."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    conn = _conn()
    _insert_new(conn, pdf_url=None)

    browser = FakeBrowser()
    n = fetch(conn, browser, str(tmp_path))

    assert n == 1
    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row["status"] == db.STATUS_FETCHED
    assert row["pdf_path"] is None


def test_fetch_download_failure_stays_new(monkeypatch, tmp_path):
    """If download_pdf raises, the row stays at 'new' for retry."""
    monkeypatch.setattr(cenipa, "DELAY", 0)

    class FailBrowser(FakeBrowser):
        def download_pdf(self, url, dest):
            raise RuntimeError("connection refused")

    conn = _conn()
    _insert_new(conn)

    fetch(conn, FailBrowser(), str(tmp_path))

    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row["status"] == db.STATUS_NEW


# ── parse ─────────────────────────────────────────────────────────────────────

def _insert_fetched(conn, case_id, pdf_path=None):
    conn.execute(
        "INSERT INTO cenipa_reports "
        "(case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (case_id, db.STATUS_FETCHED, pdf_path, db.now_ms(), db.now_ms()),
    )
    conn.commit()


def test_parse_long_text_tier_pdf(monkeypatch, tmp_path):
    """PDF with >=600 chars → source_tier='pdf', status='parsed'."""
    pdf_path = str(tmp_path / "report.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF fake")

    long_text = "A" * 700
    monkeypatch.setattr("cenipa_ingest.pipeline.extract_text", lambda p: long_text)

    conn = _conn()
    _insert_fetched(conn, "A-076/CENIPA/2023", pdf_path=pdf_path)

    n = parse(conn)
    assert n == 1
    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "pdf"
    assert row["narrative_text"] == long_text


def test_parse_tiny_text_scanned(monkeypatch, tmp_path):
    """PDF returning <600 chars → source_tier='scanned'."""
    pdf_path = str(tmp_path / "scanned.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF scanned")

    monkeypatch.setattr("cenipa_ingest.pipeline.extract_text", lambda p: "tiny")

    conn = _conn()
    _insert_fetched(conn, "A-SCAN/2001", pdf_path=pdf_path)

    parse(conn)
    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-SCAN/2001'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "scanned"


def test_parse_no_pdf_path_tier_none(monkeypatch):
    """Row with no pdf_path → source_tier='none', narrative=''."""
    monkeypatch.setattr("cenipa_ingest.pipeline.extract_text", lambda p: "should not be called")

    conn = _conn()
    _insert_fetched(conn, "A-NOPDF/2020", pdf_path=None)

    parse(conn)
    row = conn.execute(
        "SELECT * FROM cenipa_reports WHERE case_id='A-NOPDF/2020'"
    ).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert row["source_tier"] == "none"
    assert (row["narrative_text"] or "") == ""


# ── build ─────────────────────────────────────────────────────────────────────

def _insert_parsed(
    conn, case_id, narrative, source_tier="pdf",
    aircraft="EMB-110", registration="PUEPF",
    location="BOA VISTA, RR", date_of_occurrence="2026-04-15",
    classificacao="ACIDENTE",
    pdf_url=PDF_URL_EN, report_url=None,
):
    conn.execute(
        "INSERT INTO cenipa_reports "
        "(case_id, pdf_url, report_url, status, narrative_text, source_tier, "
        " aircraft, registration, location, date_of_occurrence, classificacao, "
        " discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            case_id, pdf_url, report_url, db.STATUS_PARSED,
            narrative, source_tier, aircraft, registration,
            location, date_of_occurrence, classificacao,
            db.now_ms(), db.now_ms(),
        ),
    )
    conn.commit()


def test_build_pdf_tier_creates_accident():
    """pdf tier + narrative ≥ 80 → cenipa_accidents row; country='BR'."""
    conn = _conn()
    narrative = "X" * 200
    _insert_parsed(conn, "A-076/CENIPA/2023", narrative, source_tier="pdf")

    n = build(conn)
    assert n == 1

    acc = conn.execute(
        "SELECT * FROM cenipa_accidents WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert acc is not None
    assert acc["country"] == "BR"
    assert acc["event_date"] == "2026-04-15"
    assert acc["narrative_text"] == narrative
    assert acc["source_url"] == PDF_URL_EN
    assert acc["report_type"] == "ACIDENTE"

    rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-076/CENIPA/2023'"
    ).fetchone()
    assert rep["status"] == db.STATUS_BUILT


def test_build_report_type_is_classificacao():
    """report_type in cenipa_accidents matches classificacao from cenipa_reports."""
    conn = _conn()
    _insert_parsed(
        conn, "A-001/CENIPA/2024", "Y" * 200,
        source_tier="pdf", classificacao="INCIDENTE GRAVE",
    )
    build(conn)
    acc = conn.execute(
        "SELECT report_type FROM cenipa_accidents WHERE case_id='A-001/CENIPA/2024'"
    ).fetchone()
    assert acc["report_type"] == "INCIDENTE GRAVE"


def test_build_scanned_tier_skipped():
    """scanned tier → status='skipped', NOT in cenipa_accidents."""
    conn = _conn()
    _insert_parsed(conn, "A-SCAN/2023", narrative="", source_tier="scanned")

    n = build(conn)
    assert n == 0

    acc = conn.execute(
        "SELECT * FROM cenipa_accidents WHERE case_id='A-SCAN/2023'"
    ).fetchone()
    assert acc is None

    rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-SCAN/2023'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_none_tier_skipped():
    """none tier (no PDF) → status='skipped'."""
    conn = _conn()
    _insert_parsed(conn, "A-NONE/2020", narrative="", source_tier="none")

    n = build(conn)
    assert n == 0

    rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-NONE/2020'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_pdf_short_narrative_skipped():
    """pdf tier but narrative < 80 chars → skipped."""
    conn = _conn()
    _insert_parsed(conn, "A-SHORT/2022", narrative="too short", source_tier="pdf")

    n = build(conn)
    assert n == 0

    rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-SHORT/2022'"
    ).fetchone()
    assert rep["status"] == db.STATUS_SKIPPED


def test_build_source_url_falls_back_to_report_url():
    """When pdf_url is None, source_url in cenipa_accidents = report_url."""
    conn = _conn()
    report_url = "https://sistema.cenipa.fab.mil.br/cenipa/paginas/relatorios/A-076"
    _insert_parsed(
        conn, "A-NOPDF/2023", "Z" * 200, source_tier="pdf",
        pdf_url=None, report_url=report_url,
    )

    build(conn)
    acc = conn.execute(
        "SELECT source_url FROM cenipa_accidents WHERE case_id='A-NOPDF/2023'"
    ).fetchone()
    assert acc["source_url"] == report_url


def test_build_mixed_rows():
    """One pdf + one scanned → 1 built, 1 skipped."""
    conn = _conn()
    _insert_parsed(
        conn, "A-PDF/2024", "Z" * 200, source_tier="pdf",
        registration="PUEPF", location="BOA VISTA, RR", aircraft="C172",
    )
    _insert_parsed(
        conn, "A-SCANNED/2024", "", source_tier="scanned",
        registration="PRATB", location="MANAUS, AM", aircraft="B738",
    )

    n = build(conn)
    assert n == 1

    built_count = conn.execute("SELECT COUNT(*) FROM cenipa_accidents").fetchone()[0]
    assert built_count == 1

    pdf_rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-PDF/2024'"
    ).fetchone()
    scan_rep = conn.execute(
        "SELECT status FROM cenipa_reports WHERE case_id='A-SCANNED/2024'"
    ).fetchone()
    assert pdf_rep["status"] == db.STATUS_BUILT
    assert scan_rep["status"] == db.STATUS_SKIPPED
