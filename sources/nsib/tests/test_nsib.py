from nsib_ingest import nsib


def test_iter_page_urls_includes_page1_and_numbered(air_reports_html):
    urls = nsib.iter_page_urls(air_reports_html)
    assert urls[0] == nsib.INDEX_URL
    # numbered pages present and ordered
    assert any("page=2" in u for u in urls)
    assert all(u.startswith("https://nsib.gov.ng/") for u in urls)
    # ordered ascending by page number
    nums = [int(u.split("page=")[1]) for u in urls if "page=" in u]
    assert nums == sorted(nums)


def test_parse_listing_returns_data_rows(air_reports_html):
    rows = nsib.parse_listing(air_reports_html)
    # 4 data rows in the fixture (header row excluded)
    assert len(rows) == 4
    statuses = {r["status"] for r in rows}
    assert "Preliminary Report" in statuses
    assert "Interim Statement" in statuses
    assert "Final Report" in statuses


def test_parse_listing_prelim_row_has_pdf(air_reports_html):
    rows = nsib.parse_listing(air_reports_html)
    prelim = next(r for r in rows if r["status"] == "Preliminary Report")
    assert prelim["pdf_url"] and prelim["pdf_url"].endswith(".pdf")
    assert prelim["date"] == "2025-09-14"
    assert prelim["reg_cell"] == "5N-BQQ"
    assert prelim["post_url"].startswith("https://nsib.gov.ng/")


def test_parse_listing_final_row_has_no_pdf(air_reports_html):
    rows = nsib.parse_listing(air_reports_html)
    final = next(r for r in rows if r["status"] == "Final Report")
    assert final["pdf_url"] is None  # finals carry no downloadable report


def test_parse_listing_pdf_href_trimmed_at_first_pdf(air_reports_html):
    """The site doubles the filename (…18.pdf…18.pdf); href must stop at .pdf."""
    rows = nsib.parse_listing(air_reports_html)
    for r in rows:
        if r["pdf_url"]:
            assert r["pdf_url"].count(".pdf") == 1
