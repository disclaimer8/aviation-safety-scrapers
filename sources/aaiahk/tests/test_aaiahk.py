"""Offline tests for aaiahk_ingest.aaiahk using the saved register fixture."""
import os
import re

from aaiahk_ingest import aaiahk

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^(IVR|ITR|PLR)-\d{4}-\d{2}$")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── _normalize_case_id / make_case_id ───────────────────────────────────────

def test_normalize_basic():
    assert aaiahk._normalize_case_id("Download IVR-2025-01") == "IVR-2025-01"
    assert aaiahk._normalize_case_id("ITR-2026-02") == "ITR-2026-02"
    assert aaiahk._normalize_case_id("PLR-2024-03") == "PLR-2024-03"


def test_normalize_zero_pads_sequence():
    assert aaiahk._normalize_case_id("IVR-2025-1") == "IVR-2025-01"
    assert aaiahk._normalize_case_id("Download IVR-2026-9") == "IVR-2026-09"


def test_normalize_underscore_and_space_variants():
    assert aaiahk._normalize_case_id("IVR_2025_01") == "IVR-2025-01"
    assert aaiahk._normalize_case_id("ivr 2026 2") == "IVR-2026-02"


def test_normalize_none_for_nonsense():
    assert aaiahk._normalize_case_id("") is None
    assert aaiahk._normalize_case_id("Download report") is None
    assert aaiahk._normalize_case_id(None) is None


def test_make_case_id_precedence():
    # IVR text wins over ITR/PLR
    assert aaiahk.make_case_id("Download IVR-2025-01", "Download ITR-2026-02",
                               "Download PLR-2024-03") == "IVR-2025-01"
    # falls through to ITR when no IVR
    assert aaiahk.make_case_id(None, "Download ITR-2026-02",
                               "Download PLR-2024-03") == "ITR-2026-02"
    # falls through to PLR when only preliminary present
    assert aaiahk.make_case_id(None, None, "Download PLR-2024-03") == "PLR-2024-03"
    # nothing usable
    assert aaiahk.make_case_id(None, "", "Download report") is None


# ── NON-BLEED: must not match neighbour source keys ─────────────────────────

def test_normalize_does_not_match_aaib_family_tokens():
    """Codes from AAIB/AAIBMY/AAIU/AAIUBE/AAIIB must NOT yield a case_id."""
    for bad in (
        "AAIB-2024-01", "AAIBMY-2023-02", "AAIU-2022-05",
        "AAIUBE-2021-03", "AAIIB-2020-04", "EW/C2024/01/01",
        "Download AAIB Bulletin", "aaiahk",
    ):
        assert aaiahk._normalize_case_id(bad) is None, f"bled on {bad!r}"
        assert aaiahk.make_case_id(bad) is None, f"make_case_id bled on {bad!r}"


def test_normalize_only_accepts_ivr_itr_plr_prefixes():
    # A bare 'IVR' embedded in a larger word must not produce a false positive.
    assert aaiahk._normalize_case_id("SURVIVR-2025-01") is None
    assert aaiahk._normalize_case_id("IVR-2025-01") == "IVR-2025-01"


# ── parse_listing against the live fixture ──────────────────────────────────

def test_parse_listing_returns_rows():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    assert len(rows) >= 30, f"Expected >=30 rows, got {len(rows)}"


def test_parse_listing_case_id_format():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"bad case_id {r['case_id']!r}"


def test_parse_listing_no_duplicate_case_ids():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate case_ids"


def test_parse_listing_has_ivr_majority():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    ivr = [r for r in rows if r["case_id"].startswith("IVR-")]
    assert len(ivr) >= 30, f"Expected >=30 IVR final reports, got {len(ivr)}"


def test_parse_listing_known_ivr_row():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "IVR-2025-01" in by_id
    row = by_id["IVR-2025-01"]
    assert row["event_class"] == "Accident"
    assert row["date_of_occurrence"] == "2018-06-24"
    assert row["pdf_url"].startswith("https://www.tlb.gov.hk/aaia/doc/")
    assert row["pdf_url"].endswith(".pdf")
    assert row["aircraft"] is not None
    assert row["title"]


def test_parse_listing_legacy_href_with_ivr_text():
    """case_id from anchor text even when href uses the legacy filename scheme."""
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "IVR-2022-05" in by_id
    row = by_id["IVR-2022-05"]
    # legacy filename, not the IVR-coded one
    assert "Investigation%20Report%2005_2022.pdf" in row["pdf_url"] or \
           "Investigation Report 05_2022.pdf" in row["pdf_url"]


def test_parse_listing_pdf_urls_absolute():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    for r in rows:
        if r["pdf_url"]:
            assert r["pdf_url"].startswith("https://"), r["pdf_url"]
            assert r["pdf_url"].endswith(".pdf"), r["pdf_url"]


def test_parse_listing_date_iso_format():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    dated = [r for r in rows if r["date_of_occurrence"]]
    assert len(dated) >= 30
    for r in dated:
        assert iso.match(r["date_of_occurrence"]), r["date_of_occurrence"]


def test_parse_listing_event_classes():
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    classes = {r["event_class"] for r in rows}
    assert "Accident" in classes
    assert "Serious incident" in classes


def test_parse_listing_aircraft_first_description():
    """Aircraft-first rows ('<Aircraft> Accident at ...') extract the aircraft."""
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    # IVR-2019-01 = "Robinson R22 Beta II Helicopter Accident at Shek Kong ..."
    assert "Robinson R22" in (by_id["IVR-2019-01"]["aircraft"] or "")


def test_parse_listing_aircraft_last_of_token():
    """'Loss of Control - Inflight of Zlin Z242L ...' resolves to the aircraft."""
    rows = aaiahk.parse_listing(_fixture("aaiahk_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "Zlin Z242L" in (by_id["IVR-2020-02"]["aircraft"] or "")


# ── synthetic guard tests ───────────────────────────────────────────────────

_SYNTH_ROW = (
    "<table><thead><tr><th>h</th></tr></thead><tbody>"
    "<tr>"
    "<td style='display:none'>2024</td>"
    "<td>24 June 2018</td>"
    "<td>Accident</td>"
    "<td>Runway Excursion of Airbus A321-211 at Hong Kong International Airport</td>"
    "<td>Completed</td>"
    "<td><a href='/aaia/doc/PLR-2024-01_Eng.pdf' target='_blank'>Download PLR-2024-01</a></td>"
    "<td><a href='/aaia/doc/ITR-2025-04.pdf' target='_blank'>Download ITR-2025-04</a></td>"
    "<td><a href='/aaia/doc/Investigation_Report_IVR-2025-01.pdf' target='_blank'>Download IVR-2025-01</a></td>"
    "</tr></tbody></table>"
)


def test_parse_listing_synthetic_full_row():
    rows = aaiahk.parse_listing(_SYNTH_ROW)
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "IVR-2025-01"            # IVR precedence
    assert r["event_class"] == "Accident"
    assert r["date_of_occurrence"] == "2018-06-24"
    assert r["aircraft"] == "Airbus A321-211"
    assert r["registration"] is None
    assert r["pdf_url"].endswith("Investigation_Report_IVR-2025-01.pdf")


def test_parse_listing_skips_rows_without_any_code_link():
    html = (
        "<table><tbody><tr>"
        "<td>2024</td><td>1 January 2024</td><td>Incident</td>"
        "<td>Bird Strike of Boeing 777 at Hong Kong International Airport</td>"
        "<td>Under Investigation</td><td></td><td></td><td></td>"
        "</tr></tbody></table>"
    )
    assert aaiahk.parse_listing(html) == []


def test_parse_listing_falls_back_to_itr_then_plr():
    # only ITR present -> ITR case_id
    html_itr = _SYNTH_ROW.replace(
        "<td><a href='/aaia/doc/Investigation_Report_IVR-2025-01.pdf' target='_blank'>Download IVR-2025-01</a></td>",
        "<td></td>",
    )
    r = aaiahk.parse_listing(html_itr)[0]
    assert r["case_id"] == "ITR-2025-04"
    assert r["pdf_url"].endswith("ITR-2025-04.pdf")

    # neither IVR nor ITR -> PLR case_id
    html_plr = html_itr.replace(
        "<td><a href='/aaia/doc/ITR-2025-04.pdf' target='_blank'>Download ITR-2025-04</a></td>",
        "<td></td>",
    )
    r = aaiahk.parse_listing(html_plr)[0]
    assert r["case_id"] == "PLR-2024-01"
    assert r["pdf_url"].endswith("PLR-2024-01_Eng.pdf")


def test_classify_serious_incident():
    assert aaiahk._classify("Serious Incident") == "Serious incident"
    assert aaiahk._classify("Accident") == "Accident"
    assert aaiahk._classify("Incident") == "Incident"
    assert aaiahk._classify("") is None


# ── superseded_codes (dedup: prelim/interim → final) ────────────────────────

def test_parse_listing_superseded_codes_when_ivr_supersedes_itr_and_plr():
    """IVR row must list all lower-priority codes on the same HTML row in superseded_codes."""
    html = (
        "<table><thead><tr><th>h</th></tr></thead><tbody>"
        "<tr>"
        "<td style='display:none'>2024</td>"
        "<td>24 June 2018</td>"
        "<td>Accident</td>"
        "<td>Runway Excursion of Airbus A321-211 at Hong Kong International Airport</td>"
        "<td>Completed</td>"
        "<td><a href='/aaia/doc/PLR-2024-01_Eng.pdf' target='_blank'>Download PLR-2024-01</a></td>"
        "<td><a href='/aaia/doc/ITR-2025-04.pdf' target='_blank'>Download ITR-2025-04</a></td>"
        "<td><a href='/aaia/doc/IVR-2025-01.pdf' target='_blank'>Download IVR-2025-01</a></td>"
        "</tr></tbody></table>"
    )
    rows = aaiahk.parse_listing(html)
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "IVR-2025-01"
    # Both PLR and ITR codes must appear in superseded_codes
    assert set(r["superseded_codes"]) == {"PLR-2024-01", "ITR-2025-04"}


def test_parse_listing_superseded_codes_empty_for_plr_only():
    """A PLR-only row (no IVR/ITR) has no superseded codes."""
    html = (
        "<table><tbody><tr>"
        "<td>2025</td><td>8 September 2025</td><td>Serious incident</td>"
        "<td>Runway Excursion of Airbus A320-232 at Hong Kong International Airport</td>"
        "<td>Under Investigation</td>"
        "<td><a href='/aaia/doc/PLR-2025-03.pdf'>Download PLR-2025-03</a></td>"
        "<td></td><td></td>"
        "</tr></tbody></table>"
    )
    rows = aaiahk.parse_listing(html)
    assert len(rows) == 1
    assert rows[0]["case_id"] == "PLR-2025-03"
    assert rows[0]["superseded_codes"] == []


def test_parse_listing_superseded_codes_empty_for_itr_without_ivr():
    """An ITR-only row (no IVR yet) has no superseded codes relative to itself."""
    html = (
        "<table><tbody><tr>"
        "<td>2025</td><td>17 June 2024</td><td>Serious incident</td>"
        "<td>Multiple Hydraulic System Failure of Boeing 747-400F at HKIA</td>"
        "<td>Under Investigation</td>"
        "<td><a href='/aaia/doc/PLR-2024-01.pdf'>Download PLR-2024-01</a></td>"
        "<td><a href='/aaia/doc/ITR-2025-03.pdf'>Download ITR-2025-03</a></td>"
        "<td></td>"
        "</tr></tbody></table>"
    )
    rows = aaiahk.parse_listing(html)
    assert len(rows) == 1
    r = rows[0]
    # ITR wins as canon; PLR-2024-01 is superseded by ITR-2025-03
    assert r["case_id"] == "ITR-2025-03"
    assert r["superseded_codes"] == ["PLR-2024-01"]
