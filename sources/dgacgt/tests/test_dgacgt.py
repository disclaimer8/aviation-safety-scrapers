from dgacgt_ingest import dgacgt


# ── autoindex discovery ───────────────────────────────────────────────────────

def test_iter_year_urls(read_fixture):
    urls = dgacgt.iter_year_urls(read_fixture("dgacgt_index.html"))
    # 1999..2026 present in fixture (year 2000/2004 absent server-side)
    assert len(urls) >= 25
    assert all(u.endswith("/") for u in urls)
    assert any(u.endswith("/2024/") for u in urls)
    assert any(u.endswith("/1999/") for u in urls)
    # ascending order
    years = [int(u.rstrip("/").rsplit("/", 1)[-1]) for u in urls]
    assert years == sorted(years)
    # parent dir link must NOT be picked up as a year
    assert not any(u.rstrip("/").endswith("INFORMES%20FINALES") for u in urls)


def test_iter_pdf_urls_2024(read_fixture):
    pdfs = dgacgt.iter_pdf_urls(read_fixture("dgacgt_year_2024.html"))
    assert len(pdfs) == 5
    assert all(u.lower().endswith(".pdf") for u in pdfs)
    assert all(u.startswith("https://www.dgac.gob.gt/") for u in pdfs)


def test_iter_pdf_urls_2008(read_fixture):
    pdfs = dgacgt.iter_pdf_urls(read_fixture("dgacgt_year_2008.html"))
    assert len(pdfs) == 20


def test_iter_pdf_urls_excludes_parent_dir(read_fixture):
    pdfs = dgacgt.iter_pdf_urls(read_fixture("dgacgt_year_2006.html"))
    assert all(".pdf" in u.lower() for u in pdfs)
    assert len(pdfs) == 11


# ── filename -> registration / date ───────────────────────────────────────────

def test_filename_from_url():
    u = ("https://www.dgac.gob.gt/wp-content/uploads/ORGANIZACION/UIA/"
         "INVESTIGACION%20DE%20ACCIDENTES/INFORMES%20FINALES/2024/"
         "Informe-Final-y-Anexos-TG-MIC-31JUL2024.pdf")
    assert dgacgt.filename_from_url(u) == "Informe-Final-y-Anexos-TG-MIC-31JUL2024"


import pytest


@pytest.mark.parametrize("name,expected", [
    ("Informe-Final-y-Anexos-TG-MIC-31JUL2024", "2024-07-31"),
    ("Informe-Final -y-Anexos-TG-SES-06-12-2024", "2024-12-06"),
    ("Informe-Final-y- Anexos-TG-HUY-19.12.2024", "2024-12-19"),
    ("01. Informe Final TG-LOK 19ENE2008", "2008-01-19"),
    ("Informe-Final-con-Anexos-C6-TAK-21-11-2015", "2015-11-21"),
    ("Informe-Final-y-Anexos-TG-CCF-18DIC20", "2020-12-18"),
    ("03. FAG A-37B 1654 25 ENE 2006", "2006-01-25"),
    ("08. TG-LUT 22 MAYO 2006", "2006-05-22"),
    ("INFORME FINAL TG-CFE DEL 18 SEP 2001 ", "2001-09-18"),
    # date must NOT latch onto registration digits (N38782 -> day '82')
    ("Informe-Final-y-Anexos-N38782-16-10-2014", "2014-10-16"),
    ("Informe-Final-con-anexos-N55500-10-03-2019", "2019-03-10"),
    # space-separated numeric date
    ("Informe-Final-con-Anexos-TG-BAA-15 02 2010", "2010-02-15"),
    # malformed 5-digit year typo -> no date (falls back to year-only case_id)
    ("Informe-Final-con-Anexos-TG-VUE-25-09-20112", None),
])
def test_parse_date_from_name(name, expected):
    assert dgacgt.parse_date_from_name(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("Informe-Final-y-Anexos-TG-MIC-31JUL2024", "TG-MIC"),
    ("02. Informe Final N6082P 06FEB2008", "N6082P"),
    ("04. Informe Final YS1001P 14FEB2008", "YS1001P"),
    ("Informe-Final-con-Anexos-C6-TAK-21-11-2015", "C6-TAK"),
    ("Informe-Final-con-Anexos-N-431SR-10-01-2011", "N431SR"),
    ("Informe Final Aeronave DC-1030 F-GTDI ano 2000 21.12.1999", "F-GTDI"),
    ("03. FAG A-37B 1654 25 ENE 2006", None),  # military, no civil mark
])
def test_parse_registration_from_name(name, expected):
    assert dgacgt.parse_registration_from_name(name) == expected


# ── case_id ───────────────────────────────────────────────────────────────────

def test_make_case_id_reg_and_date():
    assert dgacgt.make_case_id("TG-MIC", "2024-07-31", 2024, "x") == "TG-MIC-2024-07-31"


def test_make_case_id_missing_reg():
    assert dgacgt.make_case_id(None, "2006-01-25", 2006, "x") == "DGACGT-2006-01-25"


def test_make_case_id_missing_date():
    assert dgacgt.make_case_id("TG-XYZ", None, 2010, "x") == "TG-XYZ-2010"


def test_normalize_case_id_collapses_whitespace_and_unicode_hyphen():
    # CENIPA slug-collision lesson: spacing variants collapse to one id.
    assert dgacgt._normalize_case_id("A - 02 - 2015") == "A-02-2015"
    assert dgacgt._normalize_case_id("UIA‐A‐11‐2024") == "UIA-A-11-2024"
    assert dgacgt._normalize_case_id("TG-MIC / 2024") == "TG-MIC/2024"
    assert dgacgt._normalize_case_id("a--b") == "A-B"


def test_case_id_spacing_variants_are_equal():
    a = dgacgt.make_case_id("TG-MIC", "2024-07-31", 2024)
    b = dgacgt._normalize_case_id("TG-MIC - 2024-07-31")
    assert a == b


# ── PDF header extraction ─────────────────────────────────────────────────────

def test_extract_report_no():
    txt = "Unidad de Investigacion\nReporte No.:\nA-02-2015.\nInforme final."
    assert dgacgt.extract_report_no(txt) == "A-02-2015"


def test_extract_report_no_uia_prefix_and_unicode_hyphen():
    txt = "Reporte No.: UIA‐A‐11‐2024 Titulo:"
    assert dgacgt.extract_report_no(txt) == "UIA-A-11-2024"


def test_extract_pdf_metadata_date_and_location():
    txt = ("Titulo: Informe Final.\nTG-MIC.\nBell Helicopter Textron 206 B\n"
           "31 de julio de 2024\nFinca Las Marias, municipio de Escuintla, "
           "departamento de Escuintla, Guatemala.\n")
    meta = dgacgt.extract_pdf_metadata(txt)
    assert meta["date_iso"] == "2024-07-31"
    assert meta["location"] and "Escuintla" in meta["location"]
