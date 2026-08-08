# tests/test_carol.py
"""Unit tests for carol.py helpers (no network calls)."""
from ntsbcarol_ingest import carol


def test_case_id_lowercase():
    assert carol.case_id("DCA17IA148") == "ntsbcarol-dca17ia148"
    assert carol.case_id("ANC20MA010") == "ntsbcarol-anc20ma010"
    assert carol.case_id("DCA95MA001") == "ntsbcarol-dca95ma001"


def test_pdf_url_format():
    assert carol.pdf_url("AIR2205") == "https://www.ntsb.gov/investigations/AccidentReports/Reports/AIR2205.pdf"
    assert carol.pdf_url(None) is None
    assert carol.pdf_url("") is None


def test_parse_event_date():
    assert carol.parse_event_date("2019-12-26T17:57:00Z") == "2019-12-26"
    assert carol.parse_event_date("") is None
    assert carol.parse_event_date(None) is None
    assert carol.parse_event_date("1994-10-31T00:00:00Z") == "1994-10-31"


def test_parse_row_fields():
    raw = {
        "Fields": [
            {"FieldName": "NtsbNo", "Values": ["DCA17IA148"]},
            {"FieldName": "Mkey", "Values": ["95528"]},
            {"FieldName": "EventDate", "Values": ["2017-07-07T23:56:00Z"]},
            {"FieldName": "City", "Values": ["San Francisco"]},
            {"FieldName": "State", "Values": ["California"]},
            {"FieldName": "Country", "Values": ["United States"]},
            {"FieldName": "N#", "Values": ["C-FKCK"]},
            {"FieldName": "VehicleMake", "Values": ["Airbus"]},
            {"FieldName": "VehicleModel", "Values": ["A320-211"]},
            {"FieldName": "HighestInjuryLevel", "Values": ["None"]},
            {"FieldName": "InjuryOnboardCount", "Values": []},
            {"FieldName": "CompletionStatus", "Values": ["Completed"]},
            {"FieldName": "ReportNo", "Values": ["AIR1801"]},
            {"FieldName": "EV_ID", "Values": []},
        ],
        "EntryId": "abc123",
    }
    row = carol._parse_row(raw)
    assert row["ntsb_no"] == "DCA17IA148"
    assert row["mkey"] == "95528"
    assert row["city"] == "San Francisco"
    assert row["registration"] == "C-FKCK"
    assert row["make"] == "Airbus"
    assert row["model"] == "A320-211"
    assert row["ev_id"] == ""
    assert row["report_no"] == "AIR1801"
    assert row["fatalities_str"] == ""


def test_parse_row_empty_values():
    raw = {
        "Fields": [
            {"FieldName": "NtsbNo", "Values": ["ANC20MA010"]},
            {"FieldName": "EV_ID", "Values": []},
        ],
        "EntryId": "",
    }
    row = carol._parse_row(raw)
    assert row["ev_id"] == ""
    assert row["city"] == ""


def test_board_prefixes():
    assert "DCA" in carol.BOARD_PREFIXES
    assert "ANC" in carol.BOARD_PREFIXES
    assert len(carol.BOARD_PREFIXES) == 4
