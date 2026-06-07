# tests/test_ciaiac.py
"""Offline tests for ciaiac_ingest.ciaiac using saved HTML fixtures."""
import os
import re

import pytest

from ciaiac_ingest import ciaiac

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^(A|IN)-\d{1,4}/\d{4}$")


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────
# iter_year_urls
# ──────────────────────────────────────────────

def test_iter_year_urls_returns_list():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    assert isinstance(urls, list)
    assert len(urls) >= 20, f"Expected ≥20 year URLs, got {len(urls)}"


def test_iter_year_urls_includes_2024():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    assert any("2024" in u for u in urls), "No 2024 URL found"


def test_iter_year_urls_all_absolute():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    for url in urls:
        assert url.startswith("https://"), f"Not absolute: {url!r}"


def test_iter_year_urls_no_distribucion():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    for url in urls:
        assert "distribucion" not in url, f"distribucion crept in: {url!r}"


def test_iter_year_urls_includes_semester_variants():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    # 2002 has _1s / _2s, 2004 has _1s / _2s, 2019 has primer/segundo semestre
    semesters = [u for u in urls if re.search(r"(1s|2s|semestre)", u)]
    assert len(semesters) >= 2, f"Expected ≥2 semester URLs, got {semesters}"


def test_iter_year_urls_no_duplicates():
    html = _fixture("ciaiac_index.html")
    urls = ciaiac.iter_year_urls(html)
    assert len(urls) == len(set(urls)), "Duplicate URLs in iter_year_urls result"


# ──────────────────────────────────────────────
# parse_listing — 2024 fixture
# ──────────────────────────────────────────────

def test_parse_listing_2024_returns_rows():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    assert len(rows) >= 10, f"Expected ≥10 rows from 2024 listing, got {len(rows)}"


def test_parse_listing_2024_case_id_format():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    for row in rows:
        assert CASE_ID_RE.match(row["case_id"]), (
            f"Invalid case_id format: {row['case_id']!r}"
        )


def test_parse_listing_2024_known_row():
    """IN-002/2024 is the first row in the 2024 fixture (Jan 8 collision BCN)."""
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    by_id = {r["case_id"]: r for r in rows}

    assert "IN-002/2024" in by_id, "Expected IN-002/2024 in 2024 listing"
    row = by_id["IN-002/2024"]

    assert row["event_class"] == "Serious incident"
    assert row["pdf_url_es"] is not None
    assert row["pdf_url_es"].startswith("https://")
    assert row["pdf_url_es"].endswith(".pdf")
    assert row["pdf_url_en"] is not None
    assert row["pdf_url_en"].startswith("https://")
    assert row["date_of_occurrence"] == "2024-01-08"
    assert row["location"] is not None


def test_parse_listing_2024_accident_row():
    """A-005/2024 is an accident row."""
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    by_id = {r["case_id"]: r for r in rows}

    assert "A-005/2024" in by_id
    row = by_id["A-005/2024"]
    assert row["event_class"] == "Accident"


def test_parse_listing_2024_pdf_urls_absolute():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    for row in rows:
        for key in ("pdf_url_es", "pdf_url_en"):
            val = row[key]
            if val is not None:
                assert val.startswith("https://"), f"{key} not absolute: {val!r}"
                assert val.endswith(".pdf"), f"{key} not a .pdf: {val!r}"


def test_parse_listing_2024_has_en_pdfs():
    """Several 2024 rows have EN translation PDFs."""
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    en_rows = [r for r in rows if r["pdf_url_en"] is not None]
    assert len(en_rows) >= 3, (
        f"Expected ≥3 rows with EN PDF in 2024, got {len(en_rows)}"
    )


def test_parse_listing_2024_date_iso_format():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for row in rows:
        if row["date_of_occurrence"] is not None:
            assert iso_re.match(row["date_of_occurrence"]), (
                f"Bad ISO date: {row['date_of_occurrence']!r}"
            )


def test_parse_listing_2024_no_duplicates():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids)), "Duplicate case_ids in 2024 listing"


def test_parse_listing_2024_title_nonempty():
    html = _fixture("ciaiac_year_2024.html")
    rows = ciaiac.parse_listing(html)
    for row in rows:
        assert row["title"], f"Empty title for {row['case_id']}"


# ──────────────────────────────────────────────
# parse_listing — 2008 fixture (old PDF URL scheme)
# ──────────────────────────────────────────────

def test_parse_listing_2008_returns_rows():
    html = _fixture("ciaiac_year_2008.html")
    rows = ciaiac.parse_listing(html)
    assert len(rows) >= 10, f"Expected ≥10 rows from 2008 listing, got {len(rows)}"


def test_parse_listing_2008_case_id_format():
    html = _fixture("ciaiac_year_2008.html")
    rows = ciaiac.parse_listing(html)
    for row in rows:
        assert CASE_ID_RE.match(row["case_id"]), (
            f"Invalid case_id: {row['case_id']!r}"
        )


def test_parse_listing_2008_old_pdf_scheme():
    """Old URL scheme: recursos_mfom/YYYY_NNN_a.pdf (no /comodin/recursos/)."""
    html = _fixture("ciaiac_year_2008.html")
    rows = ciaiac.parse_listing(html)
    by_id = {r["case_id"]: r for r in rows}

    assert "A-001/2008" in by_id, "Expected A-001/2008 in 2008 listing"
    row = by_id["A-001/2008"]
    assert row["pdf_url_es"] is not None
    assert row["pdf_url_es"].startswith("https://")
    assert "recursos_mfom" in row["pdf_url_es"]


def test_parse_listing_2008_has_en_pdfs():
    html = _fixture("ciaiac_year_2008.html")
    rows = ciaiac.parse_listing(html)
    en_rows = [r for r in rows if r["pdf_url_en"] is not None]
    assert len(en_rows) >= 3, (
        f"Expected ≥3 rows with EN PDF in 2008, got {len(en_rows)}"
    )


# ──────────────────────────────────────────────
# Minimal synthetic HTML guard tests
# ──────────────────────────────────────────────

def test_parse_listing_skips_rows_without_case_id():
    html = """
    <div><h2>1 de enero de 2024. Aeronave CESSNA 172, matrícula EC-AAA. Madrid. Sin referencia</h2>
    <ul class='listado_generico'><li class='enlace_pdf'><a href='https://example.com/x.pdf' title='Enlace a un archivo pdf'>Informe final</a></li></ul></div>
    """
    rows = ciaiac.parse_listing(html)
    assert rows == [], f"Expected empty list, got {rows}"


def test_parse_listing_synthetic_complete():
    html = """
    <div><h2>15 de marzo de 2023. Aeronave AIRBUS A320, matrícula EC-XYZ. Aeropuerto de Madrid (Madrid). Ref. A-042/2023</h2>
    <ul class='listado_generico'>
      <li class='enlace_pdf'><a href='https://www.transportes.gob.es/recursos_mfom/comodin/recursos/a-042-2023_informe-final_nm.pdf' target='_blank' title='Enlace a un archivo pdf' >Informe final</a></li>
      <li class='enlace_pdf'><a href='https://www.transportes.gob.es/recursos_mfom/comodin/recursos/a-042-2023_final-report_nm.pdf' target='_blank' title='Enlace a un archivo pdf. Enlace en Inglés' ><span class='english'></span>Final report</a></li>
    </ul></div>
    """
    rows = ciaiac.parse_listing(html)
    assert len(rows) == 1
    row = rows[0]
    assert row["case_id"] == "A-042/2023"
    assert row["event_class"] == "Accident"
    assert row["date_of_occurrence"] == "2023-03-15"
    assert row["aircraft"] == "AIRBUS A320"
    assert row["registration"] == "EC-XYZ"
    assert row["location"] == "Aeropuerto de Madrid (Madrid)"
    assert row["pdf_url_es"] is not None
    assert row["pdf_url_en"] is not None


def test_parse_listing_in_prefix_gives_serious_incident():
    html = """
    <div><h2>10 de junio de 2023. Aeronave BOEING 737, matrícula EI-ABC. Sevilla (Sevilla). Ref. IN-015/2023</h2>
    <ul class='listado_generico'>
      <li class='enlace_pdf'><a href='https://www.transportes.gob.es/recursos_mfom/comodin/recursos/in-015-2023_informe-final_nm.pdf' target='_blank' title='Enlace a un archivo pdf' >Informe final</a></li>
    </ul></div>
    """
    rows = ciaiac.parse_listing(html)
    assert len(rows) == 1
    assert rows[0]["event_class"] == "Serious incident"
