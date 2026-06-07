# tests/test_otkes.py
import os

from otkes_ingest import otkes

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _fix(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


# ─── year-URL harvest ─────────────────────────────────────────────────────────

ROOT_LINKS = [
    # aviation year pages (≥2014 plain, 2023 suffix, ≤2013 ilmailu-prefixed)
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2025.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2023_1.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/ilmailu2013.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/ilmailu1996.html",
    # topic pages
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/vanhemmattutkinnat.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/teematutkinnat.html",
    # NON-aviation year pages (must be rejected)
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/raideliikenneonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024.html",
    "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/vesiliikenneonnettomuuksientutkinta/tutkintaselostuksetvuosittain/raideliikenne2010.html",
    # global nav chrome (must be ignored)
    "https://turvallisuustutkinta.fi/fi/index/yhteystiedot.html",
]


def test_harvest_listing_urls_filters_and_orders():
    out = otkes.harvest_listing_urls(ROOT_LINKS)
    # 5 aviation year pages + 2 topic pages, NO rail/marine, NO nav chrome
    assert len(out) == 7
    assert all("/ilmailuonnettomuuksientutkinta/" in u for u in out)
    assert not any("raideliikenneonnettomuuksientutkinta" in u for u in out)
    assert not any("vesiliikenneonnettomuuksientutkinta" in u for u in out)
    assert not any("yhteystiedot" in u for u in out)
    # year pages first, newest-first
    year_part = [u for u in out if otkes.is_year_page(u)]
    assert "2025.html" in year_part[0]
    assert "ilmailu1996.html" in year_part[-1]
    # topic pages appended at the end
    assert otkes.is_topic_page(out[-1])


def test_year_page_pattern_flip_2013_vs_2014():
    y2014 = ROOT_LINKS[1]
    y2013 = ROOT_LINKS[3]
    assert otkes.year_from_year_url(y2014) == "2024"
    assert otkes.year_from_year_url(y2013) == "2013"
    assert otkes.is_year_page(y2014)
    assert otkes.is_year_page(y2013)


def test_year_page_suffix_variant():
    y2023 = ROOT_LINKS[2]
    assert otkes.is_year_page(y2023)
    assert otkes.year_from_year_url(y2023) == "2023"


def test_topic_page_recognised():
    assert otkes.is_topic_page(ROOT_LINKS[5])  # vanhemmat
    assert otkes.is_topic_page(ROOT_LINKS[6])  # teema
    assert not otkes.is_topic_page(ROOT_LINKS[1])  # plain year page


# ─── detail-URL harvest ───────────────────────────────────────────────────────

def test_harvest_detail_urls():
    year_links = [
        "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/selvityshelikopterinhatavaistostamuhoksella20.7.2024.html",
        "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/onnettomuusvantaalla.html",
        # nav chrome + the year-page itself (not details)
        "https://turvallisuustutkinta.fi/fi/index/yhteystiedot.html",
        "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024.html",
    ]
    out = otkes.harvest_detail_urls(year_links, "2024")
    assert len(out) == 2
    assert all("/2024/" in u and u.endswith(".html") for u in out)


def test_harvest_detail_urls_dedup():
    u = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/x.html"
    assert otkes.harvest_detail_urls([u, u, u], "2024") == [u]


def test_harvest_detail_urls_legacy_and_aggregated_folders():
    """≤2013 reports nest under ilmailu{year}/ and some years aggregate under a
    suffixed folder (2022 reports under 2023_1/) — both must be detected."""
    legacy = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/ilmailu2003/b12003llento-onnettomuus.html"
    aggregated = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2023_1/l2022-_1.html"
    # the listing page itself and a nested-listing link must NOT count
    listing = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/ilmailu2003.html"
    nested_listing = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/x/tutkintaselostuksetvuosittain.html"
    out = otkes.harvest_detail_urls([legacy, aggregated, listing, nested_listing])
    assert out == [legacy, aggregated]


# ─── case-number normalize + fallback ─────────────────────────────────────────

def test_normalize_modern_case():
    assert otkes.normalize_case_number("L2024-01") == "l2024-01"
    assert otkes.normalize_case_number("B2010-1") == "b2010-01"
    assert otkes.normalize_case_number("Tutkintanumero: C2005-12") == "c2005-12"


def test_normalize_legacy_case():
    assert otkes.normalize_case_number("B 4/1996") == "b1996-04"
    assert otkes.normalize_case_number("C12/2003") == "c2003-12"
    # trailing class letter (C9/2003L) must be dropped
    assert otkes.normalize_case_number("C9/2003L") == "c2003-09"
    # case number embedded in a title
    assert otkes.normalize_case_number(
        "C9/2003L Liikennelentokoneen vähäinen polttoaine"
    ) == "c2003-09"


def test_normalize_returns_none_when_absent():
    assert otkes.normalize_case_number("") is None
    assert otkes.normalize_case_number(None) is None
    assert otkes.normalize_case_number("ei numeroa") is None


def test_fallback_case_id_deterministic():
    url = "https://turvallisuustutkinta.fi/fi/index/tutkintaselostukset/ilmailuonnettomuuksientutkinta/tutkintaselostuksetvuosittain/2024/selvitys.html"
    a = otkes.fallback_case_id(url)
    b = otkes.fallback_case_id(url)
    assert a == b
    assert a.startswith("otkes-")
    assert len(a) == len("otkes-") + 8
    # different url → different id
    assert otkes.fallback_case_id(url + "x") != a


# ─── date parse ───────────────────────────────────────────────────────────────

def test_parse_fi_date():
    assert otkes.parse_fi_date("19.07.2024") == "2024-07-19"
    assert otkes.parse_fi_date("1.2.2003") == "2003-02-01"
    assert otkes.parse_fi_date("") is None
    assert otkes.parse_fi_date("ei pvm") is None


# ─── PDF pick (LIITE filtering) ───────────────────────────────────────────────

def test_pick_report_pdf_prefers_tutkintaselostus():
    hrefs = [
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/L2024-01_LIITE_1.pdf",
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/L2024-01_Tutkintaselostus.pdf",
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/L2024-01_LIITE_2.pdf",
    ]
    assert otkes.pick_report_pdf(hrefs).endswith("L2024-01_Tutkintaselostus.pdf")


def test_pick_report_pdf_skips_all_liite():
    hrefs = [
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/x_LIITE_1.pdf",
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/x_Liite_2.pdf",
    ]
    assert otkes.pick_report_pdf(hrefs) is None


def test_pick_report_pdf_fallback_first_nonliite():
    hrefs = [
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/x_LIITE_1.pdf",
        "https://turvallisuustutkinta.fi/material/sites/otkes/otkes/abc/raportti.pdf",
    ]
    assert otkes.pick_report_pdf(hrefs).endswith("raportti.pdf")


def test_pick_report_pdf_relative_made_absolute():
    out = otkes.pick_report_pdf(["/material/sites/otkes/otkes/abc/r_Tutkintaselostus.pdf"])
    assert out.startswith("https://turvallisuustutkinta.fi/")


def test_pick_report_pdf_none():
    assert otkes.pick_report_pdf([]) is None


# ─── registration ─────────────────────────────────────────────────────────────

def test_extract_registration():
    assert otkes.extract_registration("Lentokone OH-LZA joutui") == "OH-LZA"
    assert otkes.extract_registration("ei rekisteritunnusta") is None


# ─── detail innerText parse ───────────────────────────────────────────────────

def test_parse_detail_full():
    meta = otkes.parse_detail_text(_fix("detail_full.txt"))
    assert meta["case_number"] == "L2024-01"
    assert meta["occurrence_type"] == "Lentokoneet"
    assert meta["event_date"] == "2024-01-15"
    assert meta["publish_date"] == "2024-09-30"
    assert "OH-LZA" in meta["summary"]
    assert "TAPAHTUMAT" in meta["summary"]
    # the 'Tutkinnan aloituspäivä' label/value must NOT leak into the summary
    assert "aloituspäivä" not in meta["summary"].lower()


def test_parse_detail_selvitys_empty_case_number():
    meta = otkes.parse_detail_text(_fix("detail_selvitys.txt"))
    # blank Tutkintanumero (value line is the next label)
    assert meta["case_number"] is None
    assert meta["occurrence_type"] == "Helikopterit"
    assert meta["event_date"] == "2024-07-19"
    assert meta["publish_date"] == "2024-10-15"
    assert "FinnHEMS" in meta["summary"]


def test_parse_detail_legacy_no_event_date_label():
    """Old reports carry Tutkintanumero (with trailing letter) + Julkaisupäivä
    but NO Onnettomuuspäivä label."""
    meta = otkes.parse_detail_text(_fix("detail_legacy.txt"))
    assert otkes.normalize_case_number(meta["case_number"]) == "c2003-09"
    assert meta["event_date"] is None  # no Onnettomuuspäivä label present
    assert meta["publish_date"] == "2003-02-17"
    assert "Scandinavian" in meta["summary"]
