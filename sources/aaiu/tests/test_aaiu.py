from aaiu_ingest import aaiu


def test_parse_title_modern():
    m = aaiu.parse_title(
        "Final Report: Accident involving an Airbus A321-271NX (neo), "
        "registration TC-LTL, at Dublin Airport (EIDW), Ireland on "
        "18 October 2024. Report 2026-004"
    )
    assert m["case_id"] == "2026-004"
    assert m["report_kind"] == "Final"
    assert m["registration"] == "TC-LTL"
    assert "Airbus A321" in m["aircraft"]
    assert "Dublin Airport" in m["location"]
    assert m["event_date"] == "2024-10-18"


def test_parse_title_interim():
    m = aaiu.parse_title("Interim Statement: Accident involving a Cessna 172, "
                         "registration EI-ABC, at Weston on 2 May 2023. Report 2024-001")
    assert m["report_kind"] == "Interim"
    assert m["case_id"] == "2024-001"


def test_parse_title_legacy_caps_no_number():
    m = aaiu.parse_title("ACCIDENT Cessna FR 172K EI-CHV Glenforan 23 August 2002")
    assert m["case_id"] is None
    assert m["report_kind"] == "Final"
    assert m["registration"] == "EI-CHV"


def test_parse_title_live_fixture(rest_rows):
    parsed = [aaiu.parse_title(r["title"]["rendered"]) for r in rest_rows]
    with_num = sum(1 for p in parsed if p["case_id"])
    assert with_num >= len(parsed) * 0.5
    with_reg = sum(1 for p in parsed if p["registration"])
    assert with_reg >= len(parsed) * 0.6


def test_make_case_id():
    assert aaiu.make_case_id("2026-004", 99) == "2026-004"
    assert aaiu.make_case_id(None, 2896) == "wp-2896"
    assert aaiu.make_case_id("2026-004", 99, taken={"2026-004"}) == "2026-004-2"


def test_find_pdf_url():
    html = ('<a href="https://aaiu.ie/wp-content/uploads/2026/05/'
            'Report-2026-004.pdf">Download</a>'
            '<a href="https://other.site/x.pdf">no</a>')
    assert aaiu.find_pdf_url(html).endswith("Report-2026-004.pdf")
    assert aaiu.find_pdf_url("<p>none</p>") is None


def test_synopsis_text(rest_rows):
    txt = aaiu.synopsis_text(rest_rows[0]["content"]["rendered"])
    assert len(txt) > 300
    assert "<p>" not in txt


def test_find_pdf_url_legacy_path_spaces_encoded():
    html = ('<a href="https://aaiu.ie/sites/default/files/report-attachments/'
            'REPORT 2019-003.pdf">DL</a>')
    url = aaiu.find_pdf_url(html)
    assert url.endswith("REPORT%202019-003.pdf")
    assert " " not in url
