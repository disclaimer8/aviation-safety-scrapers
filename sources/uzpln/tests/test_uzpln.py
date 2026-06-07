from uzpln_ingest import uzpln


# ── event-date parsing (ISO listing + dotted detail) ───────────────────────


def test_parse_event_date_iso():
    assert uzpln.parse_event_date("2025-07-29") == "2025-07-29"
    assert uzpln.parse_event_date(" 2003-01-06 ") == "2003-01-06"


def test_parse_event_date_dotted():
    assert uzpln.parse_event_date("2025.07.29") == "2025-07-29"
    assert uzpln.parse_event_date("  2003.02.22  ") == "2003-02-22"


def test_parse_event_date_unparseable():
    assert uzpln.parse_event_date("") is None
    assert uzpln.parse_event_date("bez data") is None
    assert uzpln.parse_event_date("2025-13-40") is None


# ── listing parse + stop-signal detection ──────────────────────────────────


def test_parse_listing_rows(listing_html):
    recs = uzpln.parse_listing(listing_html)
    assert len(recs) == 3
    by_id = {r["incident_id"]: r for r in recs}

    r = by_id["830"]
    assert r["report_number"] == "CZ-25-1428"
    assert r["event_date"] == "2025-07-29"
    assert r["report_kind"] == "Závěrečná zpráva"
    assert r["event_kind"] == "Letecká nehoda"
    assert "Ranské hory" in r["location"]
    assert r["detail_url"] == "https://uzpln.gov.cz/incident/830"

    # ICAO-style location.
    assert by_id["824"]["location"] == "LKNM"
    assert by_id["824"]["event_kind"] == "Vážný incident"


def test_parse_listing_blank_case_number(listing_html):
    # Old rows carry a BLANK Číslo zprávy → report_number None.
    by_id = {r["incident_id"]: r for r in uzpln.parse_listing(listing_html)}
    assert by_id["2"]["report_number"] is None
    assert by_id["2"]["event_date"] == "2003-02-22"


def test_stop_signal_empty_page(listing_empty_html):
    assert uzpln.parse_listing(listing_empty_html) == []
    assert uzpln.has_incident_links(listing_empty_html) is False


def test_has_incident_links_true(listing_html):
    assert uzpln.has_incident_links(listing_html) is True


# ── detail-page metadata parse ─────────────────────────────────────────────


def test_parse_detail_recent(detail_recent_html):
    d = uzpln.parse_detail(detail_recent_html)
    assert d["report_number"] == "CZ-25-1428"
    assert d["event_date"] == "2025-07-29"
    assert d["report_kind"] == "Závěrečná zpráva"
    assert d["location"] == "jihovýchodní svah Ranské hory"
    assert d["operation"] == "Rekreační a sportovní létání"
    assert d["event_kind"] == "Letecká nehoda"
    assert d["aircraft"] == "MAGIC M"
    assert d["pdf_href"] == "/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf"


def test_parse_detail_old_no_case_number(detail_old_html):
    d = uzpln.parse_detail(detail_old_html)
    assert d["report_number"] is None  # blank Číslo zprávy
    assert d["event_date"] == "2003-02-22"
    assert d["aircraft"] == "MAGGIC 165."
    assert d["pdf_href"] == "/pdf/ecrSLXV8.pdf"


# ── PDF href URL-encoding (spaces + Czech diacritics) ──────────────────────


def test_encode_pdf_href_spaces_and_diacritics():
    raw = "/pdf/202601121455-ZZ CZ-25-1428 Originál PK.pdf"
    url = uzpln.encode_pdf_href(raw)
    assert url == (
        "https://uzpln.gov.cz/pdf/"
        "202601121455-ZZ%20CZ-25-1428%20Origin%C3%A1l%20PK.pdf"
    )
    assert " " not in url            # all spaces encoded
    assert url.count("/") == 4       # path separators preserved, not encoded


def test_encode_pdf_href_hash_passthrough():
    url = uzpln.encode_pdf_href("/pdf/ecrSLXV8.pdf")
    assert url == "https://uzpln.gov.cz/pdf/ecrSLXV8.pdf"


def test_encode_pdf_href_none():
    assert uzpln.encode_pdf_href(None) is None


# ── case_id fallback logic ─────────────────────────────────────────────────


def test_make_case_id_uses_report_number():
    assert uzpln.make_case_id("CZ-25-1428", "830") == "CZ-25-1428"
    # upper-cased
    assert uzpln.make_case_id("cz-25-1379", "824") == "CZ-25-1379"


def test_make_case_id_surrogate_when_absent():
    assert uzpln.make_case_id(None, "2") == "uzpln-2"
    assert uzpln.make_case_id("", "69") == "uzpln-69"
    assert uzpln.make_case_id("   ", "72") == "uzpln-72"


def test_make_case_id_collision_suffix():
    assert uzpln.make_case_id(
        "CZ-25-1428", "830", taken={"CZ-25-1428"}) == "CZ-25-1428-2"
    assert uzpln.make_case_id(
        None, "2", taken={"uzpln-2", "uzpln-2-2"}) == "uzpln-2-3"


# ── registration from PDF text (best-effort) ───────────────────────────────


def test_extract_registration_found():
    text = "Letadlo poznávací značky OK-ABC provádělo místní let."
    assert uzpln.extract_registration(text) == "OK-ABC"


def test_extract_registration_none_for_foreign():
    text = "letadlo zahraniční registrace (Německo), bez české značky"
    assert uzpln.extract_registration(text) is None
    assert uzpln.extract_registration("") is None
