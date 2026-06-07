from sacaa_ingest import sacaa


# ── listing: main page (latest 4-col + big 7-col tables) ─────────────────────

def test_parse_main_listing(main_html):
    rows = sacaa.parse_listing(main_html)
    assert len(rows) > 15  # 10 latest + 11-row slice of the big table
    by_name = {r["name"]: r for r in rows}
    # 7-col row
    r = by_name["9690"]
    assert r["registration"] == "ZU-RLK"
    assert r["aircraft"] == "RAF 2000 GTX FI"
    assert "Limpopo" in r["location"]
    assert r["event_date"] == "2018-03-02"  # "2 March" + Year column 2018
    assert r["report_kind"] == "Final"
    assert r["pdf_url"].startswith(
        "https://caasanwebsitestorage.blob.core.windows.net/")


def test_parse_latest_table_4col(main_html):
    rows = sacaa.parse_listing(main_html)
    by_name = {r["name"]: r for r in rows}
    r = by_name["1524"]
    assert r["registration"] == "ZU-FNS and ZS-GFP"
    assert r["event_date"] == "2026-01-27"  # full date in the cell
    assert r["aircraft"] is None  # latest table has no type column


def test_preliminary_title_row_detected(main_html):
    rows = sacaa.parse_listing(main_html)
    prelim = [r for r in rows if r["report_kind"] == "Preliminary"]
    assert any("ZU-RDP" in (r["name"] or "") for r in prelim)


# ── listing: archive page (category rows) ─────────────────────────────────────

def test_parse_archive_foreign_rows(archive_html):
    rows = sacaa.parse_listing(archive_html)
    foreign = [r for r in rows if r["report_kind"] == "Foreign"]
    assert foreign
    f = foreign[0]
    # category rows carry the FULL date in the Date column
    assert f["event_date"] == "2004-01-25"
    assert f["registration"] == "ZS-SAX and I-DEIB"


def test_parse_archive_numeric_rows(archive_html):
    rows = sacaa.parse_listing(archive_html)
    by_name = {r["name"]: r for r in rows}
    r = by_name["6950"]
    assert r["event_date"] == "1998-09-23"
    assert r["registration"] == "ZS-LRX"


def test_pdf_url_spaces_encoded(archive_html):
    rows = sacaa.parse_listing(archive_html)
    spaced = [r for r in rows if "%20" in r["pdf_url"]]
    assert spaced  # 'ZS-SAX and I-DEIB.pdf' etc.
    assert not any(" " in r["pdf_url"] for r in rows)


def test_duplicate_pdf_urls_dropped():
    row = ('<table><tr><td>2020</td><td>1 May</td><td>T</td><td>L</td>'
           '<td>1234</td><td>ZS-ABC</td>'
           '<td><a href="https://x.blob.core.windows.net/c/1234.pdf">D</a></td>'
           '</tr></table>')
    html = row + row
    assert len(sacaa.parse_listing(html)) == 1


# ── case_id ───────────────────────────────────────────────────────────────────

def test_case_id_numeric_name():
    assert sacaa.make_case_id("9690", "ZU-RLK", "2018-03-02") == "9690"


def test_case_id_category_row_uses_reg_date():
    cid = sacaa.make_case_id("ZU-PPA", "ZU-PPA", "2023-01-02")
    assert cid == "zu-ppa-2023-01-02"


def test_case_id_collision_suffix():
    taken = {"9690"}
    assert sacaa.make_case_id("9690", None, None, taken=taken) == "9690_2"


# ── helpers ───────────────────────────────────────────────────────────────────

def test_iso_date_variants():
    assert sacaa._iso_date("2 March", "2018") == "2018-03-02"
    assert sacaa._iso_date("27 January 2026") == "2026-01-27"
    assert sacaa._iso_date("17 February", None) is None
    assert sacaa._iso_date("") is None
