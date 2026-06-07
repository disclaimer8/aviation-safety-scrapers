from aaiube_ingest import aaiube

_ROW_MODERN = """
<table>
<thead><tr><th>Date of occurrence</th><th>Type of aircraft</th>
<th>Casualties</th><th>Location</th><th>Status</th></tr></thead>
<tbody>
<tr>
<td><p>12/09/2022</p></td>
<td><p>Grumman American AA-5B Tiger</p></td>
<td><p>None</p></td>
<td>Brussels FIR, class G</td>
<td><p><a href="/sites/default/files/documents/publications/2024/AAIU-2022-09-12-01-final.pdf">Final</a></p></td>
</tr>
</tbody>
</table>
""".strip()

_ROW_LEGACY = """
<table>
<tbody>
<tr>
<td>05/03/2009</td>
<td>Cessna 172</td>
<td>1 fatal</td>
<td>Antwerp</td>
<td><a href="/sites/default/files/documents/publications/2009/2009_01.pdf">Final report</a></td>
</tr>
</tbody>
</table>
""".strip()

_ROW_NO_PDF = """
<table>
<tbody>
<tr>
<td>20/07/2024</td>
<td>Beechcraft B200C</td>
<td>None</td>
<td>Latvian airspace</td>
<td><p>In progress</p></td>
</tr>
</tbody>
</table>
""".strip()


# ── case_id derivation ────────────────────────────────────────────────────────

def test_case_id_modern():
    url = "/sites/default/files/documents/publications/2024/AAIU-2022-09-12-01-final.pdf"
    assert aaiube.derive_case_id(url) == "aaiu-2022-09-12-01"


def test_case_id_modern_with_reupload_suffix():
    url = "/x/AAIU-2024-11-21-01-progress_0.pdf"
    assert aaiube.derive_case_id(url) == "aaiu-2024-11-21-01"


def test_case_id_legacy_year_seq():
    url = "/sites/default/files/documents/publications/2009/2009_01.pdf"
    assert aaiube.derive_case_id(url, year="2009") == "be-2009-2009_01".replace("_", "-")


def test_case_id_legacy_bare_number():
    url = "/sites/default/files/documents/publications/2011/10.pdf"
    assert aaiube.derive_case_id(url, year="2011") == "be-2011-10"


def test_case_id_legacy_aa_form():
    url = "/sites/default/files/documents/publications/2010/AA-7-1.pdf"
    assert aaiube.derive_case_id(url, year="2010") == "be-2010-aa-7-1"


def test_case_id_collision_suffix():
    taken = {"be-2011-10"}
    url = "/x/2011/10.pdf"
    assert aaiube.derive_case_id(url, year="2011", taken=taken) == "be-2011-10-2"


def test_case_id_modern_collision_suffix():
    taken = {"aaiu-2022-09-12-01"}
    url = "/x/AAIU-2022-09-12-01-final_0.pdf"
    assert aaiube.derive_case_id(url, taken=taken) == "aaiu-2022-09-12-01-2"


# ── PDF URL absolutise / encode ───────────────────────────────────────────────

def test_absolutise_relative():
    out = aaiube.absolutise_pdf_url("/sites/default/files/x/y.pdf")
    assert out == "https://mobilit.belgium.be/sites/default/files/x/y.pdf"


def test_absolutise_encodes_spaces():
    out = aaiube.absolutise_pdf_url(
        "/sites/default/files/documents/publications/2026/AAIU-2022-09-08-02 final.pdf"
    )
    assert " " not in out
    assert out.endswith("AAIU-2022-09-08-02%20final.pdf")


def test_absolutise_keeps_absolute():
    out = aaiube.absolutise_pdf_url("https://mobilit.belgium.be/a/b.pdf")
    assert out == "https://mobilit.belgium.be/a/b.pdf"


# ── kind / lang detection ─────────────────────────────────────────────────────

def test_detect_kind():
    assert aaiube.detect_kind("Final") == "Final"
    assert aaiube.detect_kind("Preliminary report") == "Preliminary"
    assert aaiube.detect_kind("Progress statement") == "Progress"
    assert aaiube.detect_kind("Interim") == "Interim"
    assert aaiube.detect_kind("AAIU-2022-09-12-01-final.pdf") == "Final"
    assert aaiube.detect_kind("something else") == "Final"  # default


def test_detect_lang_default_en():
    assert aaiube.detect_lang("AAIU-2022-09-12-01-final.pdf") == "en"


def test_detect_lang_fr_nl():
    assert aaiube.detect_lang("rapport-2010-fr.pdf") == "fr"
    assert aaiube.detect_lang("verslag-2010-nl.pdf") == "nl"


# ── table parse ───────────────────────────────────────────────────────────────

def test_parse_modern_row_all_columns():
    rows = aaiube.parse_listing(_ROW_MODERN)
    assert len(rows) == 1
    r = rows[0]
    assert r["date_of_occurrence"] == "2022-09-12"
    assert r["aircraft"] == "Grumman American AA-5B Tiger"
    assert r["casualties"] == "None"
    assert r["location"] == "Brussels FIR, class G"
    assert r["status"] == "Final"
    assert r["report_kind"] == "Final"
    assert r["lang"] == "en"
    assert r["pdf_url"].endswith("AAIU-2022-09-12-01-final.pdf")
    assert r["pdf_url"].startswith("https://mobilit.belgium.be/")


def test_parse_legacy_row():
    rows = aaiube.parse_listing(_ROW_LEGACY)
    assert len(rows) == 1
    r = rows[0]
    assert r["date_of_occurrence"] == "2009-03-05"
    assert r["aircraft"] == "Cessna 172"
    assert r["report_kind"] == "Final"


def test_parse_skips_rows_without_pdf():
    assert aaiube.parse_listing(_ROW_NO_PDF) == []


def test_parse_year_heading_supplies_year():
    page = (
        "<h3 class='accordion__title'>Reports occurrences 2011</h3>" + _ROW_LEGACY
    )
    rows = aaiube.parse_listing(page)
    assert rows[0]["year"] == "2011"


def test_parse_live_fixture_counts(listing_html):
    rows = aaiube.parse_listing(listing_html)
    # ~164 PDF-bearing rows on the live page.
    assert 150 <= len(rows) <= 180
    # every row has an absolute PDF url and unique-ish filenames
    assert all(r["pdf_url"].startswith("https://mobilit.belgium.be/") for r in rows)
    assert all(r["pdf_url"].lower().endswith(".pdf") for r in rows)
    # a healthy share carry modern AAIU refs
    modern = sum(1 for r in rows if "AAIU-" in r["pdf_url"].split("/")[-1])
    assert modern >= 20


def test_parse_live_fixture_case_ids_unique(listing_html):
    rows = aaiube.parse_listing(listing_html)
    taken = set()
    ids = []
    for r in rows:
        cid = aaiube.derive_case_id(r["pdf_url"], year=r["year"], taken=taken)
        taken.add(cid)
        ids.append(cid)
    assert len(ids) == len(set(ids))  # no collisions after suffixing
    # modern refs lowercased verbatim
    assert any(cid.startswith("aaiu-") for cid in ids)
    assert any(cid.startswith("be-") for cid in ids)
