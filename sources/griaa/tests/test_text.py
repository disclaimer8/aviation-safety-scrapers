from griaa_ingest.text import (
    strip_html, slugify, make_site_slug, normalize_case_id,
    dmy_to_iso, strip_advertencia,
)


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tren de aterrizaje &amp; motor</p>  <b>falla</b>") == "Tren de aterrizaje & motor falla"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("COL-08-31-GIA") == "col-08-31-gia"
    assert slugify("  HK-4235!! ") == "hk-4235"


def test_make_site_slug_from_case_id():
    assert make_site_slug("COL-08-31-GIA") == "col-08-31-gia"
    assert make_site_slug("COL-24-58-DIACC") == "col-24-58-diacc"
    assert make_site_slug("COL-08-03-GIAB") == "col-08-03-giab"


def test_make_site_slug_fallback():
    assert make_site_slug("") == "crash-griaa"
    assert make_site_slug(None) == "crash-griaa"


def test_normalize_case_id_collapses_separator_whitespace():
    # CENIPA slug-collision lesson: the same case under two spellings must
    # normalise to one canonical form.
    assert normalize_case_id("COL - 08 - 03 / GIAB") == "COL-08-03/GIAB"
    assert normalize_case_id("COL-08-03-GIAB") == "COL-08-03-GIAB"
    assert normalize_case_id("  col-24-58-diacc  ") == "COL-24-58-DIACC"
    assert normalize_case_id("COL-08-31-GIA") == "COL-08-31-GIA"


def test_normalize_case_id_idempotent():
    a = normalize_case_id("COL - 25 - 44 - DIACC")
    assert a == "COL-25-44-DIACC"
    assert normalize_case_id(a) == a


def test_normalize_case_id_empty():
    assert normalize_case_id("") == ""
    assert normalize_case_id(None) == ""


def test_dmy_to_iso():
    assert dmy_to_iso("10/11/2025") == "2025-11-10"
    assert dmy_to_iso("3/1/2008") == "2008-01-03"
    assert dmy_to_iso("") is None
    assert dmy_to_iso("garbage") is None
    assert dmy_to_iso("10/11/25") is None  # 2-digit year rejected


# ── ADVERTENCIA preamble stripping ──

_PRELIM = """INFORME PRELIMINAR

Cessna 206
Matrícula HK641

ADVERTENCIA
El presente Informe Preliminar es presentado por la Autoridad de AIG de Colombia,
Dirección Técnica de Investigación de Accidentes Aéreos – DIACC.
El contenido de este documento no debe interpretarse como una indicación de las
conclusiones de la investigación.

SINOPSIS
Aeronave: Cessna TU206E / Matrícula HK641
El piloto realizó un despegue normal...
"""


def test_strip_advertencia_removes_preamble_keeps_body():
    out = strip_advertencia(_PRELIM)
    assert "ADVERTENCIA" not in out
    assert "El presente Informe Preliminar es presentado" not in out
    assert "SINOPSIS" in out
    assert "El piloto realizó un despegue normal" in out
    # header above ADVERTENCIA preserved
    assert "INFORME PRELIMINAR" in out
    assert "Matrícula HK641" in out


def test_strip_advertencia_final_report_variant_tabla_de_contenido():
    text = (
        "INFORME FINAL\n\n"
        "ADVERTENCIA\n"
        "El presente Informe Final refleja los resultados de la investigación...\n"
        "es contrario a los propósitos de la investigación.\n\n"
        "Tabla de contenido\n"
        "SIGLAS ... 5\n"
        "SINOPSIS ... 7\n"
    )
    out = strip_advertencia(text)
    assert "ADVERTENCIA" not in out
    assert "El presente Informe Final refleja" not in out
    assert "Tabla de contenido" in out


def test_strip_advertencia_noop_when_absent():
    text = "Algun texto de narrativa sin advertencia legal."
    assert strip_advertencia(text) == text


def test_strip_advertencia_empty():
    assert strip_advertencia("") == ""
    assert strip_advertencia(None) == ""


def test_strip_advertencia_no_end_marker_drops_header_only():
    text = "Header\n\nADVERTENCIA\nDisclaimer text that runs into the body directly."
    out = strip_advertencia(text)
    assert "ADVERTENCIA" not in out
    # body retained (conservative fallback)
    assert "Disclaimer text that runs into the body directly." in out
    assert "Header" in out


def test_strip_advertencia_formfeed_prefixed_header():
    """ADVERTENCIA preceded by a page-break form-feed (\x0c) is still detected."""
    text = (
        "Bogota - Colombia\n\n\x0cADVERTENCIA\n\n"
        "El presente Informe Preliminar es presentado por la Autoridad de AIG...\n"
        "no debe interpretarse como una indicacion de las conclusiones de la investigacion.\n\n"
        "\x0cCONTENIDO\n\nSIGLAS ... 4\nSINOPSIS ... 5\n"
    )
    out = strip_advertencia(text)
    assert "El presente Informe Preliminar es presentado" not in out
    assert "CONTENIDO" in out
    assert "Bogota - Colombia" in out


def test_strip_advertencia_contenido_end_marker():
    text = (
        "ADVERTENCIA\nlegal disclaimer paragraph.\n\nCONTENIDO\nSINOPSIS ... 5\n"
    )
    out = strip_advertencia(text)
    assert "legal disclaimer paragraph" not in out
    assert "CONTENIDO" in out
