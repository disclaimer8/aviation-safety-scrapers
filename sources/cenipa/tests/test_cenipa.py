# tests/test_cenipa.py
"""Tests for cenipa_ingest.cenipa pure parsers (no browser required)."""
import json
import re
from pathlib import Path

import pytest

from cenipa_ingest.cenipa import (
    last_page,
    make_pdf_choice,
    page_url,
    parse_listing,
    BASE,
)

FIXTURE = Path(__file__).parent / "fixtures" / "cenipa_listing.html"

CASE_ID_RE = re.compile(r"^(A|IG|IN)-\d{1,3}/CENIPA/\d{4}")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def fixture_html() -> str:
    raw = FIXTURE.read_text(encoding="utf-8").strip()
    # Fixture is stored as a JSON-encoded string (captured via JSON.stringify).
    if raw.startswith('"') or raw.startswith("'"):
        return json.loads(raw)
    return raw


# ─── parse_listing ───────────────────────────────────────────────────────────

def test_parse_listing_row_count(fixture_html):
    rows = parse_listing(fixture_html)
    # Fixture has 120 data rows (one <thead> header row excluded).
    assert len(rows) == 120, f"Expected 120 rows, got {len(rows)}"


def test_parse_listing_case_id_format(fixture_html):
    rows = parse_listing(fixture_html)
    # Every row must have a non-empty case_id.
    for row in rows:
        assert row["case_id"], f"Empty case_id in {row}"
    # At least one row must match the standard ^(A|IG|IN)-NNN/CENIPA/YYYY pattern.
    matching = [r for r in rows if CASE_ID_RE.match(r["case_id"])]
    assert len(matching) >= 100, f"Expected ≥100 standard case_ids, got {len(matching)}"


def test_parse_listing_first_row(fixture_html):
    rows = parse_listing(fixture_html)
    first = rows[0]
    assert first["case_id"] == "A-076/CENIPA/2023"
    assert first["date_of_occurrence"] == "2026-04-15"
    assert first["registration"] == "PUEPF"
    assert first["classificacao"] == "ACIDENTE"
    assert first["location"] == "BOA VISTA, RR"


def test_parse_listing_date_iso(fixture_html):
    rows = parse_listing(fixture_html)
    # All non-None dates must be ISO.
    for row in rows:
        d = row["date_of_occurrence"]
        if d is not None:
            assert ISO_DATE_RE.match(d), f"Bad date: {d!r} in {row['case_id']}"


def test_parse_listing_registration_nonempty(fixture_html):
    rows = parse_listing(fixture_html)
    for row in rows:
        assert row["registration"], f"Empty registration in {row['case_id']}"


def test_parse_listing_pdf_url_pt_absolute(fixture_html):
    rows = parse_listing(fixture_html)
    # All non-None PT urls must be absolute https and end with .pdf.
    for row in rows:
        url = row["pdf_url_pt"]
        if url is not None:
            assert url.startswith("https://"), f"Non-https pt url: {url}"
            assert url.endswith(".pdf"), f"Non-.pdf pt url: {url}"


def test_parse_listing_pdf_url_en_absolute(fixture_html):
    rows = parse_listing(fixture_html)
    for row in rows:
        url = row["pdf_url_en"]
        if url is not None:
            assert url.startswith("https://"), f"Non-https en url: {url}"
            assert url.endswith(".pdf"), f"Non-.pdf en url: {url}"


def test_parse_listing_en_pdf_count(fixture_html):
    rows = parse_listing(fixture_html)
    en_rows = [r for r in rows if r["pdf_url_en"]]
    # Fixture has 24 EN pdf links verified by inspection.
    assert len(en_rows) >= 20, f"Expected ≥20 EN pdfs, got {len(en_rows)}"


def test_parse_listing_known_en_row(fixture_html):
    """A-032/CENIPA/2025 has both PT and EN links in the fixture."""
    rows = parse_listing(fixture_html)
    row = next((r for r in rows if r["case_id"] == "A-032/CENIPA/2025"), None)
    assert row is not None, "A-032/CENIPA/2025 not found"
    assert row["pdf_url_pt"] is not None
    assert row["pdf_url_en"] is not None
    assert BASE in row["pdf_url_pt"]
    assert BASE in row["pdf_url_en"]


def test_parse_listing_pt_url_path(fixture_html):
    """PT urls must contain /rf/pt/."""
    rows = parse_listing(fixture_html)
    for row in rows:
        if row["pdf_url_pt"]:
            assert "/rf/pt/" in row["pdf_url_pt"], (
                f"PT url missing /rf/pt/: {row['pdf_url_pt']}"
            )


def test_parse_listing_en_url_path(fixture_html):
    """EN urls must contain /rf/en/."""
    rows = parse_listing(fixture_html)
    for row in rows:
        if row["pdf_url_en"]:
            assert "/rf/en/" in row["pdf_url_en"], (
                f"EN url missing /rf/en/: {row['pdf_url_en']}"
            )


# ─── last_page ───────────────────────────────────────────────────────────────

def test_last_page_fallback(fixture_html):
    """Fixture has no pagination links → fallback value must be >= 2."""
    result = last_page(fixture_html)
    assert isinstance(result, int)
    assert result >= 2


def test_last_page_with_pag_links():
    """Inject synthetic pagination HTML and verify extraction."""
    html = (
        '<a href="?&?&pag=1">1</a>'
        '<a href="?&?&pag=15">15</a>'
        '<a href="?&?&pag=7">7</a>'
    )
    assert last_page(html) == 15


def test_last_page_fallback_value():
    """When no pag= links present, fallback is exactly 33."""
    assert last_page("<html>no pagination here</html>") == 33


# ─── page_url ────────────────────────────────────────────────────────────────

def test_page_url():
    url = page_url(5)
    assert url.endswith("pag=5")
    assert BASE in url


# ─── make_pdf_choice ─────────────────────────────────────────────────────────

def test_make_pdf_choice_en_preferred():
    row = {"pdf_url_pt": "https://x.com/rf/pt/a.pdf", "pdf_url_en": "https://x.com/rf/en/a.pdf"}
    url, lang = make_pdf_choice(row)
    assert url == "https://x.com/rf/en/a.pdf"
    assert lang == "en"


def test_make_pdf_choice_pt_fallback():
    row = {"pdf_url_pt": "https://x.com/rf/pt/a.pdf", "pdf_url_en": None}
    url, lang = make_pdf_choice(row)
    assert url == "https://x.com/rf/pt/a.pdf"
    assert lang == "pt"


def test_make_pdf_choice_none():
    row = {"pdf_url_pt": None, "pdf_url_en": None}
    url, lang = make_pdf_choice(row)
    assert url is None
    assert lang == "pt"


# ─── date conversion ─────────────────────────────────────────────────────────

def test_date_conversion_direct():
    """Test date conversion via parse_listing on a synthetic row."""
    html = (
        "<table><tbody>"
        '<tr><td>A-001/CENIPA/2024</td><td>25/12/2024</td><td>PPABC</td>'
        "<td>ACIDENTE</td><td>[LOC-I]</td><td>SAO PAULO</td><td>SP</td>"
        '<td><a href="rf/pt/test.pdf"></a></td><td></td></tr>'
        "</tbody></table>"
    )
    rows = parse_listing(html)
    assert len(rows) == 1
    assert rows[0]["date_of_occurrence"] == "2024-12-25"


def test_date_conversion_invalid():
    """Invalid date leaves date_of_occurrence as the raw string or None."""
    html = (
        "<table><tbody>"
        '<tr><td>A-002/CENIPA/2024</td><td>not-a-date</td><td>PPXYZ</td>'
        "<td>ACIDENTE</td><td>[RE]</td><td>CITY</td><td>ST</td>"
        '<td><a href="rf/pt/x.pdf"></a></td><td></td></tr>'
        "</tbody></table>"
    )
    rows = parse_listing(html)
    assert len(rows) == 1
    # date_of_occurrence may be None or the raw string; must NOT be ISO
    d = rows[0]["date_of_occurrence"]
    if d is not None:
        assert not ISO_DATE_RE.match(d)


# ─── CenipaBrowser import safety ─────────────────────────────────────────────

def test_cenipa_browser_importable():
    """CenipaBrowser must be importable without launching a browser."""
    from cenipa_ingest.cenipa import CenipaBrowser  # noqa: F401
    assert CenipaBrowser is not None


def test_cenipa_browser_has_expected_api():
    from cenipa_ingest.cenipa import CenipaBrowser
    # Verify the public API surface exists.
    assert callable(getattr(CenipaBrowser, "start", None))
    assert callable(getattr(CenipaBrowser, "stop", None))
    assert callable(getattr(CenipaBrowser, "get_listing_html", None))
    assert callable(getattr(CenipaBrowser, "download_pdf", None))


def test_normalize_case_id_collapses_spaced_variants():
    # 'A - 013/CENIPA/2013' and 'A-013/CENIPA/2013' are the same report in
    # the listing; un-normalized they created dup rows and a UNIQUE slug
    # collision in the prod projection (2026-06-04).
    from cenipa_ingest.cenipa import _normalize_case_id

    assert _normalize_case_id("A - 013/CENIPA/2013") == "A-013/CENIPA/2013"
    assert _normalize_case_id("A-076 / CENIPA / 2023") == "A-076/CENIPA/2023"
    # already-clean ids and reg+date ids pass through untouched
    assert _normalize_case_id("A-076/CENIPA/2023") == "A-076/CENIPA/2023"
    assert _normalize_case_id("PP-AFS (29 JUN 1998)***") == "PP-AFS (29 JUN 1998)***"
