from tsib_ingest import tsib


# ── listing parse (inline RSC JSON) ──────────────────────────────────────────

def test_parse_listing_live_fixture(listing_html):
    items = tsib.parse_listing(listing_html)
    assert len(items) == 100  # full catalogue from one fetch
    urls = [it["pdf_url"] for it in items]
    assert len(set(urls)) == 100  # deduped, all unique
    assert all(u.startswith("https://isomer-user-content.by.gov.sg/")
               for u in urls)
    assert all(u.endswith(".pdf") for u in urls)


def test_parse_listing_fields(listing_html):
    items = tsib.parse_listing(listing_html)
    first = items[0]
    # 19 May 2025 Boeing B737-800 (9M-MLL) runway incursion
    assert first["event_date"] == "2025-05-19"
    assert first["report_kind"] == "Incident"
    assert first["registration"] == "9M-MLL"  # from filename/title
    assert first["page_url"] == tsib.LISTING_URL


def test_parse_listing_has_accidents_and_dates(listing_html):
    items = tsib.parse_listing(listing_html)
    kinds = {it["report_kind"] for it in items}
    assert kinds == {"Accident", "Incident"}
    dated = [it for it in items if it["event_date"]]
    assert len(dated) >= 90
    # dates span the catalogue era 2009..2025
    years = {it["event_date"][:4] for it in dated}
    assert "2009" in years and "2025" in years


def test_parse_listing_strips_pdf_size_hint(listing_html):
    items = tsib.parse_listing(listing_html)
    assert all(not (it["title"] or "").rstrip().endswith("]")
               for it in items)


# ── aria-label fallback parse ────────────────────────────────────────────────

def test_parse_anchor_label_basic():
    m = tsib.parse_anchor_label(
        "19 May 2025 Status Past Reports Boeing B737-800 Incident "
        "(opens in new tab)")
    assert m["event_date"] == "2025-05-19"
    assert m["report_kind"] == "Incident"
    assert m["title"] == "Boeing B737-800"


def test_parse_anchor_label_accident_and_reg():
    m = tsib.parse_anchor_label(
        "6 September 2024 Status Past Reports Boeing B787-9 (9V-OJD) "
        "Accident (opens in new tab)")
    assert m["event_date"] == "2024-09-06"
    assert m["report_kind"] == "Accident"
    assert m["registration"] == "9V-OJD"


def test_parse_listing_anchor_fallback():
    # No inline JSON → fall back to rendered <a aria-label>.
    html = (
        '<a target="_blank" aria-label="21 May 2024 Status Past Reports '
        'Boeing B777-300ER Accident (opens in new tab)" '
        'href="https://isomer-user-content.by.gov.sg/287/abc/'
        'b777-final.pdf">x</a>')
    items = tsib.parse_listing(html)
    assert len(items) == 1
    assert items[0]["event_date"] == "2024-05-21"
    assert items[0]["report_kind"] == "Accident"
    assert items[0]["pdf_url"].endswith("b777-final.pdf")


# ── clamp-stop helper ────────────────────────────────────────────────────────

def test_first_pdf_url(listing_html):
    url = tsib.first_pdf_url(listing_html)
    assert url and url.endswith(".pdf")


def test_first_pdf_url_empty():
    assert tsib.first_pdf_url("<html>no reports</html>") is None


# ── case_id extraction (both eras + canonicalisation + fallback) ─────────────

def test_extract_case_id_new_era(new_pdf_head):
    assert tsib.extract_case_id(new_pdf_head) == "tib-aai-cas-246"


def test_extract_case_id_old_era(old_pdf_head):
    # 'AIB/AAI/CAS.058' — leading 'A' must NOT be clipped
    assert tsib.extract_case_id(old_pdf_head) == "aib-aai-cas-058"


def test_extract_case_id_canonicalisation():
    assert tsib.extract_case_id("ref TIB/AAI/CAS.246 page") == "tib-aai-cas-246"
    assert tsib.extract_case_id("x AIB/AAI/CAS.087 y") == "aib-aai-cas-087"


def test_extract_case_id_only_first_4000_chars():
    txt = ("x" * 4100) + "TIB/AAI/CAS.999"
    assert tsib.extract_case_id(txt) is None


def test_extract_case_id_absent():
    assert tsib.extract_case_id("no formal id in this text") is None


def test_uuid_from_url():
    u = ("https://isomer-user-content.by.gov.sg/287/"
         "850c7511-0531-4d04-a578-e8b45c469fc5/report.pdf")
    assert tsib.uuid_from_url(u) == "850c7511-0531-4d04-a578-e8b45c469fc5"
    assert tsib.uuid_from_url("https://x/287/notauuid/r.pdf") is None


def test_make_case_id_uuid_fallback():
    u = ("https://isomer-user-content.by.gov.sg/287/"
         "850c7511-0531-4d04-a578-e8b45c469fc5/r.pdf")
    assert tsib.make_case_id("no id here", u) == \
        "tsib-850c7511-0531-4d04-a578-e8b45c469fc5"


def test_make_case_id_collision_suffix(new_pdf_head):
    base = tsib.extract_case_id(new_pdf_head)
    assert tsib.make_case_id(new_pdf_head, "u", taken={base}) == base + "-2"
    assert tsib.make_case_id(new_pdf_head, "u",
                             taken={base, base + "-2"}) == base + "-3"


# ── misc helpers ─────────────────────────────────────────────────────────────

def test_percent_encode_spaces_and_parens():
    u = ("https://isomer-user-content.by.gov.sg/287/x/"
         "20260401 B737 (9M-MLL) Final Report.pdf")
    enc = tsib.percent_encode(u)
    assert " " not in enc
    assert "%20" in enc
    assert enc.endswith("Report.pdf")
