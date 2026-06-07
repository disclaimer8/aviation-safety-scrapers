from sust_ingest import sust


# ── skeleton parse ────────────────────────────────────────────────────────────

def test_parse_skeleton_extracts_uid_and_lazyload(skeleton_html):
    rows = sust.parse_skeleton(skeleton_html)
    assert len(rows) == 10
    uid, url = rows[0]
    assert uid == 3844
    # entity-decoded: contains literal & not &amp;
    assert "&amp;" not in url
    assert "tx_sustemas_listavexamination%5Bid%5D=3844" in url
    assert "cHash=" in url
    # all uids are ints
    assert all(isinstance(u, int) for u, _ in rows)


def test_absolute_url():
    assert sust.absolute_url("/inhalte/AV-berichte/1.pdf") == (
        "https://www.sust.admin.ch/inhalte/AV-berichte/1.pdf"
    )
    assert sust.absolute_url("https://x/y") == "https://x/y"
    assert sust.absolute_url(None) is None


# ── date parse ────────────────────────────────────────────────────────────────

def test_parse_date_spaces():
    assert sust.parse_date("04. 05. 2026") == "2026-05-04"
    assert sust.parse_date("29. 03. 1959") == "1959-03-29"


def test_parse_date_bad():
    assert sust.parse_date(None) is None
    assert sust.parse_date("garbage") is None


# ── lang suffix detection ─────────────────────────────────────────────────────

def test_lang_from_filename_uppercase():
    assert sust.lang_from_filename("/x/HB-ZEJ_VB_D.pdf") == "de"
    assert sust.lang_from_filename("/x/HB-ZEJ_VB_F.pdf") == "fr"
    assert sust.lang_from_filename("/x/HB-ZEJ_VB_I.pdf") == "it"
    assert sust.lang_from_filename("/x/HB-JHK_Notification_E.pdf") == "en"


def test_lang_from_filename_lowercase():
    assert sust.lang_from_filename("/x/D-EXIK_VB_e.pdf") == "en"
    assert sust.lang_from_filename("/x/D-EXIK_FB_d.pdf") == "de"


def test_lang_from_filename_numeric_defaults_de():
    assert sust.lang_from_filename("/inhalte/AV-berichte/1.pdf") == "de"
    assert sust.lang_from_filename("/inhalte/AV-berichte/511.pdf") == "de"


# ── document preference ───────────────────────────────────────────────────────

def test_pick_document_prefers_schlussbericht():
    docs = [
        {"name": "Vorbericht", "url": "/x/D-EXIK_VB_D.pdf"},
        {"name": "Notification", "url": "/x/D-EXIK_VB_e.pdf"},
        {"name": "Schlussbericht", "url": "/x/D-EXIK_FB_D.pdf"},
    ]
    pick = sust.pick_document(docs)
    assert pick["kind"] == "Final"
    assert pick["lang"] == "de"
    assert pick["url"].endswith("D-EXIK_FB_D.pdf")
    assert pick["url"].startswith("https://www.sust.admin.ch/")


def test_pick_document_full_preference_order():
    docs = [
        {"name": "Notification", "url": "/x/a_Notification.pdf"},
        {"name": "Vorbericht", "url": "/x/a_VB_D.pdf"},
        {"name": "Faktenbericht", "url": "/x/a_Fakten_D.pdf"},
        {"name": "Summarischer Bericht", "url": "/x/a_summ.pdf"},
    ]
    # no final present → summary wins
    assert sust.pick_document(docs)["kind"] == "Summary"


def test_pick_document_localized_names():
    # French final ('Rapport final') over Italian preliminary
    docs = [
        {"name": "Rapporto preliminare", "url": "/x/a_pre.pdf"},
        {"name": "Rapport final", "url": "/x/a_fin_F.pdf"},
    ]
    pick = sust.pick_document(docs)
    assert pick["kind"] == "Final"
    assert pick["lang"] == "fr"


def test_pick_document_prefers_native_over_en_notification():
    # native-lang Schlussbericht beats an EN Notification
    docs = [
        {"name": "Notification", "url": "/x/a_Notification_E.pdf"},
        {"name": "Schlussbericht", "url": "/x/a_FB_D.pdf"},
    ]
    assert sust.pick_document(docs)["lang"] == "de"


def test_pick_document_none_when_empty():
    assert sust.pick_document([]) is None
    assert sust.pick_document(None) is None


# ── entry parse ───────────────────────────────────────────────────────────────

def test_parse_entry_multi(entry_multi):
    m = sust.parse_entry(entry_multi)
    assert m["case_id"] == "3811"
    assert m["date_of_occurrence"] == "2025-03-17"
    assert m["registration"] == "D-EXIK"
    assert m["aircraft"] == "EXTRA AIRCRAFT EA400"
    assert m["location"] == "La Punt Chamues-ch, GR"
    assert m["doc"]["kind"] == "Final"
    assert m["doc"]["lang"] == "de"


def test_parse_entry_single(entry_single):
    m = sust.parse_entry(entry_single)
    assert m["case_id"] == "3844"
    assert m["registration"] == "HB-ZEJ"
    assert m["doc"]["lang"] == "it"  # _VB_I
    assert m["location"] == "Mezzovico-Vira, TI"


def test_parse_entry_docless(entry_docless):
    m = sust.parse_entry(entry_docless)
    assert m["case_id"] == "3751"
    assert m["doc"] is None
    assert m["registration"] is None
    # location with no canton key falls back to place only
    assert m["location"] == "Aarhus Sea Airport (EKAC)"
