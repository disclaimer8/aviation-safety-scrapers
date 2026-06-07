from nsia_ingest import nsia


def test_parse_listing(listing_html):
    rows = nsia.parse_listing(listing_html)
    assert len(rows) == 30
    by_id = {r["case_id"]: r for r in rows}
    r = by_id["2024-02"]
    assert r["detail_url"] == "https://nsia.no/Aviation/Aviation/Published-reports/2024-02"
    assert "PA-28" in r["aircraft"]
    assert r["registration"] == "LN-NAS"
    assert r["event_date"] == "2021-05-11"
    assert "Voss" in r["location"]
    assert r["lang"] == "Norwegian"


def test_parse_listing_langs_mixed(listing_html):
    rows = nsia.parse_listing(listing_html)
    langs = {r["lang"] for r in rows}
    assert "English" in langs and "Norwegian" in langs


def test_parse_listing_empty():
    assert nsia.parse_listing("<html><table></table></html>") == []


def test_canonical_case_id():
    assert nsia.canonical_case_id("2024/02") == "2024-02"
    assert nsia.canonical_case_id(" 1998/11 ") == "1998-11"
    assert nsia.canonical_case_id("") is None


def test_parse_detail(detail_html):
    d = nsia.parse_detail(detail_html)
    assert d["operator"] == "Private"
    assert d["report_kind"] == "Accident"
    assert d["title"] and "Voss" in d["title"]


def test_pdf_url():
    assert nsia.pdf_url(
        "https://nsia.no/Aviation/Aviation/Published-reports/2024-02"
    ) == ("https://nsia.no/Aviation/Aviation/Published-reports/2024-02"
          "?pid=SHT-Report-ReportFile&attach=1")
