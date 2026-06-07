from rnsa_ingest import rnsa


# ── per-year archive URL construction ──────────────────────────────────────


def test_year_pages_start_2009_and_probe_future():
    pages = rnsa.year_pages(first_year=2009, last_year=2025)
    assert pages[0] == \
        "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2009/"
    assert pages[-1] == \
        "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2025/"
    # one URL per year inclusive
    assert len(pages) == 2025 - 2009 + 1


def test_year_pages_default_probes_current_plus_one():
    import datetime
    pages = rnsa.year_pages()
    last_year = datetime.date.today().year + 1
    assert pages[-1].endswith(f"/{last_year}/")


def test_year_from_url():
    assert rnsa.year_from_url(
        "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2020/") == "2020"
    assert rnsa.year_from_url("https://rnsa.is/flug/") is None


# ── filename date parse (Icelandic + English months, both orders) ──────────


def test_parse_filename_date_icelandic_months():
    assert rnsa.parse_filename_date(
        "lokaskyrsla-tf-kff-a-bikf-23-mai-2020.pdf") == "2020-05-23"
    assert rnsa.parse_filename_date(
        "lokaskyrsla-tf-zzz-thann-6-agust-2013.pdf") == "2013-08-06"
    assert rnsa.parse_filename_date(
        "sandari-a-bieg-26-februar-2020.pdf") == "2020-02-26"


def test_parse_filename_date_english_day_after_month():
    # 'february-23rd-2017' (month before day, ordinal suffix)
    assert rnsa.parse_filename_date(
        "final-report-tf-fip-fuel-emergency-at-man-on-february-23rd-2017.pdf"
    ) == "2017-02-23"
    assert rnsa.parse_filename_date(
        "final-report-n525ff-on-birk-january-11th-2018.pdf") == "2018-01-11"


def test_parse_filename_date_english_day_before_month():
    assert rnsa.parse_filename_date(
        "final-report-tf-fij-on-final-approach-26-february-2013-1.pdf"
    ) == "2013-02-26"


def test_parse_filename_date_none_when_absent():
    assert rnsa.parse_filename_date("bradabirgdaskyrsla-tf-myx.pdf") is None
    assert rnsa.parse_filename_date("") is None
    assert rnsa.parse_filename_date("tf-ppa-final-report.pdf") is None


# ── registration extraction (single + dual TF-) ────────────────────────────


def test_extract_dual_registration():
    regs = rnsa.extract_registrations(
        "lokaskyrsla-um-flugumferdaratvik-tf-dro-og-tf-kfb-1")
    assert regs == ["TF-DRO", "TF-KFB"]


def test_extract_registration_single_and_normalised():
    assert rnsa.extract_registration(
        "lokaskyrsla-tf-zzz-i-fluggordum") == "TF-ZZZ"
    # filename without hyphen still normalises to TF-XXX
    assert rnsa.extract_registration("flugslys-tfkex-2010") == "TF-KEX"


def test_extract_registration_from_pdf_text():
    text = "The aircraft TF-KFG collided on the runway at Keflavik."
    assert rnsa.extract_registration(text) == "TF-KFG"


def test_extract_registration_none():
    assert rnsa.extract_registration(
        "m-01313-aig-09-russian-97005-final-report") is None
    assert rnsa.extract_registration("") is None


# ── ICAO airport token ─────────────────────────────────────────────────────


def test_extract_icao():
    assert rnsa.extract_icao(
        "lokaskyrsla-tf-kff-flugumferdaratvik-a-bikf-23-mai-2020") == "BIKF"
    assert rnsa.extract_icao("sandari-a-bieg-26-februar-2020") == "BIEG"
    assert rnsa.extract_icao("tf-nla-loss-of-cabin-pressure-bgno") == "BGNO"
    assert rnsa.extract_icao("bradabirgdaskyrsla-tf-myx") is None


# ── report kind (final vs interim) ─────────────────────────────────────────


def test_report_kind_final():
    assert rnsa.report_kind("lokaskyrsla-tf-zzz-birk.pdf") == "Final"
    assert rnsa.report_kind("final-report-tf-kfd-near-geysir.pdf") == "Final"


def test_report_kind_interim():
    assert rnsa.report_kind("bradabirgdaskyrsla-tf-myx.pdf") == "Interim"
    assert rnsa.report_kind(
        "interim-report-tf-fia-landing-gear-collapse.pdf") == "Interim"


def test_report_kind_none():
    assert rnsa.report_kind("tf-fto-a-birk-thann-8-mars-2017.pdf") is None


# ── language detection ─────────────────────────────────────────────────────


def test_detect_lang_from_filename():
    assert rnsa.detect_lang("lokaskyrsla-tf-zzz-birk.pdf") == "is"
    assert rnsa.detect_lang("bradabirgdaskyrsla-tf-myx.pdf") == "is"
    assert rnsa.detect_lang("final-report-tf-kfd-near-geysir.pdf") == "en"
    assert rnsa.detect_lang("interim-report-tf-fia.pdf") == "en"


def test_detect_lang_text_overrides_filename():
    # Icelandic-named file but the PDF text layer is actually English.
    text = ("This final report describes the aircraft and the pilot during "
            "the flight on the runway. The investigation found...")
    assert rnsa.detect_lang("lokaskyrsla-tf-kfd.pdf", text=text) == "en"
    # English-named file but Icelandic body.
    is_text = ("Þann dag var flugvél og flugmaður á flugi þar sem ekki "
               "tókst að lenda við flugvöllinn.")
    assert rnsa.detect_lang("final-report-tf-x.pdf", text=is_text) == "is"


def test_detect_lang_defaults_is():
    # No keyword, no decisive text → defaults to source-native 'is'.
    assert rnsa.detect_lang("tf-ppa.pdf") == "is"


# ── form / notification PDF filter ─────────────────────────────────────────


def test_is_form_pdf():
    assert rnsa.is_form_pdf("tilkynning-flugatvik-eydublad.pdf")
    assert rnsa.is_form_pdf("skraningarform.pdf")
    assert not rnsa.is_form_pdf("lokaskyrsla-tf-zzz-birk.pdf")
    assert not rnsa.is_form_pdf("final-report-tf-kfd.pdf")


# ── fallback event date ────────────────────────────────────────────────────


def test_fallback_event_date():
    assert rnsa.fallback_event_date("2022") == "2022-01-01"
    assert rnsa.fallback_event_date(None) is None


# ── live-fixture year-page parse ───────────────────────────────────────────


def test_parse_year_page_count_and_form_filter(year2013_html):
    recs = rnsa.parse_year_page(
        year2013_html, "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2013/")
    # 6 item blocks, but the tilkynning form (media 9001) is dropped → 5.
    assert len(recs) == 5
    ids = {r["case_id"] for r in recs}
    assert "9001" not in ids
    assert ids == {"1173", "1168", "1170", "1169", "1172"}


def test_parse_year_page_metadata(year2013_html):
    recs = rnsa.parse_year_page(
        year2013_html, "https://rnsa.is/flug/slysa-og-atvikaskyrslur/2013/")
    by_id = {r["case_id"]: r for r in recs}

    # case_id = numeric media id; year from URL.
    assert by_id["1168"]["year"] == "2013"
    assert by_id["1168"]["registration"] == "TF-ZZZ"
    assert by_id["1168"]["event_date"] == "2013-08-06"  # Icelandic 'agust'
    assert by_id["1168"]["report_kind"] == "Final"
    assert by_id["1168"]["lang"] == "is"
    assert by_id["1168"]["pdf_url"].startswith("https://rnsa.is/media/1168/")
    assert by_id["1168"]["title"]
    assert by_id["1168"]["summary"]

    # Dual registration on one report.
    assert by_id["1169"]["registrations"] == ["TF-DRO", "TF-KFB"]
    assert by_id["1169"]["registration"] == "TF-DRO"

    # English final report + ICAO + English month date.
    assert by_id["1172"]["report_kind"] == "Final"
    assert by_id["1172"]["lang"] == "en"
    assert by_id["1172"]["location"] == "BIKF"
    assert by_id["1172"]["event_date"] == "2013-02-26"

    # Interim report, no parseable date.
    assert by_id["1170"]["report_kind"] == "Interim"
    assert by_id["1170"]["event_date"] is None
