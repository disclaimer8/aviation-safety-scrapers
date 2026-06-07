from shk_ingest import shk


def test_parse_sitemap(sitemap_xml):
    urls = shk.parse_sitemap(sitemap_xml)
    assert len(urls) == 15
    assert all("/search-investigation/aviation/" in u for u in urls)


def test_case_id_strips_migration_date():
    url = ("https://shk.se/engelska/x/search-investigation/aviation/"
           "2023-11-22-helicopter-accident-to-se-hlk-at-joesjo")
    assert shk.case_id_from_url(url) == "helicopter-accident-to-se-hlk-at-joesjo"


def test_case_id_collision_suffix():
    url = "https://shk.se/x/aviation/2023-11-22-same-slug"
    taken = {"same-slug"}
    assert shk.case_id_from_url(url, taken=taken) == "same-slug-2"


def test_parse_detail_completed(detail_completed):
    d = shk.parse_detail(detail_completed)
    assert d["title"] == "Helicopter accident to SE-HLK at Joesjö"
    assert d["registration"] == "SE-HLK"
    assert d["event_date"] == "2004-07-07"  # display text, NOT the UTC attr
    assert d["diarienummer"] == "L-22/04"
    assert d["rl_number"] == "RL 2005:08"
    assert d["pdf_href"].endswith("rl2005_08e.pdf")
    assert d["lang"] == "en"  # e.pdf suffix = full English report
    assert d["report_kind"] == "Final"


def test_parse_detail_ongoing(detail_ongoing):
    d = shk.parse_detail(detail_ongoing)
    assert d["pdf_href"] is None
    assert d["title"] and "SE-HSX" in d["title"]
    assert d["registration"] == "SE-HSX"


def test_pick_pdf_preference():
    links = [
        ("/download/a/1/rl2022_03.pdf", "Final report RL 2022:03"),
        ("/download/b/2/RL2022_03-Summary.pdf", "Summary in English"),
    ]
    href, lang, kind = shk.pick_pdf(links)
    assert href.endswith("Summary.pdf")
    assert lang == "en-summary"

    links.append(("/download/c/3/rl2022_03e.pdf", "Final report English"))
    href, lang, _ = shk.pick_pdf(links)
    assert href.endswith("e.pdf")
    assert lang == "en"


def test_pick_pdf_swedish_only():
    href, lang, _ = shk.pick_pdf([("/download/a/1/MBF.pdf", "Slutrapport")])
    assert href.endswith("MBF.pdf")
    assert lang == "sv"


def test_pick_pdf_empty():
    assert shk.pick_pdf([]) == (None, None, None)
