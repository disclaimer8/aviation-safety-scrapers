import pytest

from aet_ingest import aet


# ── parse_listing (live fixture) ──────────────────────────────────────────────

def test_parse_listing_keeps_final_and_historical(index_html):
    rows = aet.parse_listing(index_html)
    sections = {}
    for r in rows:
        sections.setdefault(r["section"], 0)
        sections[r["section"]] += 1
    # 8 AET-issued final + 19 foreign-authority final + 8 historical = 35
    assert sections.get("final-aet") == 8
    assert sections.get("final-foreign") == 19
    assert sections.get("historical") == 8
    assert len(rows) == 35


def test_parse_listing_excludes_preliminary_open_and_forms(index_html):
    rows = aet.parse_listing(index_html)
    sections = {r["section"] for r in rows}
    assert "preliminary" not in sections
    assert "open" not in sections
    assert "other" not in sections
    # no ASR reporting forms slipped in
    assert not any("asr-" in r["case_id"] for r in rows)


def test_parse_listing_drops_bulletins(index_html):
    rows = aet.parse_listing(index_html)
    urls = {r["pdf_url"] for r in rows}
    ids = {r["case_id"] for r in rows}
    # the known preliminary bulletin must be dropped
    assert not any("/bulletin/" in u for u in urls)
    assert "aet-oe-9513-fr-bulletin-1-2026" not in ids
    assert "aet-oe-9513-en-bulletin-1-2026" not in ids
    # the open-section preliminary report must be dropped
    assert "aet-aet-preliminary-report-lx-ocv-14052023-r060824" not in ids
    # communiqués (preliminary section) dropped
    assert "aet-oo-elf-communique-fr" not in ids


def test_parse_listing_case_ids_unique_and_intrinsic(index_html):
    rows = aet.parse_listing(index_html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))                 # unique
    assert all(c.startswith("aet-") for c in ids)    # namespaced


def test_parse_listing_urls_absolute_https_and_titled(index_html):
    rows = aet.parse_listing(index_html)
    for r in rows:
        assert r["pdf_url"].startswith("https://aet.gouvernement.lu/")
        assert r["pdf_url"].endswith(".pdf")
        # legacy content/dam alias normalised to the working dam-assets twin
        assert "/content/dam/gouv2024_aet/" not in r["pdf_url"]
        assert r["title"]
        assert "<" not in r["title"]


def test_parse_listing_known_final_cases(index_html):
    rows = aet.parse_listing(index_html)
    ids = {r["case_id"] for r in rows}
    assert "aet-cessna-c177-factual-report-final" in ids
    assert "aet-acc-ellx-b742-01-11-1992" in ids
    assert "aet-lx-vcf-report" in ids


def test_parse_listing_flags_foreign_authority(index_html):
    rows = aet.parse_listing(index_html)
    foreign = {r["case_id"] for r in rows if r["foreign"]}
    # foreign-section docs are flagged, AET/historical are not
    assert "aet-bea2020-0237" in foreign            # French BEA report
    assert "aet-lx-vcf-report" in foreign
    assert "aet-cessna-c177-factual-report-final" not in foreign   # AET-issued
    assert "aet-acc-ellx-b742-01-11-1992" not in foreign           # historical
    assert len(foreign) == 19


def test_parse_listing_detects_languages(index_html):
    rows = aet.parse_listing(index_html)
    langs = {r["case_id"]: r["lang"] for r in rows}
    # explicit filename suffixes
    assert langs["aet-bea2020-0237en"] == "en"          # '-...0237en'
    assert langs["aet-20200719-lf-sb-lx-ava-v7-0-e-ref"] == "en"  # '-e-ref'
    # mixed corpus -> more than one language present
    assert len(set(langs.values())) >= 2


# ── absolutize ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("href,expected", [
    ("//aet.gouvernement.lu/dam-assets/x/y.pdf",
     "https://aet.gouvernement.lu/dam-assets/x/y.pdf"),
    ("http://aet.gouvernement.lu/content/dam/gouv2024_aet/l-administration/aviation-civile/z.pdf",
     "https://aet.gouvernement.lu/dam-assets/l-administration/aviation-civile/z.pdf"),
    ("https://aet.gouvernement.lu/dam-assets/a.pdf",
     "https://aet.gouvernement.lu/dam-assets/a.pdf"),
])
def test_absolutize(href, expected):
    assert aet.absolutize(href) == expected


# ── make_case_id ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("href,expected", [
    ("//aet.gouvernement.lu/dam-assets/l-administration/aviation-civile/CESSNA-C177-factual-report-FINAL.pdf",
     "aet-cessna-c177-factual-report-final"),
    ("//aet.gouvernement.lu/dam-assets/l-administration/aviation-civile/acc-ellx-b742-01-11-1992.pdf",
     "aet-acc-ellx-b742-01-11-1992"),
    ("http://aet.gouvernement.lu/content/dam/gouv2024_aet/l-administration/aviation-civile/aviation-civile-ancien-site-doc-a-renommer/20170201-accident-ballon-limpach.pdf",
     "aet-20170201-accident-ballon-limpach"),
])
def test_make_case_id(href, expected):
    assert aet.make_case_id(href) == expected


def test_make_case_id_intrinsic_independent_of_alias(index_html):
    # the content/dam alias and its dam-assets twin yield the SAME case_id
    a = aet.make_case_id(
        "http://aet.gouvernement.lu/content/dam/gouv2024_aet/l-administration/aviation-civile/x.pdf")
    b = aet.make_case_id(
        "//aet.gouvernement.lu/dam-assets/l-administration/aviation-civile/x.pdf")
    assert a == b == "aet-x"


def test_make_case_id_deterministic():
    href = "//aet.gouvernement.lu/dam-assets/x/LX-VCF-Report.pdf"
    assert aet.make_case_id(href) == aet.make_case_id(href)


def test_make_case_id_urlencoded_equals_plain():
    assert aet.make_case_id("//aet.gouvernement.lu/x/A%20B.pdf") == \
           aet.make_case_id("//aet.gouvernement.lu/x/A B.pdf")


# ── detect_lang ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("href,expected", [
    ("//aet/x/oo-elf-communique-en.pdf", "en"),
    ("//aet/x/oo-elf-communique-fr.pdf", "fr"),
    ("//aet/x/20200719-LF-SB-LX-AVA-V7-0-e-ref.pdf", "en"),
    ("//aet/x/BEA2020-0237en.pdf", "en"),
])
def test_detect_lang_filename(href, expected):
    assert aet.detect_lang(href) == expected


def test_detect_lang_default_fr_no_signal():
    assert aet.detect_lang("//aet/x/acc-ellx-b742-01-11-1992.pdf") == "fr"


def test_detect_lang_text_heuristic():
    fr = "le rapport relatif à l'accident survenu de l'aéronef avec des"
    en = "the final report on the accident of the aircraft with investigation"
    de = "der untersuchungsbericht über den unfall mit dem flugzeug und die"
    assert aet.detect_lang("//aet/x/foo.pdf", fr) == "fr"
    assert aet.detect_lang("//aet/x/foo.pdf", en) == "en"
    assert aet.detect_lang("//aet/x/foo.pdf", de) == "de"


# ── registration ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("the aircraft LX-VCF departed", "LX-VCF"),
    ("Boeing 747 registered LX-DCV involved", "LX-DCV"),
    ("foreign aircraft N524AT involved", "N524AT"),
    ("Diamond DA42 OO-ELF event", "OO-ELF"),
    ("no registration present", None),
])
def test_find_registration(text, expected):
    assert aet.find_registration(text) == expected


def test_find_registration_in_sample(sample_report_text):
    # best-effort; the C177 cover may or may not carry it -- must not crash
    aet.find_registration(sample_report_text)


def test_registration_from_filename():
    assert aet.registration_from_filename(
        "//aet/x/bombardier-global-6000-LX-NST-04-23.pdf") == "LX-NST"
    assert aet.registration_from_filename("//aet/x/acc-ellx-il-62m-29-09-1982.pdf") is None


# ── event date ────────────────────────────────────────────────────────────────

def test_extract_event_date_english_inline(sample_report_text):
    # 'EMERGENCY LANDING ... ON 18 JANUARY 2015'
    assert aet.extract_event_date(sample_report_text) == "2015-01-18"


@pytest.mark.parametrize("text,expected", [
    ("Accident survenu le 6 novembre 2002 à Luxembourg", "2002-11-06"),
    ("Unfall am 30. September 2015 Saarbrücken", "2015-09-30"),
    ("emergency landing on 18 January 2015 near Stegen", "2015-01-18"),
    ("accident du 1er novembre 1992 à Luxembourg", "1992-11-01"),
])
def test_extract_event_date_multilingual(text, expected):
    assert aet.extract_event_date(text) == expected


def test_extract_event_date_none_when_absent():
    assert aet.extract_event_date("no parseable date here") is None
    assert aet.extract_event_date("") is None
    assert aet.extract_event_date(None) is None


def test_date_from_filename():
    # DD-MM-YYYY / DD.MM.YYYY = genuine occurrence date -> kept.
    assert aet.date_from_filename(
        "//aet/x/acc-ellx-b742-01-11-1992.pdf") == "1992-11-01"
    assert aet.date_from_filename(
        "//aet/x/acc-ellx-b742-01.11.1992.pdf") == "1992-11-01"
    # A leading YYYYMMDD- prefix is the PUBLICATION date, not the occurrence
    # date (e.g. 20150801-...-embraer-145 published 2015, occurred 2006), so it
    # must NOT be returned as the event date.
    assert aet.date_from_filename(
        "//aet/x/20170201-accident-ballon-limpach.pdf") is None
    assert aet.date_from_filename(
        "//aet/x/20150801-schwere-stoerung-embraer-145-saarbrucken.pdf") is None
    assert aet.date_from_filename("//aet/x/CESSNA-C177-factual-report-FINAL.pdf") is None


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
        aet._VALUE_RE.match("\n" * n)
        assert time.perf_counter() - start < 0.5, (
            f"{n} newlines took too long — the value regex is backtracking again"
        )


def test_value_regex_skips_a_separator_on_its_own_line():
    """Behaviour the possessive rewrite had to preserve."""
    m = aet._VALUE_RE.match(" \n:\nthe value\nnext")
    assert m and m.group(1) == "the value"
