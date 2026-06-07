# tests/test_jtsb.py
"""Tests for jtsb_ingest.jtsb — parse_listing() against the live fixture."""

import re
from pathlib import Path

import pytest

from jtsb_ingest.jtsb import (
    BASE,
    INDEX_URL,
    DELAY,
    parse_listing,
    iter_index,
    download,
)

# ── Fixture ────────────────────────────────────────────────────────────────────

FIXTURE = Path(__file__).parent / "fixtures" / "jtsb_listing.html"

_ROWS_CACHE: list[dict] | None = None


def _rows() -> list[dict]:
    global _ROWS_CACHE
    if _ROWS_CACHE is None:
        html = FIXTURE.read_text(encoding="utf-8-sig")
        _ROWS_CACHE = parse_listing(html)
    return _ROWS_CACHE


# ── Module constants ───────────────────────────────────────────────────────────

def test_constants():
    assert BASE == "https://jtsb.mlit.go.jp"
    assert INDEX_URL == "https://jtsb.mlit.go.jp/airrep.html"
    assert isinstance(DELAY, (int, float)) and DELAY > 0


# ── Row count ─────────────────────────────────────────────────────────────────

def test_parse_listing_returns_over_300_rows():
    rows = _rows()
    assert len(rows) > 300, f"Expected >300 rows, got {len(rows)}"


def test_parse_listing_all_have_report_url():
    """Every returned row must have a non-None, non-empty report_url."""
    rows = _rows()
    missing = [r["case_id"] for r in rows if not r.get("report_url")]
    assert missing == [], f"Rows with no report_url: {missing}"


# ── case_id format ─────────────────────────────────────────────────────────────

_CASE_RE = re.compile(r"^A[AI]\d{4}-\d+(-\d+)?$")


def test_all_case_ids_match_pattern():
    rows = _rows()
    bad = [r["case_id"] for r in rows if not _CASE_RE.match(r["case_id"])]
    assert bad == [], f"Non-matching case_ids: {bad[:10]}"


def test_known_case_id_aa2025_8_1():
    rows = _rows()
    match = [r for r in rows if r["case_id"] == "AA2025-8-1"]
    assert len(match) == 1, "Expected exactly one row for AA2025-8-1"
    r = match[0]
    assert r["registration"] == "JA4098"
    assert "JA4098" in r["report_url"]
    assert r["report_url"].startswith("https://")
    assert r["report_url"].endswith(".pdf")
    assert r["report_type"] == "Accident"


def test_known_case_id_ai2025_8_2():
    rows = _rows()
    match = [r for r in rows if r["case_id"] == "AI2025-8-2"]
    assert len(match) == 1
    r = match[0]
    assert r["report_type"] == "Serious Incident"
    assert r["registration"] == "JA6686"
    assert r["category"] == "EXTERNAL LOAD RELATED OCCURRENCES"
    assert r["flight_phase"] == "MANEUVERING"
    assert r["operator"] == "Shin Nihon Helicopter Co., Ltd."


# ── report_url / pdf_url absolute https PDF ────────────────────────────────────

def test_report_url_is_absolute_https_pdf_under_eng_air_report():
    rows = _rows()
    for r in rows:
        url = r["report_url"]
        assert url.startswith("https://"), f"{r['case_id']}: not https: {url}"
        assert "eng-air_report/" in url, f"{r['case_id']}: not under eng-air_report: {url}"
        assert url.endswith(".pdf"), f"{r['case_id']}: not .pdf: {url}"


def test_pdf_url_equals_report_url():
    rows = _rows()
    for r in rows:
        assert r["pdf_url"] == r["report_url"], (
            f"{r['case_id']}: pdf_url != report_url"
        )


# ── jp_pdf_url ─────────────────────────────────────────────────────────────────

def test_jp_pdf_url_is_absolute_https():
    rows = _rows()
    for r in rows:
        url = r["jp_pdf_url"]
        assert url.startswith("https://"), f"{r['case_id']}: jp_pdf_url not https: {url}"
        assert "aircraft/rep-" in url, f"{r['case_id']}: jp_pdf_url unexpected: {url}"


# ── registration ───────────────────────────────────────────────────────────────

def test_most_registrations_start_with_ja():
    rows = _rows()
    ja_count = sum(1 for r in rows if (r["registration"] or "").startswith("JA"))
    # Majority are Japanese-registered (some rows have foreign/multi aircraft)
    assert ja_count > len(rows) * 0.7, (
        f"Expected >70% JA registrations, got {ja_count}/{len(rows)}"
    )


# ── date_of_occurrence ─────────────────────────────────────────────────────────

_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_date_of_occurrence_iso_or_none():
    rows = _rows()
    bad = [
        (r["case_id"], r["date_of_occurrence"])
        for r in rows
        if r["date_of_occurrence"] is not None
        and not _DATE_ISO_RE.match(r["date_of_occurrence"])
    ]
    assert bad == [], f"Non-ISO dates: {bad}"


def test_most_dates_present():
    rows = _rows()
    none_count = sum(1 for r in rows if r["date_of_occurrence"] is None)
    assert none_count < 10, f"Too many missing dates: {none_count}"


# ── report_type ────────────────────────────────────────────────────────────────

def test_report_type_values():
    rows = _rows()
    valid = {"Accident", "Serious Incident"}
    bad = [(r["case_id"], r["report_type"]) for r in rows if r["report_type"] not in valid]
    assert bad == [], f"Invalid report_types: {bad}"


def test_both_accident_and_serious_incident_present():
    rows = _rows()
    types = {r["report_type"] for r in rows}
    assert "Accident" in types
    assert "Serious Incident" in types


# ── Swapped-column rows (old records) ─────────────────────────────────────────

def test_swapped_column_rows_correctly_parsed():
    """Older rows (e.g. AI2020-7-1) have category/phase in cols 2-3, type in col 4."""
    rows = _rows()
    row = next((r for r in rows if r["case_id"] == "AI2020-7-1"), None)
    assert row is not None
    assert row["report_type"] == "Serious Incident"
    assert "SYSTEM" in (row["category"] or "")
    assert row["flight_phase"] == "EN ROUTE"


# ── No duplicates ──────────────────────────────────────────────────────────────

def test_no_duplicate_case_ids():
    rows = _rows()
    case_ids = [r["case_id"] for r in rows]
    assert len(case_ids) == len(set(case_ids)), "Duplicate case_ids found"


# ── iter_index integration (uses FakeClient) ──────────────────────────────────

def test_iter_index_calls_index_url_and_returns_rows(make_client):
    html = FIXTURE.read_bytes()
    client = make_client(
        {INDEX_URL: lambda url, params: type(
            "R", (), {
                "content": html,
                "text": html.decode("utf-8-sig"),
                "status_code": 200,
                "raise_for_status": lambda self=None: None,
            }
        )()}
    )
    result = iter_index(client)
    assert len(result) > 300
    assert client.calls[0][0] == INDEX_URL


def test_iter_index_returns_list_of_dicts(make_client):
    html = FIXTURE.read_bytes()

    class Resp:
        content = html
        text = html.decode("utf-8-sig")
        status_code = 200

        def raise_for_status(self):
            pass

    client = make_client({INDEX_URL: Resp()})
    result = iter_index(client)
    assert isinstance(result, list)
    assert all(isinstance(r, dict) for r in result)
    assert all("case_id" in r for r in result)


# ── download function ──────────────────────────────────────────────────────────

def test_download_writes_bytes(tmp_path, make_client):
    dest = str(tmp_path / "test.pdf")
    pdf_url = "https://jtsb.mlit.go.jp/eng-air_report/JA4098.pdf"
    payload = b"%PDF-1.4 test content"
    client = make_client({pdf_url: type(
        "R", (), {
            "content": payload,
            "status_code": 200,
            "raise_for_status": lambda self=None: None,
        }
    )()})
    download(client, pdf_url, dest)
    assert Path(dest).read_bytes() == payload


def test_download_raises_on_non_200(tmp_path, make_client):
    dest = str(tmp_path / "test.pdf")
    pdf_url = "https://jtsb.mlit.go.jp/eng-air_report/MISSING.pdf"
    client = make_client({pdf_url: type(
        "R", (), {
            "content": b"",
            "status_code": 404,
            "raise_for_status": lambda self=None: None,
        }
    )()})
    with pytest.raises(RuntimeError, match="HTTP 404"):
        download(client, pdf_url, dest)
