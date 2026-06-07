from ttsb_ingest import ttsb


# ── detail-node-prefix matching (NOT the list path) ────────────────────────


def test_detail_id_from_url_en_prefix():
    url = "https://www.ttsb.gov.tw/english/18609/18610/44273/post"
    assert ttsb.detail_id_from_url(url, ttsb.EN_DETAIL_PREFIX) == "44273"


def test_detail_id_from_url_zh_prefix():
    url = "https://www.ttsb.gov.tw/1243/16869/44270/post"
    assert ttsb.detail_id_from_url(url, ttsb.ZH_DETAIL_PREFIX) == "44270"


def test_parse_listing_matches_only_detail_prefix(en_list_html):
    # The list page is served under /english/16051/... but rows live under the
    # DIFFERENT detail prefix /english/18609/18610/. A chrome /post link under
    # any other prefix must never be picked up as a row.
    rows = ttsb.parse_listing(en_list_html, ttsb.EN_DETAIL_PREFIX, lang="en")
    ids = {r["detail_id"] for r in rows}
    assert ids == {"44578", "44273", "34932"}
    # Every harvested detail URL is under the detail-node prefix.
    assert all(ttsb.EN_DETAIL_PREFIX in r["detail_url"] for r in rows)


def test_parse_listing_wrong_prefix_yields_nothing(en_list_html):
    # Matching by the list's OWN path (16051/...) must not catch row details.
    rows = ttsb.parse_listing(
        en_list_html, "/english/16051/16052/", lang="en")
    assert rows == []


# ── ROC (民國) ↔ Gregorian dates ───────────────────────────────────────────


def test_roc_to_iso():
    assert ttsb.roc_to_iso("113-11-04") == "2024-11-04"
    assert ttsb.roc_to_iso("89-04-21") == "2000-04-21"   # ROC 89 → 2000
    assert ttsb.roc_to_iso("民國113-12-09") == "2024-12-09"


def test_roc_to_iso_unparseable():
    assert ttsb.roc_to_iso("") is None
    assert ttsb.roc_to_iso("no date") is None
    assert ttsb.roc_to_iso("113-13-04") is None  # month 13


def test_parse_iso_date_en():
    assert ttsb.parse_iso_date("2024-11-04") == "2024-11-04"
    assert ttsb.parse_iso_date("2000-05-08") == "2000-05-08"
    assert ttsb.parse_iso_date("") is None


def test_en_zh_dates_agree_for_same_report(en_list_html, zh_list_html):
    en = ttsb.parse_listing(en_list_html, ttsb.EN_DETAIL_PREFIX, "en")
    zh = ttsb.parse_listing(zh_list_html, ttsb.ZH_DETAIL_PREFIX, "zh")
    en_b86 = next(r for r in en if r["detail_id"] == "44273")
    zh_b86 = next(r for r in zh if r["detail_id"] == "44270")
    assert en_b86["event_date"] == "2024-11-04"
    # ROC 113-11-04 must normalise to the SAME Gregorian ISO date.
    assert zh_b86["event_date"] == "2024-11-04"


# ── registration extraction (civil + drone) ────────────────────────────────


def test_extract_registration_civil():
    assert ttsb.extract_registration("Apex Aviation B-86002, ...") == "B-86002"
    assert ttsb.extract_registration("aircraft B-18601 landed") == "B-18601"


def test_extract_registration_drone_class_wins():
    # The B-AAA… drone prefix must win over a plain civil match.
    assert ttsb.extract_registration("B-AAA01397 Drone Occurrence") \
        == "B-AAA01397"
    assert ttsb.is_drone("B-AAA01397")
    assert not ttsb.is_drone("B-86002")


def test_extract_registration_none():
    assert ttsb.extract_registration("foreign aircraft, no Taiwan reg") is None
    assert ttsb.extract_registration("") is None


def test_listing_registration_from_title(en_list_html):
    rows = {r["detail_id"]: r for r in ttsb.parse_listing(
        en_list_html, ttsb.EN_DETAIL_PREFIX, "en")}
    assert rows["44273"]["registration"] == "B-86002"
    assert rows["34932"]["registration"] == "B-AAA01397"
    # JJ2258 row has no B- reg in its title.
    assert rows["44578"]["registration"] is None


# ── report-kind + inline-vs-detail PDF ─────────────────────────────────────


def test_report_kind_from_label():
    assert ttsb.report_kind_from_label("Final Report") == "Final"
    assert ttsb.report_kind_from_label("Executive Summary") \
        == "Executive Summary"
    assert ttsb.report_kind_from_label("More Reports",
                                       "/media/1/x_final-report.pdf") == "Final"
    assert ttsb.report_kind_from_label("More Reports", "/media/1/x.pdf") \
        == "Report"


def test_listing_inline_media(en_list_html):
    rows = {r["detail_id"]: r for r in ttsb.parse_listing(
        en_list_html, ttsb.EN_DETAIL_PREFIX, "en")}
    assert rows["44273"]["pdf_url"].endswith(
        "/media/9314/b-86002_executivesummary.pdf")
    assert rows["44273"]["report_kind"] == "Executive Summary"


def test_pdf_from_detail(en_detail_html):
    # 'More Reports' row's PDF lives only on the detail /post page.
    url = ttsb.pdf_from_detail(en_detail_html)
    assert url.endswith("/media/4428/ci7916_executive-summary.pdf")


def test_registration_from_detail(en_detail_html):
    assert ttsb.registration_from_detail(en_detail_html) == "B-18601"


# ── EN↔ZH row matching (date + reg, fallback date + aircraft) ──────────────


def test_match_en_zh_by_reg_and_by_aircraft(en_list_html, zh_list_html):
    en = ttsb.parse_listing(en_list_html, ttsb.EN_DETAIL_PREFIX, "en")
    zh = ttsb.parse_listing(zh_list_html, ttsb.ZH_DETAIL_PREFIX, "zh")
    pairs = ttsb.match_en_zh(en, zh)
    # B-86002: matched by (date, registration).
    assert pairs["44273"]["detail_id"] == "44270"
    # JJ2258: no reg on either side → matched by (date, aircraft model).
    assert pairs["44578"]["detail_id"] == "44575"
    # The drone EN row has no ZH counterpart in this set.
    assert "34932" not in pairs


def test_match_en_zh_consumes_zh_once():
    en = [
        {"detail_id": "e1", "event_date": "2024-01-01",
         "registration": None, "aircraft": "Cessna/172"},
        {"detail_id": "e2", "event_date": "2024-01-01",
         "registration": None, "aircraft": "Cessna/172"},
    ]
    zh = [{"detail_id": "z1", "event_date": "2024-01-01",
           "registration": None, "aircraft": "Cessna/172"}]
    pairs = ttsb.match_en_zh(en, zh)
    # Only one EN row may consume the single ZH row.
    assert sum(1 for k in ("e1", "e2") if k in pairs) == 1


# ── case_id derivation chain ───────────────────────────────────────────────


def test_media_slug_strips_report_words():
    assert ttsb.media_slug(
        "/media/9314/b-86002_executivesummary.pdf") == "b-86002"
    assert ttsb.media_slug(
        "/media/4428/ci7916_executive-summary.pdf") == "ci7916"
    assert ttsb.media_slug(
        "/media/8925/jj2258調查報告.pdf").startswith("jj2258")


def test_report_number_extraction():
    text = "Report No. TTSB-AOR-25-11-001 issued ..."
    assert ttsb.report_number(text) == "TTSB-AOR-25-11-001"
    assert ttsb.report_number("legacy ASC-AOR-99-12-007 case") \
        == "ASC-AOR-99-12-007"
    assert ttsb.report_number("no report number here") is None


def test_derive_case_id_priority_chain():
    # (1) report number wins.
    assert ttsb.derive_case_id(
        "TTSB-AOR-25-11-001",
        "/media/9314/b-86002_executivesummary.pdf", "44273") \
        == "TTSB-AOR-25-11-001"
    # (2) media slug when no report number.
    assert ttsb.derive_case_id(
        None, "/media/9314/b-86002_executivesummary.pdf", "44273") == "b-86002"
    # (3) ttsb-{detailId} last resort.
    assert ttsb.derive_case_id(None, None, "44273") == "ttsb-44273"
    assert ttsb.derive_case_id(None, None, None) is None


# ── EN-summary-vs-ZH-full preference (the 15K threshold) ───────────────────


def test_choose_narrative_en_full_keeps_en():
    en = "E" * (ttsb.ZH_FULL_THRESHOLD + 100)
    zh = "Z" * 5000
    narrative, lang, summary = ttsb.choose_narrative(en, zh)
    # EN already full → keep EN even though a ZH text exists.
    assert lang == "en"
    assert narrative == en
    assert summary is None


def test_choose_narrative_en_stub_prefers_zh_full():
    en = "E" * 5000           # < 15K → stub
    zh = "Z" * 100000         # full ZH report
    narrative, lang, summary = ttsb.choose_narrative(en, zh)
    assert lang == "zh"
    assert narrative == zh
    assert summary == en      # EN summary preserved for reference


def test_choose_narrative_stub_but_no_zh_keeps_en():
    en = "E" * 5000
    narrative, lang, summary = ttsb.choose_narrative(en, "")
    assert lang == "en"
    assert narrative == en
    assert summary is None


def test_choose_narrative_threshold_boundary():
    # Exactly at threshold counts as 'full' → keep EN.
    en = "E" * ttsb.ZH_FULL_THRESHOLD
    narrative, lang, _ = ttsb.choose_narrative(en, "Z" * 99999)
    assert lang == "en" and narrative == en
