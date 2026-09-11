import pytest

from beacg_ingest import beacg


# -- parse_listing (live fixture) ---------------------------------------------

def test_parse_listing_finds_all_final_rows(index_html):
    rows = beacg.parse_listing(index_html)
    # 19 data rows in the table; the one still "En cours" (BEA-01-2022) has no
    # final-report PDF and is excluded -> 18 buildable rows.
    assert len(rows) == 18


def test_parse_listing_case_ids_unique_and_intrinsic(index_html):
    rows = beacg.parse_listing(index_html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))                 # unique
    assert all(c.startswith("beacg-") for c in ids)  # namespaced
    # no encounter-order numeric suffix: the trailing token is part of the
    # filename slug, never a bare 1..18 counter appended by the parser.
    assert ids == [beacg.make_case_id(r["pdf_url"]) for r in rows]


def test_parse_listing_urls_absolute_and_pdf(index_html):
    rows = beacg.parse_listing(index_html)
    for r in rows:
        assert r["pdf_url"].startswith("https://www.bea.cg/wp-content/uploads/")
        assert r["pdf_url"].lower().endswith(".pdf")


def test_parse_listing_known_cases(index_html):
    rows = beacg.parse_listing(index_html)
    by_id = {r["case_id"]: r for r in rows}
    assert "beacg-rapport-final-bea-03-2023-incid-boeing-737-36n-tn-akc-1" in by_id
    assert "beacg-rapport-denquete-bea-02-2022-accid-beechcraft-1900c-tn-aiq-equaflight-vf" in by_id
    assert "beacg-rapport-denquete-an-30-a100-de-aero-fret-business-le-06-08-07" in by_id


def test_parse_listing_extracts_structured_fields(index_html):
    rows = beacg.parse_listing(index_html)
    top = rows[0]
    assert top["bea_ref"] == "bea-03-2023"
    assert top["event_date"] == "2023-12-17"
    assert top["aircraft"] == "B737-36N"
    assert top["location"] == "Brazzaville"
    assert top["event_class"] == "Incident"


def test_parse_listing_numeric_event_dates_from_cells(index_html):
    rows = beacg.parse_listing(index_html)
    dates = {r["case_id"]: r["event_date"] for r in rows}
    # dd/mm/yyyy listing cell -> ISO
    assert dates["beacg-rapport-denquete-bea-02-2022-accid-beechcraft-1900c-tn-aiq-equaflight-vf"] == "2022-09-19"
    assert dates["beacg-rapport-denquete-an-30-a100-de-aero-fret-business-le-06-08-07"] == "2007-08-06"


def test_parse_listing_excludes_in_progress_row(index_html):
    rows = beacg.parse_listing(index_html)
    # BEA-01-2022 (B737-3Q8, status "En cours") has no final-report PDF.
    assert not any(r["bea_ref"] == "bea-01-2022" for r in rows)


def test_parse_listing_is_deduped(index_html):
    rows = beacg.parse_listing(index_html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))


# -- make_case_id -------------------------------------------------------------

@pytest.mark.parametrize("href,expected", [
    ("https://www.bea.cg/wp-content/uploads/2025/10/Rapport-final-_-BEA-03-2023_INCID-_Boeing-737-36N-TN-AKC-1.pdf",
     "beacg-rapport-final-bea-03-2023-incid-boeing-737-36n-tn-akc-1"),
    ("https://www.bea.cg/wp-content/uploads/2025/08/Rapport-denquete-AN-30-A100-de-AERO-FRET-BUSINESS-le-06.08.07.pdf",
     "beacg-rapport-denquete-an-30-a100-de-aero-fret-business-le-06-08-07"),
    ("https://www.bea.cg/wp-content/uploads/2025/08/Rapport-final-n%C2%B001-2018-du-Skyranger-Swift-le-02.09.18.pdf",
     "beacg-rapport-final-n-01-2018-du-skyranger-swift-le-02-09-18"),
])
def test_make_case_id(href, expected):
    assert beacg.make_case_id(href) == expected


def test_make_case_id_is_deterministic():
    href = "https://www.bea.cg/wp-content/uploads/2025/08/Rapport-Final-AN-12BP-de-TAC-le-21.03.11.pdf"
    assert beacg.make_case_id(href) == beacg.make_case_id(href)


def test_make_case_id_urlencoded_equals_plain():
    plain = "https://www.bea.cg/wp-content/uploads/2025/08/Rapport final.pdf"
    enc = "https://www.bea.cg/wp-content/uploads/2025/08/Rapport%20final.pdf"
    assert beacg.make_case_id(enc) == beacg.make_case_id(plain)


# -- _normalize_case_id / find_bea_ref ----------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("BEA-03-2023", "bea-03-2023"),
    ("BEA 02 2023", "bea-02-2023"),
    ("BEA-1-2023", "bea-01-2023"),          # zero-padded number
    ("No 01-2018", "bea-01-2018"),
    ("№01-2018", "bea-01-2018"),        # U+2116 NUMERO SIGN
    ("Enquete : BEA-03-2023", "bea-03-2023"),
])
def test_normalize_case_id(raw, expected):
    assert beacg._normalize_case_id(raw) == expected


def test_normalize_case_id_none():
    assert beacg._normalize_case_id("") is None
    assert beacg._normalize_case_id("no reference here") is None


def test_find_bea_ref_in_report(sample_report_text):
    assert beacg.find_bea_ref(sample_report_text) == "bea-03-2023"


# -- find_registration --------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("immatricule TN-AKC exploite", "TN-AKC"),
    ("registered TNAKA at the field", "TN-AKA"),
    ("the Boeing 5N-OTT departed", "5N-OTT"),
    ("foreign aircraft N524AT involved", "N524AT"),
    ("no registration present", None),
])
def test_find_registration(text, expected):
    assert beacg.find_registration(text) == expected


# -- extract_event_date -------------------------------------------------------

def test_extract_event_date_prose_cover_block(cover_block_text):
    # 'Incident survenu le 17 decembre 2023'
    assert beacg.extract_event_date(cover_block_text) == "2023-12-17"


def test_extract_event_date_survenu_anchored_prefers_anchor():
    # A spurious word-date appears BEFORE the prose anchor; the
    # 'survenu le' date must win.
    text = (
        "Publie le 1 janvier 2099 par le bureau.\n"
        "Incident survenu le 17 decembre 2023\n"
        "a Brazzaville\n"
    )
    assert beacg.extract_event_date(text) == "2023-12-17"


@pytest.mark.parametrize("text,expected", [
    ("survenu le 17 decembre 2023", "2023-12-17"),
    ("survenue le 2 septembre 2018", "2018-09-02"),
    ("Date de l'accident : 19 mars 2018", "2018-03-19"),
    ("evenement du 5 mai 2010", "2010-05-05"),
    ("Date de l'evenement - 06/08/2007", "2007-08-06"),   # numeric fallback
])
def test_extract_event_date_variants(text, expected):
    assert beacg.extract_event_date(text) == expected


def test_extract_event_date_accent_tolerant():
    # accented French month must still parse
    assert beacg.extract_event_date("survenu le 17 décembre 2023") == "2023-12-17"
    assert beacg.extract_event_date("le 19 août 2009") == "2009-08-19"


def test_extract_event_date_none_when_absent():
    assert beacg.extract_event_date("aucune date lisible ici") is None
    assert beacg.extract_event_date("") is None
    assert beacg.extract_event_date(None) is None


def test_extract_event_date_numeric_ddmmyyyy():
    # bare numeric dd/mm/yyyy (straight from the listing cell)
    assert beacg.extract_event_date("17/12/2023") == "2023-12-17"
    assert beacg.extract_event_date("06.08.2007") == "2007-08-06"


# -- extract_aircraft / location / operator (prose form) ----------------------

def test_extract_cover_fields_present(cover_block_text):
    assert beacg.extract_aircraft(cover_block_text) == "Boeing B737-36N"
    assert beacg.extract_location(cover_block_text) == "Brazzaville"
    assert beacg.extract_operator(cover_block_text) == "Africa Airlines"


def test_extract_cover_fields_absent():
    assert beacg.extract_aircraft("rien d'utile ici") is None
    assert beacg.extract_location("rien d'utile ici") is None
    assert beacg.extract_operator("rien d'utile ici") is None
    assert beacg.extract_aircraft("") is None
    assert beacg.extract_location(None) is None
