# tests/test_mzisi.py
"""Offline tests for mzisi_ingest.mzisi using the saved live index fixture."""
import os
import re

from mzisi_ingest import mzisi

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── parse_index ───────────────────────────────────────────────────────────────

def test_parse_index_returns_rows():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    assert isinstance(rows, list)
    assert len(rows) >= 25, f"Expected >=25 final reports, got {len(rows)}"


def test_parse_index_only_final_and_summary():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    for r in rows:
        fn = r["pdf_url"].lower()
        assert "uvodno" not in fn, f"preliminary leaked: {fn}"
        assert "obvest" not in fn, f"closure notice leaked: {fn}"
        assert re.search(r"koncn|kon[čc]n|povzetek", fn), f"non-final kept: {fn}"


def test_parse_index_urls_absolute_pdf():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    for r in rows:
        assert r["pdf_url"].startswith("https://www.gov.si/"), r["pdf_url"]
        assert r["pdf_url"].lower().endswith(".pdf"), r["pdf_url"]


def test_parse_index_case_id_shape():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    for r in rows:
        assert r["case_id"].startswith("mzisi-"), r["case_id"]
        assert re.match(r"^mzisi-\d{4}-[a-z0-9-]+$", r["case_id"]), r["case_id"]


def test_parse_index_no_duplicate_case_ids():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate staging case_ids"


def test_parse_index_no_duplicate_urls():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    urls = [r["pdf_url"] for r in rows]
    assert len(urls) == len(set(urls)), "duplicate pdf_urls"


def test_parse_index_year_range():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    years = {int(r["year"]) for r in rows}
    assert min(years) <= 2008
    assert max(years) >= 2024


def test_parse_index_report_type_values():
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    types = {r["report_type"] for r in rows}
    assert types <= {"Final report", "Summary"}
    assert "Final report" in types


def test_parse_index_registration_best_effort():
    """At least some rows pull an S5- registration from the filename."""
    rows = mzisi.parse_index(_fixture("mzisi_index.html"))
    s5 = [r for r in rows if (r["registration"] or "").startswith("S5-")]
    assert len(s5) >= 3, f"Expected >=3 S5- regs, got {len(s5)}"


# ── make_case_id / _normalize_case_id ──────────────────────────────────────────

def test_make_case_id_from_path():
    cid = mzisi.make_case_id(
        "/assets/ministrstva/MzI/porocila-o-letalskih-nesrecah/2016/"
        "Koncno-porocilo-o-preiskavi-letalske-nesrece-S5-DES.pdf",
        "2016",
    )
    assert cid == "mzisi-2016-koncno-porocilo-o-preiskavi-letalske-nesrece-s5-des"


def test_normalize_case_id_collapses_whitespace_around_separators():
    assert mzisi._normalize_case_id("37200 - 6 / 2016") == "37200-6/2016"
    assert mzisi._normalize_case_id(" 37201-1/2025-2430-58 ") == "37201-1/2025-2430-58"
    assert mzisi._normalize_case_id("37200-7 / 2016") == "37200-7/2016"


def test_normalize_case_id_preserves_slash():
    out = mzisi._normalize_case_id("37200-6/2016")
    assert "/" in out


def test_normalize_case_id_none():
    assert mzisi._normalize_case_id(None) is None
    assert mzisi._normalize_case_id("") is None


# ── extract_case_no ─────────────────────────────────────────────────────────────

def test_extract_case_no_basic():
    text = "Številka:\n37200-6/2016-2430-39\nDatum:"
    assert mzisi.extract_case_no(text) == "37200-6/2016-2430-39"


def test_extract_case_no_37201():
    assert mzisi.extract_case_no("foo 37201-1/2025-2430-58 bar") == "37201-1/2025-2430-58"


def test_extract_case_no_short_form():
    assert mzisi.extract_case_no("ref 37200-7/2016 ok") == "37200-7/2016"


def test_extract_case_no_absent():
    assert mzisi.extract_case_no("no document number here") is None
    assert mzisi.extract_case_no("") is None


# ── detect_lang ─────────────────────────────────────────────────────────────────

def test_detect_lang_slovenian():
    txt = "KONČNO POROČILO o preiskavi letalske nesreče zrakoplova. Dejstva."
    assert mzisi.detect_lang(txt) == "sl"


def test_detect_lang_english():
    txt = ("FINAL REPORT on the investigation. The aircraft. Conclusions. "
           "Probable cause. The accident on the runway.")
    assert mzisi.detect_lang(txt) == "en"


def test_detect_lang_default_sl_on_empty():
    assert mzisi.detect_lang("") == "sl"
    assert mzisi.detect_lang(None) == "sl"


# ── extract_event_date (C1) ──────────────────────────────────────────────────

# A representative Slovenian cover block: a publication 'Datum:' line (later
# year, must be ignored) followed by the EVENT date line (word-month, declined).
_COVER_S5DES = (
    "Številka:\nDatum:\n\n37200-6/2016-2430-39\n25. 1. 2020\n\n"
    "KONČNO POROČILO\nO PREISKAVI LETALSKE NESREČE\n"
    "motornega letala PIPER PA-28-161,\nreg. oznake S5-DES,\n"
    "v bližini letališča BOVEC – LJBO\n1. septembra 2016\n"
)
_COVER_OEDYM = (
    "KONČNO POROČILO\nO PREISKAVI LETALSKE NESREČE\n"
    "MOTORNEGA LETALA PIPER PA28R-201,\n"
    "reg. oznake OE-DYM, v kraju Mengeš,\n3. 12. 2015\n"
)


def test_extract_event_date_numeric_ddmmyyyy():
    txt = "Datum: 24.02.2010\nKONČNO POROČILO\n30.08.2006 NA LETALIŠČU PORTOROŽ\n"
    assert mzisi.extract_event_date(txt, "mzisi-2006-jak") == "2006-08-30"


def test_extract_event_date_numeric_spaced():
    assert mzisi.extract_event_date(_COVER_OEDYM, "mzisi-2015-x") == "2015-12-03"


def test_extract_event_date_word_month_declined():
    # "5. maja 2020" — declined (genitive) Slovenian word-month.
    txt = "Datum:\n29. 3. 2022\n5. maja 2020 v bližini vasi Spodnja Gorica\n"
    assert mzisi.extract_event_date(txt, "mzisi-2020-x") == "2020-05-05"


def test_extract_event_date_word_month_uppercase():
    txt = "Datum:\nPRIPETILA 28. JULIJA 2009 V KRAJU IZLAKE\n"
    assert mzisi.extract_event_date(txt, "mzisi-2009-utva") == "2009-07-28"


def test_extract_event_date_excludes_publication_datum_same_year():
    # Publication 'Datum: 09.12.2008' shares the event year 2008 but must lose
    # to the real event date '03. MAJA 2008'.
    txt = "Datum: 09.12.2008\nKONČNO POROČILO\n03. MAJA 2008 NA LETALIŠČU\n"
    assert mzisi.extract_event_date(txt, "mzisi-2008-lak-12") == "2008-05-03"


def test_extract_event_date_year_disambiguates():
    # Event year from case_id picks 2016 over the 2020 publication date.
    assert mzisi.extract_event_date(_COVER_S5DES, "mzisi-2016-koncno-s5-des") == "2016-09-01"


def test_extract_event_date_absent_returns_none():
    assert mzisi.extract_event_date("no dates at all here", "mzisi-2006-x") is None
    assert mzisi.extract_event_date("", "mzisi-2006-x") is None


# ── extract_location / extract_aircraft (C1) ─────────────────────────────────

def test_extract_location_present():
    assert mzisi.extract_location(_COVER_OEDYM) == "Mengeš"
    assert mzisi.extract_location(_COVER_S5DES) == "letališča BOVEC – LJBO"


def test_extract_location_absent_returns_none():
    assert mzisi.extract_location("KONČNO POROČILO\n09.12.2008\n") is None
    assert mzisi.extract_location("") is None


def test_extract_aircraft_present():
    assert mzisi.extract_aircraft(_COVER_S5DES) == "PIPER PA-28-161"
    assert mzisi.extract_aircraft(_COVER_OEDYM) == "PIPER PA28R-201"


def test_extract_aircraft_absent_returns_none():
    assert mzisi.extract_aircraft("Datum: 09.12.2008\nbesedilo brez tipa\n") is None
    assert mzisi.extract_aircraft("") is None


def test_extract_aircraft_rejects_date_time_leak():
    # Older reports without a clean model line: 'letala S5-PIJ ... 8.7.2010 ob 20:00'
    # must NOT be returned as an aircraft string.
    txt = "preiskavi nesreče letala S5-PIJ ki se je zgodila 8.7.2010 ob 20:00 po lokalnem času"
    assert mzisi.extract_aircraft(txt) is None
