"""Offline tests for ciaado_ingest.ciaado using saved live HTML fixtures."""
import os
import re

import pytest

from ciaado_ingest import ciaado

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
CASE_ID_RE = re.compile(r"^CIAA-\d{3}(?:-\d{4})?$")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


# ── _normalize_case_id ────────────────────────────────────────────────────

def test_normalize_collapses_space_around_dash():
    assert ciaado._normalize_case_id("CIAA 101 - 2019") == "CIAA-101-2019"
    assert ciaado._normalize_case_id("CIAA-101 -2019") == "CIAA-101-2019"
    assert ciaado._normalize_case_id("CIAA 101-2019") == "CIAA-101-2019"


def test_normalize_collapses_space_around_slash():
    assert ciaado._normalize_case_id("CIAA 116 / 24") == "CIAA-116/24"


def test_normalize_uppercases():
    assert ciaado._normalize_case_id("ciaa 101 2019") == "CIAA-101-2019"


def test_normalize_none_passthrough():
    assert ciaado._normalize_case_id(None) is None
    assert ciaado._normalize_case_id("") == ""


# ── make_case_id ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("Informe Final CIAA caso 101-2019 - HI-878", "CIAA-101-2019"),
    ("Informe Final caso 098-2019 - N-9956K", "CIAA-098-2019"),
    ("Informe Final Caso 095-18 HI-191", "CIAA-095-2018"),
    ("Informe Final Caso 022 HI-432", "CIAA-022"),  # no year token
    ("Declaracion Provisional caso - 116-2024 N206BH (2025)", "CIAA-116-2024"),
    ("Informe Preliminar del Caso CIAA-117-25 matricula N695PG", "CIAA-117-2025"),
    ("Informe Preliminar caso CIAA 121-2025", "CIAA-121-2025"),
    ("Informe Final Caso 001-08 HI-653", "CIAA-001-2008"),
])
def test_make_case_id(title, expected):
    assert ciaado.make_case_id(title) == expected


def test_make_case_id_2digit_year_expansion():
    # 18 -> 2018, 08 -> 2008 (both <= ceil 27)
    assert ciaado.make_case_id("caso 090-18 N2451J") == "CIAA-090-2018"
    assert ciaado.make_case_id("caso 003-08 HI740") == "CIAA-003-2008"


def test_make_case_id_none_on_junk():
    assert ciaado.make_case_id("Bienvenido a la CIAA") is None
    assert ciaado.make_case_id("") is None
    assert ciaado.make_case_id(None) is None


# ── iter_subcategory_urls ─────────────────────────────────────────────────

def test_iter_subcategory_urls_returns_years():
    html = _fixture("ciaado_top_informes.html")
    urls = ciaado.iter_subcategory_urls(html)
    assert isinstance(urls, list)
    assert len(urls) >= 10, f"expected >=10 year subcats, got {len(urls)}"


def test_iter_subcategory_urls_all_absolute_and_no_top():
    html = _fixture("ciaado_top_informes.html")
    urls = ciaado.iter_subcategory_urls(html)
    for u in urls:
        assert u.startswith("https://ciaa.gob.do/index.php/informesf/category/")
        assert "19-informes" not in u
        assert "29-informes-preliminares" not in u
        assert "40-declaraciones-provisionales" not in u


def test_iter_subcategory_urls_no_duplicates():
    html = _fixture("ciaado_top_informes.html")
    urls = ciaado.iter_subcategory_urls(html)
    assert len(urls) == len(set(urls))


def test_iter_subcategory_urls_includes_known_year():
    html = _fixture("ciaado_top_informes.html")
    urls = ciaado.iter_subcategory_urls(html)
    assert any(u.endswith("/36-2016") for u in urls)


# ── parse_listing — 2016 fixture (15 rows) ────────────────────────────────

def test_parse_listing_2016_returns_rows():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html, "https://ciaa.gob.do/x/36-2016")
    assert len(rows) >= 14, f"expected >=14 rows from 2016, got {len(rows)}"


def test_parse_listing_2016_case_id_format():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"bad case_id {r['case_id']!r}"


def test_parse_listing_2016_pdf_urls_are_phoca_downloads():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    for r in rows:
        assert r["pdf_url"].startswith("https://ciaa.gob.do/")
        assert "?download=" in r["pdf_url"]


def test_parse_listing_2016_no_duplicates():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    ids = [r["case_id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_parse_listing_2016_titles_nonempty():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    for r in rows:
        assert r["title"]


def test_parse_listing_2016_event_class_final():
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    assert all(r["event_class"] == "Final report" for r in rows)


def test_parse_listing_2016_registration_extracted():
    """Most 2016 rows carry an HI-xxx / N-xxx registration."""
    html = _fixture("ciaado_year_2016.html")
    rows = ciaado.parse_listing(html)
    with_reg = [r for r in rows if r["registration"]]
    assert len(with_reg) >= 10, f"expected >=10 regs, got {len(with_reg)}"


def test_parse_listing_2016_report_url_carried():
    html = _fixture("ciaado_year_2016.html")
    url = "https://ciaa.gob.do/index.php/informesf/category/36-2016"
    rows = ciaado.parse_listing(html, url)
    assert all(r["report_url"] == url for r in rows)


# ── parse_listing — 2019 fixture (mixed title forms) ──────────────────────

def test_parse_listing_2019_known_row():
    html = _fixture("ciaado_year_2019.html")
    rows = ciaado.parse_listing(html)
    by_id = {r["case_id"]: r for r in rows}
    assert "CIAA-101-2019" in by_id
    row = by_id["CIAA-101-2019"]
    assert row["registration"] == "HI-878"
    assert "?download=157" in row["pdf_url"]
    assert row["event_class"] == "Final report"


def test_parse_listing_2019_filename_from_alt():
    html = _fixture("ciaado_year_2019.html")
    rows = ciaado.parse_listing(html)
    with_fn = [r for r in rows if r["filename"]]
    assert with_fn, "expected at least one filename from alt= attribute"
    for r in with_fn:
        assert r["filename"].lower().endswith(".pdf")


# ── parse_listing — provisional declarations 2025 fixture ─────────────────

def test_parse_listing_decl_event_class_provisional():
    html = _fixture("ciaado_year_2025_decl.html")
    rows = ciaado.parse_listing(html)
    assert rows, "expected provisional-declaration rows"
    assert all(r["event_class"] == "Provisional declaration" for r in rows)


def test_parse_listing_decl_case_ids_valid():
    html = _fixture("ciaado_year_2025_decl.html")
    rows = ciaado.parse_listing(html)
    for r in rows:
        assert CASE_ID_RE.match(r["case_id"]), f"bad case_id {r['case_id']!r}"


def test_parse_listing_skips_rows_without_case_id():
    html = (
        '<div class="attachment__container_item">'
        '<div class="the-caption"><div class="title">Bienvenida del director</div></div>'
        '<a class="btn-descargar" href="/x?download=9:bienvenida">Descargar</a></div>'
    )
    rows = ciaado.parse_listing(html)
    assert rows == []


def test_parse_listing_synthetic_complete():
    html = (
        '<div class="attachment__container_item"><div class="the-icon">'
        '<div class="icon float-left" alt=informes101.pdf></div></div>'
        '<div class="the-caption"><div class="title">Informe Final CIAA caso 101-2019 - HI-878</div></div>'
        '<div class="the-btn"><a class="btn-descargar" '
        'href="/index.php/informesf/category/20-2019?download=157:informe-final-101">Descargar</a>'
        '</div></div>'
    )
    rows = ciaado.parse_listing(html, "https://ciaa.gob.do/index.php/informesf/category/20-2019")
    assert len(rows) == 1
    r = rows[0]
    assert r["case_id"] == "CIAA-101-2019"
    assert r["registration"] == "HI-878"
    assert r["event_class"] == "Final report"
    assert r["filename"] == "informes101.pdf"
    assert r["pdf_url"] == (
        "https://ciaa.gob.do/index.php/informesf/category/20-2019?download=157:informe-final-101"
    )
