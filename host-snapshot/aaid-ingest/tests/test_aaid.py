# tests/test_aaid.py
"""Offline tests for aaid_ingest.aaid using the saved /final-reports fixture."""
import os
import re

from aaid_ingest import aaid

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
# Intrinsic case_id: <REG>-<YYYY-MM-DD>, uppercased & dash-normalised.
CASE_ID_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*-\d{4}-\d{2}-\d{2}$")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── make_case_id / _normalize_case_id ──────────────────────────────────────

def test_make_case_id_basic():
    assert aaid.make_case_id("5Y-LOL", "2024-12-07") == "5Y-LOL-2024-12-07"


def test_make_case_id_is_intrinsic_order_independent():
    """Same reg+date always yields the same id regardless of call order."""
    a = aaid.make_case_id("ZS-OYI", "2010-05-01")
    b = aaid.make_case_id("ZS-OYI", "2010-05-01")
    assert a == b == "ZS-OYI-2010-05-01"


def test_make_case_id_no_encounter_suffix():
    """No order/encounter suffix appended: id is exactly reg + '-' + iso date."""
    cid = aaid.make_case_id("5Y-AAB", "1999-01-02")
    assert cid == "5Y-AAB-1999-01-02"
    # The id must end with the ISO date and contain nothing after it.
    assert cid.endswith("1999-01-02")
    # Re-deriving from the same inputs yields an identical id (no counter state).
    assert aaid.make_case_id("5Y-AAB", "1999-01-02") == cid


def test_make_case_id_uppercases_and_normalises():
    assert aaid.make_case_id("f-ghje", "2001-03-04") == "F-GHJE-2001-03-04"
    assert aaid.make_case_id("5y lol", "2024-12-07") == "5Y-LOL-2024-12-07"


def test_make_case_id_date_only():
    assert aaid.make_case_id(None, "2024-12-07") == "2024-12-07"


def test_make_case_id_reg_only():
    assert aaid.make_case_id("5Y-LOL", None) == "5Y-LOL"


def test_make_case_id_empty_returns_none():
    assert aaid.make_case_id(None, None) is None
    assert aaid.make_case_id("", "") is None


def test_normalize_case_id_collapses_and_strips():
    assert aaid._normalize_case_id("--5Y//LOL  2024--12--07--") == "5Y-LOL-2024-12-07"
    assert aaid._normalize_case_id("") == ""


# ── parse_listing (live fixture) ───────────────────────────────────────────

def test_parse_listing_returns_rows():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    assert len(rows) >= 10, f"Expected >=10 rows, got {len(rows)}"


def test_parse_listing_case_id_format():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"Bad case_id: {r['case_id']!r}"


def test_parse_listing_known_row():
    """First row in the fixture: 5Y-LOL, occurrence 2024-12-07."""
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "5Y-LOL-2024-12-07" in by_id
    row = by_id["5Y-LOL-2024-12-07"]
    assert row["registration"] == "5Y-LOL"
    assert row["date_of_occurrence"] == "2024-12-07"
    assert row["event_class"] == "Accident"
    assert row["lang"] == "en"
    assert row["pdf_url"].startswith("https://aaid.transport.go.ke/")
    assert row["pdf_url"].endswith(".pdf")
    assert row["pdf_url_en"] == row["pdf_url"]
    assert row["pdf_url_es"] is None


def test_parse_listing_pdf_urls_absolute():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    for r in rows:
        assert r["pdf_url"].startswith("https://"), r["pdf_url"]
        assert r["pdf_url"].endswith(".pdf"), r["pdf_url"]


def test_parse_listing_href_taken_verbatim_with_space_encoding():
    """Filenames carry %20 (Final%20Report-...); href must be taken exactly."""
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    assert any("%20" in r["pdf_url"] for r in rows), "expected %20 in some href"


def test_parse_listing_dates_iso():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for r in rows:
        if r["date_of_occurrence"] is not None:
            assert iso.match(r["date_of_occurrence"]), r["date_of_occurrence"]


def test_parse_listing_no_duplicate_case_ids():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate case_ids"


def test_parse_listing_foreign_registration():
    """Foreign reg (e.g. ZS-OYI / F-GHJE) parsed when present in the fixture."""
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    regs = {r["registration"] for r in rows}
    assert any(not (rg.startswith("5Y") or rg.startswith("5H")) for rg in regs), \
        f"expected a foreign reg among {regs}"


def test_parse_listing_titles_nonempty():
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    for r in rows:
        assert r["title"], f"empty title for {r['case_id']}"


def test_parse_listing_skips_header_row():
    """The <thead> <th> row must NOT yield a phantom data row."""
    rows = aaid.parse_listing(_fixture("aaid_final_reports.html"))
    for r in rows:
        assert "Reference Number" not in (r["registration"] or "")


# ── synthetic guards ───────────────────────────────────────────────────────

_SYNTH = """
<tbody>
<tr>
  <td class="views-field views-field-counter">1</td>
  <td class="views-field views-field-field-reference-number">07/12/2024</td>
  <td class="views-field views-field-field-date-of-occurrence"><time datetime="2024-12-07T12:00:00Z">12-07-2024</time></td>
  <td class="views-field views-field-field-air-craft-reg-no">5Y-TST</td>
  <td class="views-field views-field-field-investigation-report-title"><span class="file"><a href="/sites/default/files/2026-06/Final%20Report-5Y-TST.pdf" type="application/pdf">Final Report-5Y-TST.pdf</a></span> <span>(1 MB)</span></td>
</tr>
<tr>
  <td>2</td><td>x</td><td>y</td><td>NO-PDF-HERE</td><td>no link</td>
</tr>
</tbody>
"""


def test_parse_listing_synthetic_complete():
    rows = aaid.parse_listing(_SYNTH)
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "5Y-TST-2024-12-07"
    assert r["registration"] == "5Y-TST"
    assert r["date_of_occurrence"] == "2024-12-07"
    assert r["pdf_url"] == "https://aaid.transport.go.ke/sites/default/files/2026-06/Final%20Report-5Y-TST.pdf"
    assert r["event_class"] == "Accident"
    assert r["lang"] == "en"


def test_parse_listing_skips_row_without_pdf():
    rows = aaid.parse_listing(_SYNTH)
    assert all(r["registration"] != "NO-PDF-HERE" for r in rows)


# ── SSL pinning surface ────────────────────────────────────────────────────

def test_ca_bundle_path_exists():
    assert os.path.exists(aaid.CA_BUNDLE), aaid.CA_BUNDLE


def test_make_ssl_context_pins_and_disables_only_time():
    ctx = aaid.make_ssl_context()
    # expiry check disabled
    assert ctx.verify_flags & aaid._X509_V_FLAG_NO_CHECK_TIME
    # but chain verification still required (not CERT_NONE)
    import ssl as _ssl
    assert ctx.verify_mode == _ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
