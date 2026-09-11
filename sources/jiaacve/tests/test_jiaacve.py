# tests/test_jiaacve.py
"""Unit tests for JIAAC Venezuela scraper."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jiaacve_ingest import jiaacve


# ──────────────────────────────────────────────
# Listing parser tests
# ──────────────────────────────────────────────

SAMPLE_HTML = """
<div class="elementor-accordion-item">
<div id="elementor-tab-title-8991" class="elementor-tab-title">
    <a class="elementor-accordion-title" tabindex="0">2026</a>
</div>
<div id="elementor-tab-content-8991" class="elementor-tab-content">
    <ul class="dlm-downloads">
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/189736/?tmstv=1234"
               rel="nofollow" id="download-link-189736">
            Informe 001_2026 YV2988 Preliminar (122 descargas)
        </a></li>
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/189274/?tmstv=1234"
               rel="nofollow" id="download-link-189274">
            Informe 007_2024 YVO211 Final (69 descargas)
        </a></li>
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/167131/?tmstv=1234"
               rel="nofollow" id="download-link-167131">
            Informe final 002_2021 YV200T (313 descargas)
        </a></li>
    </ul>
</div>
</div>
<div class="elementor-accordion-item">
<div id="elementor-tab-title-89918" class="elementor-tab-title">
    <a class="elementor-accordion-title" tabindex="0">2009</a>
</div>
<div id="elementor-tab-content-89918" class="elementor-tab-content">
    <ul class="dlm-downloads">
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/121818/?tmstv=9999"
               rel="nofollow" id="download-link-121818">
            Expediente YV2492_006 (476 descargas)
        </a></li>
    </ul>
</div>
</div>
"""


def test_parse_listing_informe_count():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    assert len(rows) == 4


def test_parse_listing_informe_case_id():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    by_dl = {r["dl_id"]: r for r in rows}
    # Informe 001_2026 → 001/2026
    assert by_dl["189736"]["case_id"] == "001/2026"
    # Informe 007_2024 Final → 007/2024
    assert by_dl["189274"]["case_id"] == "007/2024"


def test_parse_listing_informe_final_type():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    by_dl = {r["dl_id"]: r for r in rows}
    assert by_dl["189274"]["report_type_raw"] == "Final"
    assert by_dl["189736"]["report_type_raw"] == "Preliminar"


def test_parse_listing_expediente_case_id():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    by_dl = {r["dl_id"]: r for r in rows}
    # Expediente YV2492_006 in 2009 section → 006/2009
    assert by_dl["121818"]["case_id"] == "006/2009"
    assert by_dl["121818"]["listing_year"] == 2009
    assert by_dl["121818"]["report_type_raw"] == "Expediente"


def test_parse_listing_registration():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    by_dl = {r["dl_id"]: r for r in rows}
    assert by_dl["189736"]["registration"] == "YV2988"
    assert by_dl["121818"]["registration"] == "YV2492"


def test_parse_listing_url_normalized():
    rows = jiaacve.parse_listing(SAMPLE_HTML)
    for r in rows:
        # tmstv should be stripped from stored URL
        assert "tmstv" not in r["pdf_url"]
        assert r["pdf_url"].startswith("https://www.mppt.gob.ve/download/")


# ──────────────────────────────────────────────
# Superseded resolution tests
# ──────────────────────────────────────────────

SAMPLE_HTML_WITH_DUPE = """
<div class="elementor-accordion-item">
<div id="elementor-tab-title-8991" class="elementor-tab-title">
    <a class="elementor-accordion-title" tabindex="0">2024</a>
</div>
<div id="elementor-tab-content-8991" class="elementor-tab-content">
    <ul class="dlm-downloads">
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/180001/?tmstv=1234">
            Informe 007_2024 YVO211 Preliminar (50 descargas)
        </a></li>
        <li><a class="download-link"
               href="https://www.mppt.gob.ve/download/189274/?tmstv=1234">
            Informe 007_2024 YVO211 Final (69 descargas)
        </a></li>
    </ul>
</div>
</div>
"""


def test_superseded_resolution_marks_prelim():
    rows = jiaacve.parse_listing(SAMPLE_HTML_WITH_DUPE)
    rows = jiaacve.resolve_superseded(rows)
    by_dl = {r["dl_id"]: r for r in rows}
    # Preliminar gets superseded_by set to the Final's dl_id
    assert by_dl["180001"]["superseded_by"] == "189274"
    # Final is primary
    assert by_dl["189274"]["superseded_by"] is None


# ──────────────────────────────────────────────
# PDF parsing helpers
# ──────────────────────────────────────────────

def test_parse_case_id_from_pdf_colon():
    text = "EXPEDIENTE: 007/2024\nOther content"
    assert jiaacve.parse_case_id_from_pdf(text) == "007/2024"


def test_parse_case_id_from_pdf_n_sign():
    text = "cursa en los registros de este despacho bajo el N°001/2026"
    assert jiaacve.parse_case_id_from_pdf(text) == "001/2026"


def test_parse_case_id_from_pdf_plain():
    text = "EXPEDIENTE 006/2009"
    assert jiaacve.parse_case_id_from_pdf(text) == "006/2009"


def test_parse_case_id_zero_pads():
    text = "EXPEDIENTE: 6/2009"
    assert jiaacve.parse_case_id_from_pdf(text) == "006/2009"


def test_parse_event_date_slash():
    text = "FECHA: 24/02/2024"
    assert jiaacve.parse_event_date(text) == "2024-02-24"


def test_parse_event_date_spanish():
    text = "El 06 de febrero de 2009, la aeronave"
    assert jiaacve.parse_event_date(text) == "2009-02-06"


def test_parse_registration():
    text = "MATRÍCULA: YVO211\nFABRICANTE"
    assert jiaacve.parse_registration(text) == "YVO211"


def test_parse_registration_ascii_variant():
    text = "MATRICULA: YV2492\n"
    assert jiaacve.parse_registration(text) == "YV2492"


def test_parse_aircraft():
    text = "FABRICANTE DE LA AERONAVE: BELL HELICOPTER TEXTRON.\nMODELO: 206B\n"
    result = jiaacve.parse_aircraft(text)
    assert "BELL" in result
    assert "206B" in result


def test_parse_location():
    text = "LUGAR: SECTOR LA PUEBLITA. ESTADO MÉRIDA.\n"
    result = jiaacve.parse_location(text)
    assert "PUEBLITA" in result


def test_parse_probable_cause():
    text = (
        "CAUSA PROBABLE\n\n"
        "la causa probable del accidente fue la combinación de factores\n"
        "meteorológicos adversos.\n\n"
        "RECOMENDACIONES"
    )
    result = jiaacve.parse_probable_cause(text)
    assert result is not None
    assert "meteorológicos" in result


def test_extract_narrative_returns_body():
    text = (
        "Cover page\nACLARATORIA\nBoilerplate legal text\n\n"
        "INFORMACIÓN SOBRE LOS HECHOS\n\n"
        "El 06 de enero de 2026, la aeronave matricula YV2988 " * 20
    )
    result = jiaacve.extract_narrative(text)
    assert "INFORMACIÓN" in result or "aeronave" in result
    assert len(result) >= 300


def test_extract_narrative_below_floor_returns_empty():
    text = "Short text"
    assert jiaacve.extract_narrative(text) == ""


# ──────────────────────────────────────────────
# site_slug test
# ──────────────────────────────────────────────

def test_site_slug_format():
    from jiaacve_ingest.pipeline import _make_site_slug
    assert _make_site_slug("007/2024") == "jiaacve-007-2024"
    assert _make_site_slug("001/2026") == "jiaacve-001-2026"
    assert _make_site_slug("dlm-189736") == "jiaacve-dlm-189736"
