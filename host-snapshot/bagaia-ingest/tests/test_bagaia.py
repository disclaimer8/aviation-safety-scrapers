# tests/test_bagaia.py
"""Unit tests for bagaia module helpers."""
import pytest
from bagaia_ingest.bagaia import (
    _normalise_date,
    _make_case_id,
    dashboard_candidates,
    SEED_REPORTS,
    MEMBER_STATE_COVERED,
)


def test_normalise_date_valid():
    assert _normalise_date("29/07/2017") == "2017-07-29"
    assert _normalise_date("01/01/2000") == "2000-01-01"


def test_normalise_date_invalid():
    assert _normalise_date("") is None
    assert _normalise_date(None) is None
    assert _normalise_date("2017-07-29") is None  # wrong format


def test_make_case_id_basic():
    assert _make_case_id("UR-CKC", "2017-07-29") == "bagaia-ur-ckc-2017"
    assert _make_case_id("CN-RGW", "2024-08-17") == "bagaia-cn-rgw-2024"


def test_make_case_id_empty():
    result = _make_case_id("", "")
    assert result.startswith("bagaia")


def test_seed_reports_have_required_fields():
    required = {"case_id", "pdf_url", "registration", "aircraft", "date_of_occurrence", "country"}
    for r in SEED_REPORTS:
        missing = required - set(r)
        assert not missing, f"Seed row missing fields: {missing}"


def test_seed_case_ids_prefixed():
    for r in SEED_REPORTS:
        assert r["case_id"].startswith("bagaia-"), f"Non-prefixed: {r['case_id']}"


def test_ur_ckc_country_is_st():
    """São Tomé (ST) is the state of occurrence, not Nigeria."""
    row = next(r for r in SEED_REPORTS if r["case_id"] == "bagaia-ur-ckc-2017")
    assert row["country"] == "ST"


def test_dashboard_candidates_filters_member_states():
    raw = [
        {
            "country": "Nigeria", "date": "01/01/2020",
            "registration_number": "5N-XYZ", "aircraft_type": "B737",
            "aircraft_operator": "Air Nigeria", "occurence": "Accident",
            "report_link": "https://example.com/ng.pdf",
        },
        {
            "country": "Liberia", "date": "17/08/2024",
            "registration_number": "CN-RGW", "aircraft_type": "B738",
            "aircraft_operator": "Royal Air Maroc", "occurence": "Incident",
            "report_link": "https://drive.google.com/x",
        },
        {
            "country": "Liberia", "date": "01/06/2023",
            "registration_number": "", "aircraft_type": "Cessna",
            "aircraft_operator": "Unknown", "occurence": "Accident",
            "report_link": "",  # no link → skip
        },
    ]
    candidates = dashboard_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0]["registration"] == "CN-RGW"


def test_member_state_covered_set():
    """Verify major sources are covered."""
    for country in ("Nigeria", "Ghana", "Guinea", "Cape Verde"):
        assert country in MEMBER_STATE_COVERED
