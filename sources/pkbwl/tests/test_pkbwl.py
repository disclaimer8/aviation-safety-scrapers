from pkbwl_ingest import pkbwl


# ── listing URL + slug regex ───────────────────────────────────────────────


def test_listing_url_page1_is_bare():
    assert pkbwl.listing_url(1) == "https://pkbwl.gov.pl/raporty/"
    assert pkbwl.listing_url(0) == "https://pkbwl.gov.pl/raporty/"


def test_listing_url_pageN():
    assert pkbwl.listing_url(2) == "https://pkbwl.gov.pl/raporty/page/2/"
    assert pkbwl.listing_url(236) == "https://pkbwl.gov.pl/raporty/page/236/"


def test_extract_slugs_dedupes_and_ignores_pagination(listing_html):
    slugs = pkbwl.extract_slugs(listing_html)
    # 10 distinct report slugs; the duplicate 2026-0047 link is deduped and the
    # /raporty/page/2/ pagination link is NOT mistaken for a slug.
    assert slugs == [
        "2026-0039", "2026-0040", "2026-0041", "2026-0042", "2026-0043",
        "2026-0044", "2026-0045", "2026-0046", "2026-0047", "2026-0048",
    ]


def test_extract_slugs_empty():
    assert pkbwl.extract_slugs("<html></html>") == []
    assert pkbwl.extract_slugs("") == []


def test_detail_url():
    assert pkbwl.detail_url("2022-2456") == \
        "https://pkbwl.gov.pl/raporty/2022-2456/"


# ── never use the gov.pl/web alias (bot trap) ──────────────────────────────


def test_base_is_own_domain_not_govpl_alias():
    assert pkbwl.BASE == "https://pkbwl.gov.pl"
    assert "gov.pl/web" not in pkbwl.LISTING
    assert pkbwl.detail_url("2026-0039").startswith("https://pkbwl.gov.pl/")


# ── date normalization (already ISO on the page) ───────────────────────────


def test_normalize_date():
    assert pkbwl.normalize_date("2022-05-23") == "2022-05-23"
    assert pkbwl.normalize_date("Occurrence 2019-05-25 closed") == "2019-05-25"


def test_normalize_date_placeholder():
    assert pkbwl.normalize_date("-") is None
    assert pkbwl.normalize_date("") is None
    assert pkbwl.normalize_date(None) is None


# ── detail metadata parse incl. registration ───────────────────────────────


def test_parse_detail_metadata(detail_2456_html):
    m = pkbwl.parse_detail(detail_2456_html, "2022-2456")
    assert m["case_id"] == "2022-2456"
    assert m["event_date"] == "2022-05-23"
    assert m["aircraft"] == "Tecnam P2006T"
    assert m["registration"] == "SP-MMB"
    assert m["operator"] == "Bartolini Air"
    assert m["location"] == "EPBC"
    assert "SERIOUS INCIDENT" in m["occurrence_class"]
    assert m["investigation_status"] == "zakończone (closed)"


def test_parse_detail_registration_foreign_marks(detail_2456_html):
    # Registration is taken verbatim from the page (SP- or foreign).
    m = pkbwl.parse_detail(detail_2456_html, "2022-2456")
    assert m["registration"] == "SP-MMB"


def test_parse_detail_nopdf_has_no_documents(detail_nopdf_html):
    m = pkbwl.parse_detail(detail_nopdf_html, "2026-0040")
    assert m["registration"] == "SP-NHM"
    assert m["documents"] == []
    assert pkbwl.pick_narrative(m["documents"]) is None


# ── PDF lang classification ────────────────────────────────────────────────


def test_pdf_lang_english_variants():
    assert pkbwl.pdf_lang("2022_2456_RW_ENG.pdf") == "en"
    assert pkbwl.pdf_lang("2023_0005_RK_EN.pdf") == "en"
    assert pkbwl.pdf_lang("https://x/2018-0503_U_ENG.pdf") == "en"


def test_pdf_lang_polish_default():
    assert pkbwl.pdf_lang("2022-2456_RK.pdf") == "pl"
    assert pkbwl.pdf_lang("2019_1816_RW.pdf") == "pl"
    assert pkbwl.pdf_lang("U_2020_3931.pdf") == "pl"


# ── PDF variant preference: Final > … and EN preferred ─────────────────────


def test_pick_narrative_prefers_final_and_english(detail_2456_html):
    m = pkbwl.parse_detail(detail_2456_html, "2022-2456")
    url, lang, rtype = pkbwl.pick_narrative(m["documents"])
    assert rtype == "Final"          # RK beats the RW preliminary
    assert lang == "en"              # EN variant within the Final row
    assert url.endswith("2022-2456_RK_ENG.pdf")


def test_pick_narrative_pl_only_resolution(detail_1098_html):
    m = pkbwl.parse_detail(detail_1098_html, "2015-1098")
    # Only a Resolution PDF (PL) exists → it is chosen, lang pl.
    url, lang, rtype = pkbwl.pick_narrative(m["documents"])
    assert rtype == "Resolution"
    assert lang == "pl"
    assert url.endswith("2015_1098_U.pdf")


def test_pl_fallback_returns_polish_sibling(detail_2456_html):
    m = pkbwl.parse_detail(detail_2456_html, "2022-2456")
    pl = pkbwl.pl_fallback(m["documents"], "Final")
    assert pl.endswith("2022-2456_RK.pdf")


# ── spaced-letter density fallback heuristic ───────────────────────────────


def test_single_char_fraction():
    assert pkbwl.single_char_fraction("the quick brown fox jumped") == 0.0
    assert pkbwl.single_char_fraction("P R E L I M I N A R Y") == 1.0
    assert pkbwl.single_char_fraction("") == 1.0


def test_is_degenerate_spaced_letters():
    clean = "This is a normal English accident report narrative. " * 20
    spaced = "P R E L I M I N A R Y R E P O R T " * 40
    assert not pkbwl.is_degenerate(clean)
    assert pkbwl.is_degenerate(spaced)        # high single-char fraction


def test_is_degenerate_too_short():
    assert pkbwl.is_degenerate("short")       # below the 300-char floor
    assert not pkbwl.is_degenerate("word " * 200)
