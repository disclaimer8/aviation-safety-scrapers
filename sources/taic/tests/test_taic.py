import pathlib
FIXTURES = pathlib.Path(__file__).parent / "fixtures"
from taic_ingest import taic


# ── listing ───────────────────────────────────────────────────────────────────

def test_parse_listing_yields_all_12_cards_mixed_modes(listing_html):
    cards = taic.parse_listing(listing_html)
    assert len(cards) == 12
    prefixes = {c["case_id"][:2] for c in cards}
    # server ignores the aviation filter — all 3 modes present
    assert prefixes == {"AO", "MO", "RO"}


def test_parse_listing_card_fields(listing_html):
    cards = {c["case_id"]: c for c in taic.parse_listing(listing_html)}
    c = cards["AO-2025-016"]
    assert c["inquiry_url"] == "https://taic.org.nz/inquiry/ao-2025-016"
    assert c["title"].startswith("Bell 206L-3, collision with terrain")
    assert "Raetihi" in c["summary"]
    assert c["event_date"] == "2025-12-13"
    assert c["publish_date"] is None  # "Not yet published"
    assert c["pill"] == "In progress"


def test_parse_listing_empty_page(listing_empty_html):
    assert taic.parse_listing(listing_empty_html) == []


def test_is_aviation():
    assert taic.is_aviation("AO-2018-006")
    assert taic.is_aviation("ao-2018-006")
    assert not taic.is_aviation("MO-2026-201")
    assert not taic.is_aviation("RO-2026-104")
    assert not taic.is_aviation(None)


# ── inquiry: rich modern page ─────────────────────────────────────────────────

def test_parse_inquiry_rich_metadata(inquiry_rich_html):
    p = taic.parse_inquiry(inquiry_rich_html)
    assert p["registration"] == "ZK-HTB"
    assert "Robinson" in p["aircraft"]
    assert p["event_date"] == "2018-07-21"
    assert p["location"]
    assert p["operator"]


def test_parse_inquiry_rich_narrative_sections(inquiry_rich_html):
    p = taic.parse_inquiry(inquiry_rich_html)
    text = p["narrative_text"]
    assert len(text) > 50_000
    # section headings present, Māori subtitle stripped from headings
    assert "Executive summary" in text
    assert "Analysis" in text
    assert "Findings" in text


def test_parse_inquiry_rich_pdfs_site_local_only(inquiry_rich_html):
    p = taic.parse_inquiry(inquiry_rich_html)
    assert any("AO-2018-006" in u and "Final" in u for u in p["pdf_urls"])
    # external (squarespace) PDF must be excluded
    assert not any("squarespace" in u for u in p["pdf_urls"])
    # URLs are absolute and entity-unescaped
    assert all(u.startswith("https://taic.org.nz/") for u in p["pdf_urls"])


# ── inquiry: old thin page ────────────────────────────────────────────────────

def test_parse_inquiry_old_thin(inquiry_old_html):
    p = taic.parse_inquiry(inquiry_old_html)
    # no Details metadata on legacy pages
    assert p["registration"] is None
    assert p["aircraft"] is None
    # narrative is a stub (well under the HTML floor)
    assert len(p["narrative_text"] or "") < 1500
    # but the report PDF is linked
    assert any(u.endswith("95-009.pdf") for u in p["pdf_urls"])


# ── inquiry: in-progress page ─────────────────────────────────────────────────

def test_parse_inquiry_in_progress_thin_no_pdf(inquiry_in_progress_html):
    p = taic.parse_inquiry(inquiry_in_progress_html)
    assert len(p["narrative_text"] or "") < 1500
    assert p["pdf_urls"] == []


# ── helpers ───────────────────────────────────────────────────────────────────

def test_balanced_div_extraction():
    html = '<div class="a"><div>x</div><p>y</p></div><div>tail</div>'
    end = taic._balanced_div(html, 0)
    assert html[:end].endswith("</div>")
    assert "tail" not in html[:end]
    assert "y" in html[:end]


def test_html_to_text_paragraphs():
    out = taic._html_to_text("<p>One</p><p>Two&nbsp;&amp; three</p>")
    assert "One" in out and "Two & three" in out
    assert "<p>" not in out


def test_aircraft_field_serial_number_trimmed(inquiry_rich_html):
    p = taic.parse_inquiry(inquiry_rich_html)
    assert "serial" not in p["aircraft"].lower()
    assert p["aircraft"].startswith("Robinson")


def test_parse_listing_bigpipe_variant():
    # Drupal BigPipe cache-miss variant: cards arrive as JSON payloads in
    # <script type="application/vnd.drupal-ajax"> tags. The first backfill
    # saw these as "empty" pages and stopped at 21 rows (2026-06-04).
    html = (FIXTURES / "listing-bigpipe.html").read_text()
    cards = taic.parse_listing(html)
    assert len(cards) == 12
    assert any(c["case_id"].startswith("AO-") for c in cards)
    assert all(c["pill"] in ("Published", "In progress") for c in cards)
