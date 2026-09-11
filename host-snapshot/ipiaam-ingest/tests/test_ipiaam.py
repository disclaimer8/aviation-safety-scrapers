# tests/test_ipiaam.py
"""Unit tests for ipiaam_ingest/ipiaam.py — pure-function tests, no network."""
import pytest
from ipiaam_ingest import ipiaam as src


# ── make_case_id ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('ref,expected', [
    ('001/INCID-A/IPIAAM/2025', 'ipiaam-001-incid-a-2025'),
    ('002/INCID-A/IPIAAM/2025', 'ipiaam-002-incid-a-2025'),
    ('003/INCID-A/IPIAAM/2025', 'ipiaam-003-incid-a-2025'),
    ('01/INCID-A/IPIAAM/2023',  'ipiaam-01-incid-a-2023'),
    ('02/INCID-A/IPIAAM/2018',  'ipiaam-02-incid-a-2018'),
    ('01/INCID-A/IPIAAM/2018',  'ipiaam-01-incid-a-2018'),
    ('001/INCID-A/IPIAAM/2021', 'ipiaam-001-incid-a-2021'),
    ('002/INCID/2020',          'ipiaam-002-incid-2020'),
])
def test_make_case_id(ref, expected):
    assert src.make_case_id(ref) == expected


def test_make_case_id_no_ipiaam_duplicate():
    """The 'ipiaam' fragment inside the ref should NOT appear twice."""
    cid = src.make_case_id('001/INCID-A/IPIAAM/2025')
    assert cid.count('ipiaam') == 1


# ── extract_event_date ────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected', [
    # DD/MM/YYYY after Data / Date label (bilingual cover block)
    ('Data / Date\n17/04/2025\n17:20 UTC\nGVNP', '2025-04-17'),
    # DD/MM/YYYY in text (no label)
    ('Ocorrência\n16/09/2020\n08:38 UTC', '2020-09-16'),
    # DD/MM/YYYY with two-digit year-like numbers after — picks first
    ('Data / Date\n22/07/2023\n00:35', '2023-07-22'),
    # No date present
    ('No date information here.', None),
])
def test_extract_event_date(text, expected):
    assert src.extract_event_date(text) == expected


def test_extract_event_date_pt_long():
    """Portuguese long-form date after label."""
    text = 'Data de Ocorrência\n12 de abril de 2021\nFAIL'
    assert src.extract_event_date(text) == '2021-04-12'


# ── extract_registration ──────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected', [
    ('Matrícula / Registration\nD4 - CCL\n', 'D4-CCL'),
    ('Registration\nD4-CCF\n', 'D4-CCF'),
    ('Matrícula / Registration\nOE-FUB\n', 'OE-FUB'),
    ('Matrícula / Registration\nOK-SWM\n', 'OK-SWM'),
    ('Nothing useful here.', None),
])
def test_extract_registration(text, expected):
    assert src.extract_registration(text) == expected


# ── extract_fatalities ────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected', [
    # All-zero table (most events are incidents)
    ('Fatais / Fatal\n0\n0\n0', 0),
    ('Fatal\n0\n0\n0', 0),
    ('Fatais / Fatal\n1\n0\n0', 1),
    ('Fatais / Fatal\n0\n2\n1', 3),
    ('No fatality table.', None),
])
def test_extract_fatalities(text, expected):
    assert src.extract_fatalities(text) == expected


# ── detect_lang ───────────────────────────────────────────────────────────────

def test_detect_lang_en():
    text = 'Summary Report Investigation Aircraft Registration Operator Serious Incident'
    assert src.detect_lang(text) == 'en'


def test_detect_lang_pt_only():
    text = 'Relatório sumário sem corpo em inglês. Apenas texto português curto aqui.'
    assert src.detect_lang(text) == 'pt'


# ── parse_listing ─────────────────────────────────────────────────────────────

SAMPLE_HTML = """
<a href="https://www.ipiaam.cv/documento/opendoc/1762945457_en.pdf">Download</a>
<span>Report No. 002/INCID-A/IPIAAM/2025</span>
2025-11-12
<a href="https://www.ipiaam.cv/documento/opendoc/1758882394_en.pdf">Download</a>
<span>Report No. 003/INCID-A/IPIAAM/2025</span>
2025-09-26
<a href="https://www.ipiaam.cv/documento/opendoc/1612350817_en.pdf">Download</a>
<span>Aircraft serious incident summary report 002/INCID/2020</span>
2021-02-03
"""


def test_parse_listing_count():
    rows = src.parse_listing(SAMPLE_HTML)
    assert len(rows) == 3


def test_parse_listing_case_ids():
    rows = src.parse_listing(SAMPLE_HTML)
    cids = [r['case_id'] for r in rows]
    assert 'ipiaam-002-incid-a-2025' in cids
    assert 'ipiaam-003-incid-a-2025' in cids
    assert 'ipiaam-002-incid-2020' in cids


def test_parse_listing_no_duplicates():
    # Duplicate PDF URL in HTML should produce only one row.
    html = SAMPLE_HTML + SAMPLE_HTML
    rows = src.parse_listing(html)
    cids = [r['case_id'] for r in rows]
    assert len(cids) == len(set(cids))


def test_parse_listing_pub_dates():
    rows = src.parse_listing(SAMPLE_HTML)
    by_cid = {r['case_id']: r for r in rows}
    assert by_cid['ipiaam-002-incid-a-2025']['pub_date'] == '2025-11-12'
    assert by_cid['ipiaam-002-incid-2020']['pub_date'] == '2021-02-03'


def test_parse_listing_pdf_urls():
    rows = src.parse_listing(SAMPLE_HTML)
    urls = [r['pdf_url'] for r in rows]
    assert all(u.startswith('https://www.ipiaam.cv/') for u in urls)


# ── make_site_slug ────────────────────────────────────────────────────────────

def test_make_site_slug_normal():
    slug = src.make_site_slug('ATR 72-212A', 'D4-CCA', 'GVAC')
    assert slug.startswith('crash-')
    assert 'atr' in slug


def test_make_site_slug_empty():
    assert src.make_site_slug(None, None, None) == 'crash-ipiaam'
