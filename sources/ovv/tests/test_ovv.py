from ovv_ingest import ovv


def test_parse_listing(listing_html):
    rows = ovv.parse_listing(listing_html)
    assert len(rows) >= 5
    slugs = {r["slug"] for r in rows}
    assert "passenger-information-flight-mh17" in slugs
    assert all(r["url"].startswith("https://onderzoeksraad.nl/en/onderzoek/")
               for r in rows)


def test_parse_detail_docs_ranked(detail_html):
    d = ovv.parse_detail(detail_html)
    assert d["title"]
    assert len(d["doc_urls"]) >= 3
    # the main report must outrank appendix/brochure/recommendations
    first = d["doc_urls"][0].lower()
    assert "report_mh17_passengerinformation-pdf" in first
    assert not any(x in first for x in ("appendix", "brochure", "aanbevelingen"))


def test_rank_docs_en_main_first():
    docs = [
        "https://onderzoeksraad.nl/aaaa11112222rapport_x_appendix-pdf/",
        "https://onderzoeksraad.nl/bbbb11112222rapport_x-pdf/",
        "https://onderzoeksraad.nl/cccc11112222rapport_x_en-pdf/",
    ]
    ranked = ovv.rank_docs(docs)
    assert "x_en-pdf" in ranked[0]
    assert "appendix" in ranked[-1]


def test_doc_lang():
    assert ovv.doc_lang("https://onderzoeksraad.nl/ab12report_x_en-pdf/") == "en"
    assert ovv.doc_lang("https://onderzoeksraad.nl/ab12rapport_x-pdf/") == "nl"


def test_parse_detail_summary(detail_html):
    d = ovv.parse_detail(detail_html)
    assert d["summary"] and len(d["summary"]) > 120


def test_parse_detail_empty():
    d = ovv.parse_detail("<html><h1>X</h1></html>")
    assert d["doc_urls"] == []
    assert d["title"] == "X"


def test_doc_re_matches_bare_and_hash_slugs():
    html = ('<a href="https://onderzoeksraad.nl/rapport_taxibaan_en_web-pdf/">a</a>'
            '<a href="https://onderzoeksraad.nl/f95ffc3669c4report_mh17-pdf/">b</a>'
            '<a href="https://onderzoeksraad.nl/en/onderzoek/not-a-doc/">c</a>')
    d = ovv.parse_detail("<h1>X</h1>" + html)
    assert len(d["doc_urls"]) == 2
    assert d["doc_urls"][0].endswith("rapport_taxibaan_en_web-pdf/")  # EN main first
