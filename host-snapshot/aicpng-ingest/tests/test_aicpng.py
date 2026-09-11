"""Offline tests for aicpng_ingest.aicpng using saved live HTML fixtures."""
import os
import re
import pytest

from aicpng_ingest import aicpng

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^AIC-\d{2}-\d{3,4}$")


def _fx(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── case_id normalisation (the SPACE trap) ────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("AIC 26-1003", "AIC-26-1003"),
    ("AIC  26 - 1003", "AIC-26-1003"),
    ("aic 13-1002", "AIC-13-1002"),
    ("AIC 23/1001", "AIC-23-1001"),
    ("  AIC 24-2002  ", "AIC-24-2002"),
    ("AIC-25-1003", "AIC-25-1003"),
])
def test_normalize_case_id(raw, expected):
    assert aicpng._normalize_case_id(raw) == expected


def test_normalize_case_id_empty():
    assert aicpng._normalize_case_id("") is None
    assert aicpng._normalize_case_id(None) is None


def test_make_case_id_from_noisy_text():
    assert aicpng.make_case_id("View report AIC 26-1003 (Accident)") == "AIC-26-1003"
    assert aicpng.make_case_id("no reference here") is None


def test_make_site_slug():
    assert aicpng.make_site_slug("AIC-26-1003") == "aic-26-1003"
    assert aicpng.make_site_slug("") == ""


# ── listing parser ────────────────────────────────────────────────────────────

def test_parse_listing_row_count():
    rows = aicpng.parse_listing(_fx("aicpng_listing_page0.html"))
    assert len(rows) == 20, f"expected 20 data rows, got {len(rows)} (BigPipe trap?)"


def test_parse_listing_no_zero_rows():
    rows = aicpng.parse_listing(_fx("aicpng_listing_page0.html"))
    assert rows, "0 rows parsed — BigPipe / header-row regression"


def test_parse_listing_case_ids_canonical():
    rows = aicpng.parse_listing(_fx("aicpng_listing_page0.html"))
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"bad case_id {r['case_id']!r}"


def test_parse_listing_fields():
    rows = aicpng.parse_listing(_fx("aicpng_listing_page0.html"))
    by_id = {r["case_id"]: r for r in rows}
    r = by_id["AIC-26-1003"]
    assert r["event_class"] == "Accident"
    assert r["date_of_occurrence"] == "2026-04-17"
    assert r["registration"] == "H4-HSA"
    assert "Solomon Islands" in r["location"]
    assert r["status"] == "Ongoing"
    assert r["report_url"].endswith("/investigation/1096")


def test_parse_listing_all_have_detail_url():
    rows = aicpng.parse_listing(_fx("aicpng_listing_page0.html"))
    for r in rows:
        assert r["report_url"] and r["report_url"].startswith("https://aic.gov.pg/investigation/")


def test_parse_date():
    assert aicpng._parse_date("17 January 2013") == "2013-01-17"
    assert aicpng._parse_date("1 May 2024") == "2024-05-01"
    assert aicpng._parse_date("garbage") is None


# ── detail parser (PDF discovery) ─────────────────────────────────────────────

def test_parse_detail_final():
    found = aicpng.parse_detail(_fx("aicpng_detail_final.html"))
    assert found, "no PDF found on final-report detail page"
    rep_type, url = found[0]
    assert "final" in (rep_type or "").lower()
    assert url.startswith("https://aic.gov.pg/sites/default/files/")
    assert url.lower().endswith(".pdf")


def test_best_pdf_final():
    rep_type, url = aicpng.best_pdf(_fx("aicpng_detail_final.html"))
    assert url and url.endswith(".pdf")


def test_best_pdf_preliminary():
    rep_type, url = aicpng.best_pdf(_fx("aicpng_detail_preliminary.html"))
    assert url and ".pdf" in url.lower()
    assert "preliminary" in (rep_type or "").lower()


def test_parse_detail_no_dupes():
    found = aicpng.parse_detail(_fx("aicpng_detail_final.html"))
    urls = [u for _, u in found]
    assert len(urls) == len(set(urls))


# ── BigPipe defence ───────────────────────────────────────────────────────────

def test_expand_big_pipe_noop_on_flat():
    flat = _fx("aicpng_listing_page0.html")
    assert aicpng._expand_big_pipe(flat) == flat


def test_expand_big_pipe_injects_payload():
    payload = '[{"command":"insert","data":"<tr><td headers=\\"view-nothing-table-column\\"><a href=\\"/investigation/9\\"><span>AIC 99-1001</span></a></td><td>Accident</td><td>1 May 2099</td><td>P2-XXX</td><td>Nowhere</td><td>Closed</td><td>x</td></tr>"}]'
    html = '<html><body><table><tbody></tbody></table>' \
           f'<script type="application/vnd.drupal-ajax" data-big-pipe-event="start">{payload}</script></body></html>'
    rows = aicpng.parse_listing(html)
    assert any(r["case_id"] == "AIC-99-1001" for r in rows), "BigPipe payload not parsed"
