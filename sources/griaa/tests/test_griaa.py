import os
import re

from griaa_ingest import griaa

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


# ── URL helpers ──

def test_year_url():
    assert griaa.year_url(2008) == "https://www.aerocivil.gov.co/investigacion/Accidentes/?inicio=2008&fin=2008"


def test_iter_year_urls_newest_first_covers_range():
    urls = griaa.iter_year_urls(2000, 2010)
    assert urls[0].endswith("?inicio=2010&fin=2010")
    assert urls[-1].endswith("?inicio=2000&fin=2000")
    assert len(urls) == 11


def test_iter_year_urls_default_reaches_pre2012():
    urls = griaa.iter_year_urls()
    assert any("inicio=1998" in u for u in urls)   # oldest year reachable
    assert any("inicio=2008" in u for u in urls)   # pre-2012 depth
    assert any(f"inicio={griaa.YEAR_MAX}" in u for u in urls)


# ── listing parse: 2008 (per-year, COL-YY-NN-GIA + scanned finals) ──

def test_parse_listing_2008_rows():
    rows = griaa.parse_listing(_read("griaa_year_2008.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "COL-08-31-GIA" in by_id

    r = by_id["COL-08-31-GIA"]
    assert r["date_of_occurrence"] == "2008-12-12"
    assert r["event_class"] == "Accidente"
    assert r["location"] == "Acandí"
    assert r["registration"] == "HK4235"
    assert r["aircraft"] == "L-410UVP-E"
    assert r["pdf_url_es"].endswith(".pdf")
    assert r["pdf_url_es"].startswith("https://www.aerocivil.gov.co/")
    assert r["pdf_url_en"] is None


def test_parse_listing_2008_prefers_final_pdf():
    rows = griaa.parse_listing(_read("griaa_year_2008.html"))
    r = next(x for x in rows if x["case_id"] == "COL-08-31-GIA")
    # filename of the chosen PDF must be the Final report, not a Prelim
    assert "final" in r["pdf_url_es"].lower()


def test_parse_listing_2008_giab_suffix_distinct_from_gia():
    rows = griaa.parse_listing(_read("griaa_year_2008.html"))
    ids = {r["case_id"] for r in rows}
    # both the GIA and the GIAB variant must survive as distinct case_ids
    assert "COL-08-03-GIA" in ids
    assert "COL-08-03-GIAB" in ids


def test_parse_listing_incidente_grave_class():
    rows = griaa.parse_listing(_read("griaa_year_2008.html"))
    r = next(x for x in rows if x["case_id"] == "COL-08-03-GIA")
    assert r["event_class"] == "Incidente grave"


# ── listing parse: index (recent DIACC) ──

def test_parse_listing_index_diacc_rows():
    rows = griaa.parse_listing(_read("griaa_index.html"))
    by_id = {r["case_id"]: r for r in rows}
    assert "COL-26-15-DIACC" in by_id
    r = by_id["COL-26-15-DIACC"]
    assert r["date_of_occurrence"] == "2026-03-13"
    assert r["registration"] == "EJC2150"
    assert "final" in r["pdf_url_es"].lower()


def test_parse_listing_index_prelim_only_row():
    """A DIACC row with only a preliminary PDF still yields that PDF."""
    rows = griaa.parse_listing(_read("griaa_index.html"))
    r = next(x for x in rows if x["case_id"] == "COL-26-10-DIACC")
    assert "prelim" in r["pdf_url_es"].lower()


def test_parse_listing_case_ids_normalised():
    rows = griaa.parse_listing(_read("griaa_index.html"))
    for r in rows:
        assert r["case_id"] == r["case_id"].upper()
        assert "  " not in r["case_id"]
        assert " - " not in r["case_id"]


def test_parse_listing_pdf_urls_percent_encoded():
    rows = griaa.parse_listing(_read("griaa_year_2008.html"))
    # spaces in filenames must be encoded, never raw
    assert all(" " not in r["pdf_url_es"] for r in rows)


def test_parse_listing_skips_rows_without_pdf():
    html = "<table><tr><td>COL-99-01-GIA</td><td>01/01/2099</td><td>Accidente</td><td>X</td><td>Op</td><td>HK9</td><td>C172</td><td>RE</td></tr></table>"
    assert griaa.parse_listing(html) == []


def test_parse_listing_dedups_case_ids():
    one = '<tr><td>COL-08-31-GIA</td><td>12/12/2008</td><td>Accidente</td><td>Acandí</td><td>Op</td><td>HK4235</td><td>L410</td><td>RE</td><td><a class="document-link" href="/x Final.pdf"></a></td></tr>'
    html = "<table>" + one + one + "</table>"
    rows = griaa.parse_listing(html)
    assert len(rows) == 1
