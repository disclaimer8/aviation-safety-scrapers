from aibdk_ingest import aibdk


def test_parse_case_ids(yearpage_html):
    ids = aibdk.parse_case_ids(yearpage_html)
    assert len(ids) >= 15
    assert all(len(i) == 8 and i[4] == "-" for i in ids)
    assert "2025-362" in ids  # 0510- prefix stripped where present


def test_candidate_years_order():
    years = aibdk.candidate_years("2018-401")
    assert years[:3] == ["2018", "2019", "2017"]
    assert "2015" in years  # the verified /2015/2018-401 trap is reachable
    assert len(set(years)) == len(years)


def test_detail_url():
    assert aibdk.detail_url("2023", "2023-506") == (
        "https://en.havarikommissionen.dk/investigation-results/"
        "search-aviation/2023/2023-506")


def test_parse_case(case_html):
    d = aibdk.parse_case(case_html)
    assert d["title"].startswith("Accident to OY-NMX")
    assert d["registration"] == "OY-NMX"
    assert d["event_date"] == "2023-10-08"  # 8-10-2023 D-M-Y
    assert "Kalundborg" in d["location"]
    assert d["pdf_url"] and d["pdf_url"].startswith(
        "https://cdn.havarikommissionen.dk/") and d["pdf_url"].endswith(".pdf")


def test_parse_case_no_pdf():
    d = aibdk.parse_case("<title>Accident to OY-XXX in Y on 1-2-2020</title>")
    assert d["pdf_url"] is None
    assert d["registration"] == "OY-XXX"
