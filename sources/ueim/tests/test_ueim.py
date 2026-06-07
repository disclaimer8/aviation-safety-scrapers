from ueim_ingest import ueim


# ── slug / case_id ─────────────────────────────────────────────────────────


def test_slug_from_url():
    assert ueim.slug_from_url(
        "https://ulasimemniyeti.uab.gov.tr/uploads/pages/hava-araci/"
        "tc-ajc-hava-araci-kazasi-nihai-raporuu.pdf"
    ) == "tc-ajc-hava-araci-kazasi-nihai-raporuu"
    assert ueim.slug_from_url(None) is None


# ── report_type from filename suffix variants ──────────────────────────────


def test_report_type_final_variants():
    for slug in (
        "tc-bdj-nihai-rapor",
        "tc-azk-nihai-raporu",
        "tc-ayh-nihai",
        "tc-eof-final-raporu",
        "tc-ajf-nihai-rapor-karar-sayili",
        "tc-uyb-nihai-raporu-doc-1",
    ):
        assert ueim.report_type_from_filename(slug) == "final", slug


def test_report_type_preliminary():
    assert ueim.report_type_from_filename("9h-dfs-on-rapor") == "preliminary"
    # 'on-rapor' must beat the shared 'rapor' substring (not become 'final').
    assert ueim.report_type_from_filename(
        "tc-xxx-on-rapor-karar") == "preliminary"


def test_report_type_unknown():
    assert ueim.report_type_from_filename("tc-cck") == "unknown"
    assert ueim.report_type_from_filename("tc-syn") == "unknown"
    assert ueim.report_type_from_filename("") == "unknown"


# ── registration prefix extraction (incl. foreign) ─────────────────────────


def test_registration_from_slug_tc():
    assert ueim.registration_from_slug(
        "tc-ajc-hava-araci-kazasi-nihai-raporuu") == "TC-AJC"
    assert ueim.registration_from_slug("tc-cck") == "TC-CCK"
    # A 4-char tail still parses (e.g. older formats).
    assert ueim.registration_from_slug("tc-bvts-nihai") == "TC-BVTS"


def test_registration_from_slug_foreign():
    assert ueim.registration_from_slug("9h-dfs-on-rapor") == "9H-DFS"
    assert ueim.registration_from_slug("ep-mnp-nihai-rapor") == "EP-MNP"
    assert ueim.registration_from_slug("tu-rkc-e-jsh-karartildi") == "TU-RKC"


def test_registration_from_slug_none_when_not_reg_shaped():
    assert ueim.registration_from_slug("policy-document") is None
    assert ueim.registration_from_slug("") is None


def test_extract_registration_from_text():
    txt = "Olaya karışan TC-AJC tescil işaretli hava aracı..."
    assert ueim.extract_registration_from_text(txt) == "TC-AJC"
    assert ueim.extract_registration_from_text("yabancı tescilli uçak") is None
    assert ueim.extract_registration_from_text("") is None


# ── date parsing (numeric + Turkish month names) ───────────────────────────


def test_parse_date_numeric():
    assert ueim.parse_date("27.02.2023") == "2023-02-27"
    assert ueim.parse_date("04.07.2021") == "2021-07-04"
    assert ueim.parse_date("01.01.2018") == "2018-01-01"


def test_parse_date_turkish_month_names():
    assert ueim.parse_date("15 Şubat 2023") == "2023-02-15"
    assert ueim.parse_date("3 Ağustos 2021") == "2021-08-03"
    assert ueim.parse_date("9 Eylül 2019") == "2019-09-09"
    assert ueim.parse_date("21 Aralık 2020") == "2020-12-21"


def test_parse_date_all_turkish_months():
    months = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
              "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    for i, mon in enumerate(months, start=1):
        assert ueim.parse_date(f"07 {mon} 2022") == f"2022-{i:02d}-07"


def test_parse_date_unparseable():
    assert ueim.parse_date("") is None
    assert ueim.parse_date("Raporu bekleniyor") is None
    assert ueim.parse_date(None) is None


# ── live-fixture listing parse ─────────────────────────────────────────────


def test_parse_listing_count(listing_html):
    recs = ueim.parse_listing(listing_html, ueim.TR_LISTING, lang="tr")
    # 10 report rows in the fixture; the 'report pending' row has no PDF, and
    # the header chrome PDF (outside /uploads/pages/hava-araci/) is dropped.
    assert len(recs) == 10
    assert all(r["pdf_url"].startswith(
        "https://ulasimemniyeti.uab.gov.tr/uploads/pages/hava-araci/")
        for r in recs)


def test_parse_listing_drops_chrome_and_no_pdf_rows(listing_html):
    recs = ueim.parse_listing(listing_html, ueim.TR_LISTING)
    urls = [r["pdf_url"] for r in recs]
    assert not any("some-policy.pdf" in u for u in urls)
    # The 'Raporu bekleniyor' (TC-XXX) row carried no PDF → absent.
    assert not any(r["registration"] == "TC-XXX" for r in recs)


def test_parse_listing_fields(listing_html):
    recs = ueim.parse_listing(listing_html, ueim.TR_LISTING)
    by_id = {r["case_id"]: r for r in recs}
    r = by_id["tc-ajc-hava-araci-kazasi-nihai-raporuu"]
    assert r["registration"] == "TC-AJC"
    assert r["report_type"] == "final"
    assert r["event_date"] == "2022-05-21"
    assert "HEZARFEN" in r["location"].upper()
    assert r["lang"] == "tr"
    # Foreign-reg preliminary.
    f = by_id["9h-dfs-on-rapor"]
    assert f["registration"] == "9H-DFS"
    assert f["report_type"] == "preliminary"


def test_parse_listing_same_reg_two_accidents(listing_html):
    # TC-ERA appears on two DIFFERENT slugs/accidents — both must survive
    # (dedup is by PDF URL, never by registration).
    recs = ueim.parse_listing(listing_html, ueim.TR_LISTING)
    era = [r for r in recs if r["registration"] == "TC-ERA"]
    assert len(era) == 2
    assert {r["case_id"] for r in era} == {
        "tc-era-nihai-rapor", "tc-era-nihai-rapor-imzali"}


def test_parse_listing_dedupes_repeated_pdf_url(listing_html):
    # Concatenate the fixture with itself; identical PDF URLs must collapse.
    doubled = listing_html + listing_html
    recs = ueim.parse_listing(doubled, ueim.TR_LISTING)
    urls = [r["pdf_url"] for r in recs]
    assert len(urls) == len(set(urls)) == 10
