# tests/test_ciaape.py
"""Offline tests for ciaape_ingest.ciaape using saved live HTML fixtures."""
import os
import re

from ciaape_ingest import ciaape

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^CIAA-(ACCID|INCID|SINCID)-\d{1,4}-\d{4}$")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── case_id normalisation ──────────────────────────────────────────────────────

def test_normalize_case_id_collapses_space_around_dash():
    assert ciaape._normalize_case_id("CIAA- SINCID-001-2022") == "CIAA-SINCID-001-2022"


def test_normalize_case_id_collapses_space_around_slash():
    assert ciaape._normalize_case_id("CIAA-ACCID-008 / 2022") == "CIAA-ACCID-008/2022"


def test_normalize_case_id_uppercases():
    assert ciaape._normalize_case_id("ciaa-accid-001-2024") == "CIAA-ACCID-001-2024"


def test_make_case_id_zero_pads_number():
    assert ciaape.make_case_id("accid", "8", "2022") == "CIAA-ACCID-008-2022"
    assert ciaape.make_case_id("SINCID", "1", "2022") == "CIAA-SINCID-001-2022"
    assert ciaape.make_case_id("INCID", "011", "2018") == "CIAA-INCID-011-2018"


def test_event_class_mapping():
    assert ciaape._event_class("ACCID") == "Accident"
    assert ciaape._event_class("SINCID") == "Serious incident"
    assert ciaape._event_class("INCID") == "Incident"


def test_sheet_url():
    assert ciaape.sheet_url(3).endswith("?sheet=3")
    assert ciaape.sheet_url(3).startswith("https://www.gob.pe/")


# ── parse_collection — live sheet1 fixture ──────────────────────────────────────

def test_parse_collection_returns_rows():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    assert len(rows) >= 15, f"Expected >=15 rows, got {len(rows)}"


def test_parse_collection_case_id_format():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    for row in rows:
        assert CASE_ID_RE.match(row["case_id"]), f"Bad case_id: {row['case_id']!r}"


def test_parse_collection_report_urls_absolute_report_pages():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    for row in rows:
        assert row["report_url"].startswith(
            "https://www.gob.pe/institucion/mtc/informes-publicaciones/"
        ), f"Not a report page URL: {row['report_url']!r}"


def test_parse_collection_dates_iso_or_none():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    iso = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for row in rows:
        d = row["date_of_occurrence"]
        assert d is None or iso.match(d), f"Bad ISO date: {d!r}"


def test_parse_collection_no_duplicate_case_ids_within_sheet():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids within a sheet"


def test_parse_collection_event_classes_present():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    classes = {r["event_class"] for r in rows}
    assert "Accident" in classes
    assert classes <= {"Accident", "Serious incident", "Incident"}


def test_parse_collection_excludes_non_ciaa_links():
    """The privacy-policy footer link (-politica-de-privacidad-) must never appear."""
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    for row in rows:
        assert "privacidad" not in row["report_url"]


def test_parse_collection_captures_registration():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    regs = [r["registration"] for r in rows if r["registration"]]
    assert len(regs) >= 10, f"Expected many registrations, got {len(regs)}"
    # Peruvian regs are OB-XXXX; foreign regs (N/HC/CC/HP) also occur
    assert any(r.startswith("OB-") for r in regs)


def test_parse_collection_sheet2_also_parses():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet2.html"))
    assert len(rows) >= 15
    for row in rows:
        assert CASE_ID_RE.match(row["case_id"])


def test_parse_collection_titles_nonempty():
    rows = ciaape.parse_collection(_fixture("ciaape_sheet1.html"))
    for row in rows:
        assert row["title"], f"Empty title for {row['case_id']}"


# ── synthetic guard tests ───────────────────────────────────────────────────────

def test_parse_collection_skips_anchor_without_case_id():
    html = (
        '<a href="/institucion/mtc/informes-publicaciones/123-informe-final-ciaa-otros">'
        'Algo sin numero de caso</a>'
    )
    assert ciaape.parse_collection(html) == []


def test_parse_collection_prefers_final_over_provisional():
    """Same case_id appears as provisional then final → final row wins."""
    html = (
        '<a href="/institucion/mtc/informes-publicaciones/1-declaracion-provisional-ciaa-sincid-001-2022-matricula-ob-2214">'
        'Declaracion Provisional CIAA-SINCID-001-2022, Matricula OB-2214, Fecha 28/01/2022</a>'
        '<a href="/institucion/mtc/informes-publicaciones/2-informe-final-ciaa-sincid-001-2022-matricula-ob-2214">'
        'Informe Final CIAA-SINCID-001-2022, Matricula OB-2214, Fecha 28/01/2022</a>'
    )
    rows = ciaape.parse_collection(html)
    assert len(rows) == 1
    assert rows[0]["case_id"] == "CIAA-SINCID-001-2022"
    assert rows[0]["report_type"].startswith("Informe Final")
    assert rows[0]["report_url"].endswith("2-informe-final-ciaa-sincid-001-2022-matricula-ob-2214")


def test_parse_collection_spaced_case_id_normalised():
    html = (
        '<a href="/institucion/mtc/informes-publicaciones/9-informe-final-ciaa-sincid-001-2022">'
        'Informe Final CIAA- SINCID-001-2022, Matricula OB-2214, Fecha 28/01/2022</a>'
    )
    rows = ciaape.parse_collection(html)
    assert len(rows) == 1
    assert rows[0]["case_id"] == "CIAA-SINCID-001-2022"


# ── parse_report_page ───────────────────────────────────────────────────────────

def test_parse_report_page_returns_cdn_pdf():
    url = ciaape.parse_report_page(_fixture("ciaape_report.html"))
    assert url is not None
    assert url.startswith("https://cdn.www.gob.pe/uploads/document/file/")
    assert url.endswith(".pdf")


def test_parse_report_page_excludes_preview_jpg():
    url = ciaape.parse_report_page(_fixture("ciaape_report.html"))
    assert "preview_" not in url
    assert not url.endswith(".jpg")


def test_parse_report_page_none_when_no_pdf():
    assert ciaape.parse_report_page("<html>no pdf here</html>") is None
