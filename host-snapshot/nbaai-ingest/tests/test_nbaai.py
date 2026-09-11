import os
from nbaai_ingest import nbaai

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


# ── sitemap discovery ────────────────────────────────────────────────────────

def test_iter_enquiry_urls_from_fixture():
    urls = nbaai.iter_enquiry_urls(_read("enquiry_sitemap.xml"))
    assert len(urls) >= 50
    assert all(u.startswith("https://nbaai.gov.ua/enquiry/") and u.endswith("/") for u in urls)
    # listing root and paginated archives must be excluded
    assert "https://nbaai.gov.ua/enquiry/" not in urls
    assert not any("/enquiry/page/" in u for u in urls)


def test_iter_enquiry_urls_dedup_and_skips_pages():
    sm = (
        "<urlset>"
        "<url><loc>https://nbaai.gov.ua/enquiry/</loc></url>"
        "<url><loc>https://nbaai.gov.ua/enquiry/page/2/</loc></url>"
        "<url><loc>https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/</loc></url>"
        "<url><loc>https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/</loc></url>"
        "</urlset>"
    )
    urls = nbaai.iter_enquiry_urls(sm)
    assert urls == ["https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/"]


# ── intrinsic case_id ────────────────────────────────────────────────────────

def test_make_case_id_reg_and_date():
    assert nbaai.make_case_id("UR-KTB", "2019-10-21", "slug") == "NBAAI-UR-KTB-2019-10-21"


def test_make_case_id_reg_only():
    assert nbaai.make_case_id("N-918Y", None, "slug") == "NBAAI-N-918Y"


def test_make_case_id_slug_fallback():
    assert nbaai.make_case_id(None, None, "katastrofa-paraplana") == "NBAAI-KATASTROFA-PARAPLANA"


def test_normalize_case_id_idempotent():
    a = nbaai._normalize_case_id("ur ktb")
    assert a == "UR-KTB"
    assert nbaai._normalize_case_id(a) == a
    assert nbaai._normalize_case_id("UR__KTB--X") == "UR-KTB-X"


# ── detail-page parsing ──────────────────────────────────────────────────────

def test_parse_detail_html_only():
    url = "https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/"
    r = nbaai.parse_detail(_read("detail_html_only.html"), url)
    assert r["case_id"] == "NBAAI-UR-KTB-2019-10-21"
    assert r["registration"] == "UR-KTB"
    assert r["date_of_occurrence"] == "2019-10-21"
    assert r["pdf_url"] is None
    assert r["event_class"] == "Accident (fatal)"
    assert "R-44" in (r["aircraft"] or "")
    # clean Unicode Ukrainian narrative body captured
    assert len(r["narrative_html"]) > 100
    assert "катастроф" in r["narrative_html"].lower()


def test_parse_detail_with_pdf():
    url = "https://nbaai.gov.ua/enquiry/zaversheno-rozsliduvannya-katastrofy-litaka-l-410-ur-two/"
    r = nbaai.parse_detail(_read("detail_with_pdf.html"), url)
    assert r["registration"] == "UR-TWO"
    assert r["pdf_url"] == "https://nbaai.gov.ua/wp-content/uploads/2020/02/l-410_ur-two.pdf"
    assert r["case_id"].startswith("NBAAI-UR-TWO-")


def test_reg_from_slug_foreign_and_cyrillic_titles():
    # Cyrillic title (UR-СІС) but Latin slug (ur-sis) -> registration from slug
    url = "https://nbaai.gov.ua/enquiry/aviaczijna-podiya-katastrofa-z-litakom-an-12bk-ur-sis-poblyzu-aerodromu-kavala-greczka-respublika/"
    r = nbaai.parse_detail("<h1>Авіаційна подія (катастрофа) з літаком Ан-12Б UR-СІС</h1>", url)
    assert r["registration"] == "UR-SIS"


# ── Finding #1: depth-aware content-text extraction (nested <div>) ─────────────

def test_content_text_depth_aware_captures_full_narrative():
    """A nested <div> inside content-text must NOT truncate the narrative.

    The naive non-greedy regex stopped at the first nested </div> (717 chars),
    silently dropping the post-nested-div causal-factors section.  The
    depth-aware walker captures the COMPLETE element.
    """
    url = "https://nbaai.gov.ua/enquiry/zaversheno-rozsliduvannya-katastrofy-litaka-l-410-ur-two/"
    r = nbaai.parse_detail(_read("detail_with_pdf.html"), url)
    nh = r["narrative_html"]
    # full element, well past the old 717-char truncation point
    assert len(nh) > 1100
    # the causal section published AFTER the first nested </div> is now present
    assert "Згідно з висновками комісії" in nh
    assert "Надзвичайно погані метеоумови" in nh  # the causal-factors list
    # embedded Leaflet <script> map must be stripped, not leaked into the body
    assert "L.map" not in nh and "<script" not in nh.lower()


def test_extract_content_text_balanced_nested_divs():
    """Direct walker test: nested divs preserved, matching close found."""
    html = (
        '<header>x</header>'
        '<div class="col-md-7 content-text">'
        'A<p>one</p>'
        '<div class="inner">B nested</div>'
        'C tail after nested'
        '</div>'
        '<footer>FOOTER</footer>'
    )
    inner = nbaai._extract_content_text(html)
    assert "A" in inner and "B nested" in inner and "C tail after nested" in inner
    assert "FOOTER" not in inner  # stopped at the matching close, not past it


# ── Finding #2: registration mined from the narrative BODY (secondary) ─────────

def test_body_reg_populates_when_slug_has_no_latin_reg():
    """Cyrillic-titled report (no Latin reg in slug) -> reg from body; case_id
    stays slug-derived and intrinsic (body-reg must NOT feed case_id)."""
    url = (
        "https://nbaai.gov.ua/enquiry/"
        "aviaczijna-podiya-katastrofa-z-litakom-an-12bk-poblyzu-aerodromu-kavala/"
    )
    html = (
        "<h1>Авіаційна "
        "подія (катастрофа)</h1>"
        '<div class="col-md-7 content-text"><p>10.04.2017 '
        "літак Ан-12БК "
        "UR-СІС борт UR-15605 "
        "зазнав катастрофи.</p></div>"
    )
    r = nbaai.parse_detail(html, url)
    # slug carries NO Latin reg, so registration comes from the body mark
    assert r["registration"] == "UR-CIC"  # UR-СІС transliterated
    # case_id stays slug-derived (intrinsic) — NOT NBAAI-UR-CIC-...
    assert r["registration"] not in r["case_id"]
    assert r["case_id"].startswith("NBAAI-AVIACZIJNA")


def test_body_reg_does_not_override_slug_reg():
    """When the slug yields a Latin reg, body-reg must not change it."""
    url = "https://nbaai.gov.ua/enquiry/katastrofa-vertolota-r-44-ur-ktb/"
    html = (
        '<h1>x</h1><div class="content-text"><p>21.10.2019 '
        'UR-KTB and a stray UR-15605 in the body</p></div>'
    )
    r = nbaai.parse_detail(html, url)
    assert r["registration"] == "UR-KTB"  # from slug, not the stray body mark
    assert r["case_id"] == "NBAAI-UR-KTB-2019-10-21"


def test_reg_from_body_cyrillic_and_foreign():
    assert nbaai._reg_from_body("борт УР-15605") == "UR-15605"  # УР- prefix
    assert nbaai._reg_from_body("aircraft 4X-AVG operated by") == "4X-AVG"
    assert nbaai._reg_from_body("no reg present here at all") is None
