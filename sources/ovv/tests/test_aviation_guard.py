"""build() publishes aviation reports only.

The 2026-08 redesign took the aviation filter off OVV's listing, so the
09-06 discover pulled in every theme OVV investigates, and build() published
91 of them (a manure silo, a fast ferry, a bridge, COVID-19) as aviation
narratives. A slug cannot tell the themes apart (glider types, balloon
registrations), so the report's own text has to.
"""
from ovv_ingest import db, ovv, pipeline

AVIATION = ("The Cessna 172 took off from runway 23 at Teuge airport. Shortly after "
            "take-off the aircraft lost power and the pilot made a forced landing in a field. ") * 5
SHIPPING = ("The container vessel was moored at the quay when a mooring line parted. "
            "The chief mate and the maritime pilot were on the bridge. ") * 5


def _conn_with(case_id, title, text):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO ovv_reports (case_id, detail_url, title, narrative_text, status, "
        "discovered_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (case_id, f"https://onderzoeksraad.nl/en/onderzoek/{case_id}/", title, text,
         db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    return conn


def _status(conn, case_id):
    return conn.execute("SELECT status FROM ovv_reports WHERE case_id=?", (case_id,)).fetchone()["status"]


def test_a_shipping_report_is_skipped_not_built():
    conn = _conn_with("fatal-outcome-following-parting-of-mooring-line", "Mooring line parted", SHIPPING)
    pipeline.build(conn)
    assert _status(conn, "fatal-outcome-following-parting-of-mooring-line") == db.STATUS_SKIPPED
    assert conn.execute("SELECT COUNT(*) FROM ovv_accidents").fetchone()[0] == 0


def test_an_aviation_report_is_built():
    conn = _conn_with("engine-failure-cessna-172-teuge", "Engine failure Cessna 172", AVIATION)
    pipeline.build(conn)
    assert _status(conn, "engine-failure-cessna-172-teuge") == db.STATUS_BUILT


def test_an_airport_word_in_the_slug_alone_is_not_enough():
    # fire-at-the-detention-centre-schiphol-oost: "schiphol" in the slug, a
    # building fire in the text.
    text = "A fire broke out in a cell of the detention centre. Staff evacuated the wing. " * 8
    assert not ovv.looks_like_aviation("fire-at-the-detention-centre-schiphol-oost", "Fire", text)


def test_a_maritime_pilot_does_not_count():
    assert not ovv.looks_like_aviation("pilot-boarding-accident", "Pilot ladder", "The pilot boarded the ship. " * 40)


def test_a_dutch_glider_report_counts():
    text = "Het zweefvliegtuig maakte een grondzwaai tijdens de lierstart op het vliegveld. " * 4
    assert ovv.looks_like_aviation("ground-loop-during-winch-start-ls-4-ph-1219", "Grondzwaai LS-4", text + " luchtvaartuig " * 3)
