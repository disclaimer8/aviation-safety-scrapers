from sub_ingest import sub


# ── hub parse: 8 categories, de-duped, unrelated nav ignored ───────────────


def test_parse_hub_eight_categories(hub_html):
    cats = sub.parse_hub(hub_html)
    assert cats == [
        "motorflugzeuge", "motorsegler", "segelflugzeuge", "hubschrauber",
        "ultraleichtflugzeuge", "heissluftballons",
        "fallschirme-haenge-paragleiter", "international",
    ]
    # duplicate motorflugzeuge link de-duped; schifffahrt (wrong path) excluded.
    assert cats.count("motorflugzeuge") == 1


def test_categories_constant_matches_hub(hub_html):
    assert set(sub.parse_hub(hub_html)) == set(sub.CATEGORIES)


# ── year-vs-flat branch detection ──────────────────────────────────────────


def test_parse_year_links_reads_actual_list_with_gaps(cat_year_html):
    links = sub.parse_year_links("motorflugzeuge", cat_year_html)
    years = [y for y, _ in links]
    # Gaps exist (2003 missing) — list is read, never assumed as a range.
    assert years == ["2001", "2002", "2004", "2024"]
    assert links[-1][1] == \
        "https://www.bmimi.gv.at/sub/berichte/luftfahrt/motorflugzeuge/2024.html"


def test_flat_category_has_no_year_links(cat_flat_html):
    # heissluftballons is FLAT → zero year links ⇒ pipeline takes the flat path.
    assert sub.parse_year_links("heissluftballons", cat_flat_html) == []


def test_parse_report_links_year_page(year2024_html):
    links = sub.parse_report_links("motorflugzeuge", year2024_html)
    assert links == [
        "https://www.bmimi.gv.at/sub/berichte/luftfahrt/motorflugzeuge/2024/"
        "0330_reims-cessna-fr172f_85305.html",
        "https://www.bmimi.gv.at/sub/berichte/luftfahrt/motorflugzeuge/2024/"
        "1223_airbus-a220-300_en.html",
    ]


def test_parse_report_links_flat_page(cat_flat_html):
    links = sub.parse_report_links("heissluftballons", cat_flat_html)
    assert links == [
        "https://www.bmimi.gv.at/sub/berichte/luftfahrt/heissluftballons/"
        "20221112_schroeder-fire-balloons-g60_24_85297.html",
        "https://www.bmimi.gv.at/sub/berichte/luftfahrt/heissluftballons/"
        "20241014_schroeder-fire-balloons-20250783344.html",
    ]


def test_parse_report_links_excludes_year_index(cat_year_html):
    # On a year-based CATEGORY page the only links are year-index pages
    # (/{cat}/{YYYY}.html) — none of those are report links.
    assert sub.parse_report_links("motorflugzeuge", cat_year_html) == []


# ── case_id derivation (path-based; trailing numeric NON-unique) ────────────


def test_case_id_from_url_year_based():
    url = ("https://www.bmimi.gv.at/sub/berichte/luftfahrt/"
           "motorflugzeuge/2024/0330_cirrus-sr20_85305.html")
    assert sub.case_id_from_url(url) == "motorflugzeuge--2024--0330_cirrus-sr20_85305"


def test_case_id_from_url_flat():
    url = ("/sub/berichte/luftfahrt/heissluftballons/"
           "20221112_schroeder-fire-balloons-g60_24_85297.html")
    assert sub.case_id_from_url(url) == \
        "heissluftballons--20221112_schroeder-fire-balloons-g60_24_85297"


def test_case_id_en_suffix_is_part_of_slug():
    # '_en' is part of the slug, NOT a language toggle — it stays in the id.
    url = "/sub/berichte/luftfahrt/motorflugzeuge/2024/1223_airbus-a220-300_en.html"
    assert sub.case_id_from_url(url) == "motorflugzeuge--2024--1223_airbus-a220-300_en"


def test_case_id_disambiguates_nonunique_trailing_numeric():
    # Two different reports could share trailing '85119' (scout: 5 collisions in
    # 231). The path-based case_id keeps them distinct.
    a = sub.case_id_from_url(
        "/sub/berichte/luftfahrt/heissluftballons/20061202_cameron_n-145_85119.html")
    b = sub.case_id_from_url(
        "/sub/berichte/luftfahrt/motorflugzeuge/2006/1202_cameron_85119.html")
    assert a != b
    assert a == "heissluftballons--20061202_cameron_n-145_85119"


def test_case_id_handles_no_clean_numeric():
    # 15+ slugs have no clean trailing numeric (e.g. '..._dnk-1', '..._3-14-27').
    url = "/sub/berichte/luftfahrt/international/20141129_oe_lfj_fokker_dnk-1.html"
    assert sub.case_id_from_url(url) == "international--20141129_oe_lfj_fokker_dnk-1"


def test_category_and_year_helpers():
    cid = "motorflugzeuge--2024--0330_cirrus-sr20_85305"
    assert sub.category_of(cid) == "motorflugzeuge"
    assert sub.year_of(cid) == "2024"
    flat = "heissluftballons--20221112_schroeder-g60_85297"
    assert sub.category_of(flat) == "heissluftballons"
    assert sub.year_of(flat) is None  # flat slugs have no year segment


# ── report-detail parse (real live fixtures) ───────────────────────────────


def test_parse_report_recent(report_recent_html):
    r = sub.parse_report(report_recent_html)
    assert r["event_date"] == "2024-03-30"           # time[datetime]
    assert r["aircraft"] == "Reims-Cessna FR172F"    # &#xa0; stripped
    assert r["gz"] == "2025-0.211.836"               # GZ token dropped
    assert "Stubaier Alpen" in r["location"]
    assert r["report_kind"] == "Abschlussbericht"
    assert r["pdf_url"].endswith("85305_AB.pdf")
    assert r["pdf_url"].startswith("https://www.bmimi.gv.at/dam/jcr:")


def test_parse_report_summary_between_abstract_and_infobox(report_recent_html):
    r = sub.parse_report(report_recent_html)
    s = r["summary_text"]
    # Narrative summary = the <p>s between p.abstract and div.infobox.
    assert s and len(s) > 300
    assert s.startswith("Am 30. März 2024")
    assert "tödliche Verletzungen" in s
    # The infobox 'erstellt am' / PDF label must NOT leak into the summary.
    assert "erstellt am" not in s
    assert "Abschlussbericht" not in s


def test_parse_report_old_vub(report_old_html):
    r = sub.parse_report(report_old_html)
    assert r["event_date"] == "2002-08-17"
    assert r["aircraft"] == "Diamond DA 20"          # nested lang span flattened
    assert r["gz"] == "2023-0.614.053"
    assert r["report_kind"] == "Vereinfachter Untersuchungsbericht"
    assert r["pdf_url"].endswith("_vub.pdf")


def test_parse_report_drops_zwischenbericht(report_zwischen_html):
    r = sub.parse_report(report_zwischen_html)
    # Zwischenbericht (interim) → report_kind None; GZ absent → None.
    assert r["report_kind"] is None
    assert r["gz"] is None
    assert r["aircraft"] == "Cirrus SR20"


# ── OE- registration (PDF-only, best-effort) ───────────────────────────────


def test_extract_registration_found():
    text = "Das Luftfahrzeug OE-DXY befand sich im Reiseflug."
    assert sub.extract_registration(text) == "OE-DXY"


def test_extract_registration_none_for_foreign():
    text = "Das spanisch registrierte Flugzeug EC-ABC kollidierte mit dem Gelände."
    assert sub.extract_registration(text) is None
    assert sub.extract_registration("") is None


# ── card parse (best-effort enrichment) ────────────────────────────────────


def test_parse_cards(year2024_html):
    cards = sub.parse_cards(year2024_html)
    url = ("https://www.bmimi.gv.at/sub/berichte/luftfahrt/motorflugzeuge/2024/"
           "0330_reims-cessna-fr172f_85305.html")
    assert url in cards
    assert cards[url]["aircraft"] == "Reims-Cessna FR172F"
    assert "Stubaier Alpen" in cards[url]["location"]
    assert cards[url]["card_date"] == "30. März 2024"
