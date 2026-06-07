from bea_ingest import govuk
from tests.conftest import FakeResp


def test_slug_from_link():
    assert govuk.slug_from_link("/aaib-reports/aaib-investigation-to-leonardo-aw139-g-cimu") == \
        "aaib-investigation-to-leonardo-aw139-g-cimu"
    assert govuk.slug_from_link("/aaib-reports/foo/") == "foo"


def test_pick_main_pdf_skips_glossary_and_non_pdf():
    atts = [
        {"content_type": "text/html", "title": "Web", "url": "u0"},
        {"content_type": "application/pdf", "title": "Glossary of abbreviations", "url": "u1"},
        {"content_type": "application/pdf", "title": "Leonardo AW139, G-CIMU", "url": "u2"},
    ]
    assert govuk.pick_main_pdf(atts)["url"] == "u2"
    assert govuk.pick_main_pdf([]) is None
    assert govuk.pick_main_pdf([{"content_type": "application/pdf", "title": "Glossary", "url": "g"}]) is None


def test_iter_search_paginates_until_total(make_client):
    def page(url, params):
        start = params["start"]
        if start == 0:
            return FakeResp(json_data={"total": 3, "results": [
                {"link": "/aaib-reports/a", "title": "A", "public_timestamp": "2026-01-02"},
                {"link": "/aaib-reports/b", "title": "B", "public_timestamp": "2026-01-01"},
            ]})
        return FakeResp(json_data={"total": 3, "results": [
            {"link": "/aaib-reports/c", "title": "C", "public_timestamp": "2025-12-31"},
        ]})
    client = make_client({govuk.SEARCH_URL: page})
    slugs = [govuk.slug_from_link(r["link"]) for r in govuk.iter_search(client, page_size=2)]
    assert slugs == ["a", "b", "c"]


def test_get_content_hits_content_api(make_client):
    client = make_client({
        f"{govuk.CONTENT_URL}/aaib-reports/x": FakeResp(json_data={"details": {"body": "<p>hi</p>"}}),
    })
    assert govuk.get_content(client, "x")["details"]["body"] == "<p>hi</p>"
