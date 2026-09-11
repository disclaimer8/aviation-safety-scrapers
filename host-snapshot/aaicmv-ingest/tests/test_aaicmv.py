# tests/test_aaicmv.py
import datetime
import pytest

from aaicmv_ingest import aaicmv
from tests._fx import load


@pytest.fixture(scope="module")
def index_html():
    return load("aaicmv_index.html")


@pytest.fixture(scope="module")
def listing(index_html):
    return aaicmv.parse_listing(index_html)


# ── discovery / parsing on the live fixture ───────────────────────────────────

def test_parse_listing_finds_all_reports(listing):
    # 45 report rows in the live fixture (header row has no PDF, excluded).
    assert len(listing) == 45


def test_every_row_has_pdf_url(listing):
    for r in listing:
        assert r["pdf_url"].startswith("https://caa.gov.mv/attachments/")
        assert r["pdf_url"].endswith(".pdf")


def test_case_ids_are_unique(listing):
    ids = [r["case_id"] for r in listing]
    assert len(ids) == len(set(ids))


def test_case_ids_intrinsic_format(listing):
    ids = {r["case_id"] for r in listing}
    # spot-check known intrinsic ids (NO encounter-order suffix)
    assert "MV-2024-03-final" in ids
    assert "MV-2024-03-prelim" in ids
    assert "MV-2001-01-report" in ids


def test_final_and_prelim_same_ref_distinct(listing):
    by_id = {r["case_id"]: r for r in listing}
    assert "MV-2021-02-final" in by_id
    assert "MV-2021-02-prelim" in by_id
    assert by_id["MV-2021-02-final"]["pdf_url"] != by_id["MV-2021-02-prelim"]["pdf_url"]


def test_date_parsed_iso(listing):
    by_id = {r["case_id"]: r for r in listing}
    assert by_id["MV-2024-03-final"]["date_of_occurrence"] == "2024-10-13"


def test_registration_extracted(listing):
    by_id = {r["case_id"]: r for r in listing}
    assert by_id["MV-2024-03-final"]["registration"] == "8Q-TBB"


def test_aircraft_extracted(listing):
    by_id = {r["case_id"]: r for r in listing}
    ac = by_id["MV-2024-03-final"]["aircraft"]
    assert ac and "DHC-6" in ac


def test_event_class_values(listing):
    classes = {r["event_class"] for r in listing}
    assert classes <= {"Accident", "Serious incident", "Incident"}


def test_dates_are_valid_iso_or_none(listing):
    for r in listing:
        d = r["date_of_occurrence"]
        if d is not None:
            datetime.date.fromisoformat(d)


# ── case_id unit behaviour ────────────────────────────────────────────────────

@pytest.mark.parametrize("ref,details,expected", [
    ("2024/03", "Final report on DHC-6", "MV-2024-03-final"),
    ("2024/03/P", "Preliminary report", "MV-2024-03-prelim"),
    ("P2020/04", "Some report", "MV-2020-04-prelim"),
    ("2020-01", "Final report", "MV-2020-01-final"),
    ("2022/01", "Investigation report", "MV-2022-01-report"),
])
def test_make_case_id(ref, details, expected):
    assert aaicmv.make_case_id(ref, details) == expected


def test_normalize_case_id_separators():
    assert aaicmv._normalize_case_id("2024/03") == "2024-03"
    assert aaicmv._normalize_case_id("2020-01") == "2020-01"
    assert aaicmv._normalize_case_id("P2020/04") == "2020-04"


def test_registration_foreign_mark():
    rows = aaicmv.parse_listing(
        "<table><tr>"
        "<td>Accident: <a href='/attachments/x.pdf'>2018-01</a></td>"
        "<td>07 July 2018</td>"
        "<td>Final report on the serious incident to Airbus A330-300 (9M-XXC)</td>"
        "</tr></table>"
    )
    assert rows[0]["registration"] == "9M-XXC"
    # col1 label ("Accident:") is the authoritative AICC filing class and wins
    # over wording in the details text.
    assert rows[0]["event_class"] == "Accident"
