import pytest

from aaibzm_ingest import aaibzm


EXPECTED_REGS = {
    "9J-RDN", "9J-PZB", "9J-GRO", "9S-GAP", "9J-YVT", "9J-KMN", "9J-RHE",
}


# ── parse_listing (live fixture) ──────────────────────────────────────────────

def test_parse_listing_finds_all_cards(index_html):
    rows = aaibzm.parse_listing(index_html)
    assert len(rows) == 7


def test_parse_listing_finds_expected_registrations(index_html):
    rows = aaibzm.parse_listing(index_html)
    regs = {r["registration"] for r in rows}
    assert regs == EXPECTED_REGS


def test_parse_listing_excludes_nav_and_static_pages(index_html):
    # about/contact/downloads/terminology are NOT cards and must not appear,
    # and the nav-dropdown duplicates are de-duped to 7 rows.
    rows = aaibzm.parse_listing(index_html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))                  # unique
    assert all(c.startswith("aaibzm-") for c in ids)  # namespaced
    cids = set(ids)
    assert "aaibzm-about" not in cids
    assert "aaibzm-downloads" not in cids


def test_parse_listing_case_ids_intrinsic(index_html):
    rows = aaibzm.parse_listing(index_html)
    ids = {r["case_id"] for r in rows}
    assert "aaibzm-9j-yvt" in ids
    assert "aaibzm-9s-gap" in ids
    assert "aaibzm-9j-rdn" in ids


def test_parse_listing_pdf_urls_derived(index_html):
    rows = aaibzm.parse_listing(index_html)
    for r in rows:
        assert r["pdf_url"] == f"https://aaib.org.zm/reports/{r['registration']}.pdf"
        assert r["report_url"] == f"https://aaib.org.zm/pages/{r['registration']}.php"


def test_parse_listing_card_fields(index_html):
    rows = {r["registration"]: r for r in aaibzm.parse_listing(index_html)}
    rdn = rows["9J-RDN"]
    assert rdn["event_class"] == "Accident"
    assert rdn["event_date"] == "2022-02-27"
    assert "Piper PA-32-300" in rdn["aircraft"]
    assert "Mulobezi" in rdn["location"]

    gap = rows["9S-GAP"]
    assert gap["event_class"] == "Serious Incident"
    assert gap["event_date"] == "2020-01-10"
    assert "Shorts 360" in gap["aircraft"]
    assert "Ndola" in gap["location"]


def test_parse_listing_all_have_event_date(index_html):
    rows = aaibzm.parse_listing(index_html)
    assert all(r["event_date"] for r in rows)


# ── make_case_id ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reg,expected", [
    ("9J-YVT", "aaibzm-9j-yvt"),
    ("9J-RDN", "aaibzm-9j-rdn"),
    ("9S-GAP", "aaibzm-9s-gap"),
    ("9j-yvt", "aaibzm-9j-yvt"),       # case-insensitive
    ("9J – YVT", "aaibzm-9j-yvt"),     # en-dash / spaced PDF form
])
def test_make_case_id(reg, expected):
    assert aaibzm.make_case_id(reg) == expected


def test_make_case_id_is_deterministic():
    assert aaibzm.make_case_id("9J-YVT") == aaibzm.make_case_id("9J-YVT")


def test_make_case_id_intrinsic_no_order_suffix():
    # Same registration always yields the same id regardless of encounter order.
    assert aaibzm.make_case_id("9J-KMN") == "aaibzm-9j-kmn"
    cid = aaibzm.make_case_id("9J-KMN")
    assert not cid.split("-")[-1].isdigit() or "-" in cid


# ── pdf_url_for / detail_url_for ───────────────────────────────────────────────

@pytest.mark.parametrize("reg,expected", [
    ("9J-YVT", "https://aaib.org.zm/reports/9J-YVT.pdf"),
    ("9j – yvt", "https://aaib.org.zm/reports/9J-YVT.pdf"),
])
def test_pdf_url_for(reg, expected):
    assert aaibzm.pdf_url_for(reg) == expected


def test_detail_url_for():
    assert aaibzm.detail_url_for("9J-RHE") == "https://aaib.org.zm/pages/9J-RHE.php"


# ── find_registration ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("the aircraft 9J-YVT departed", "9J-YVT"),
    ("Registration Marks 9J – YVT, Serial", "9J-YVT"),   # spaced en-dash form
    ("a Shorts 360 9S-GAP at Ndola", "9S-GAP"),
    ("no registration present", None),
])
def test_find_registration(text, expected):
    assert aaibzm.find_registration(text) == expected


def test_find_registration_in_report(sample_report_text):
    # The real YVT PDF body mentions '9J-YVT'.
    assert aaibzm.find_registration(sample_report_text) == "9J-YVT"


# ── extract_event_date ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Accident ... on 27th February 2022", "2022-02-27"),
    ("on 7th September 2021", "2021-09-07"),
    ("on 10th JANUARY, 2020", "2020-01-10"),       # comma + caps
    ("on 3rd September 2019", "2019-09-03"),
])
def test_extract_event_date_variants(text, expected):
    assert aaibzm.extract_event_date(text) == expected


def test_extract_event_date_cover_block(cover_block_text):
    # 'Date of Accident - 10th JANUARY, 2020'
    assert aaibzm.extract_event_date(cover_block_text) == "2020-01-10"


def test_extract_event_date_none_when_absent():
    assert aaibzm.extract_event_date("no parseable date anywhere here") is None
    assert aaibzm.extract_event_date("") is None
    assert aaibzm.extract_event_date(None) is None


def test_extract_event_date_ignores_numeric_only():
    assert aaibzm.extract_event_date("on 27/02/2022") is None


# ── card-title field extractors ───────────────────────────────────────────────

def test_extract_event_class():
    assert aaibzm.extract_event_class("Serious Incident involving a Shorts 360") == \
        "Serious Incident"
    assert aaibzm.extract_event_class("Accident involving a Piper") == "Accident"
    assert aaibzm.extract_event_class("nothing here") is None


def test_extract_aircraft_from_title():
    t = ("Accident involving a Piper PA-32-300 Cherokee Six Aircraft "
         "Registration: 9J-RDN, 652m from Mulobezi airstrip on 27th February 2022")
    assert aaibzm.extract_aircraft_from_title(t) == "Piper PA-32-300 Cherokee Six"


def test_extract_location_from_title():
    t = ("Accident involving Cessna 172 M Registration: 9J-GRO, at Masebe "
         "airstrip in Central Province, on 7th September 2021")
    assert aaibzm.extract_location_from_title(t) == \
        "at Masebe airstrip in Central Province"


def test_extract_location_from_title_reg_form_agnostic():
    # The location extractor must tolerate BOTH the plain '9J-YVT' reg form and
    # the spaced en-dash '9J – YVT' form (LOW-1), like _REG_RE / _canon_reg.
    plain = ("Accident involving a Neo Tanarg Microlight Registration: 9J-YVT, "
             "at Maramba Aerodrome in Southern Province on 10th January 2020")
    spaced = ("Accident involving a Neo Tanarg Microlight Registration: 9J – YVT, "
              "at Maramba Aerodrome in Southern Province on 10th January 2020")
    expected = "at Maramba Aerodrome in Southern Province"
    assert aaibzm.extract_location_from_title(plain) == expected
    assert aaibzm.extract_location_from_title(spaced) == expected


# ── PDF cover-block fallback extractors ────────────────────────────────────────

def test_extract_cover_fields_present(cover_block_text):
    assert aaibzm.extract_aircraft(cover_block_text) == "TANARG NEO MICROLIGHT"
    assert aaibzm.extract_location(cover_block_text) == \
        "MARAMBA AERODROME, SOUTHERN PROVINCE, ZAMBIA"
    assert aaibzm.extract_operator(cover_block_text) == "BATOKA SKY LIMITED"


def test_extract_cover_fields_absent():
    assert aaibzm.extract_aircraft("nothing labelled here") is None
    assert aaibzm.extract_location("nothing labelled here") is None
    assert aaibzm.extract_aircraft("") is None
    assert aaibzm.extract_location(None) is None


def test_value_regex_does_not_backtrack_on_blank_text_layer():
    """A scanned PDF whose text layer is only newlines must not hang the parse.

    The original pattern was `\\s*[-:–—]?\\s*(?:\\n\\s*)*([^\\n]+)`. \\s already
    matches \\n, so the two constructs could divide the same run of newlines
    exponentially many ways, and input that never reaches [^\\n]+ doubled the
    match time per newline — 24 newlines took 0.9s.

    Asserting a wall-clock budget rather than inspecting the pattern: what
    matters is that it finishes, and a rewrite that reintroduces the
    ambiguity should fail here.
    """
    import time

    for n in (24, 200, 2500):
        start = time.perf_counter()
        aaibzm._VALUE_RE.match("\n" * n)
        assert time.perf_counter() - start < 0.5, (
            f"{n} newlines took too long — the value regex is backtracking again"
        )


def test_value_regex_skips_a_separator_on_its_own_line():
    """Behaviour the possessive rewrite had to preserve."""
    m = aaibzm._VALUE_RE.match(" \n:\nthe value\nnext")
    assert m and m.group(1) == "the value"
