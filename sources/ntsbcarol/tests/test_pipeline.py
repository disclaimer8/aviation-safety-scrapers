# tests/test_pipeline.py
"""Pipeline tests for ntsbcarol discover → fetch → parse → build.

All network calls are mocked; no live CAROL API is used.
"""
import os
import tempfile

import pytest

from ntsbcarol_ingest import carol, db
from ntsbcarol_ingest.pipeline import build, discover, fetch, parse


def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


# ── mock CAROL data ─────────────────────────────────────────────────────────

_FAKE_CAROL_ROW_KAUAI = {
    "ntsb_no": "ANC20MA010",
    "mkey": "100736",
    "event_date_raw": "2019-12-26T17:57:00Z",
    "city": "Kekaha",
    "state": "Hawaii",
    "country_name": "United States",
    "registration": "N985SA",
    "make": "Airbus",
    "model": "AS350B2",
    "event_type": "Accident",
    "highest_injury": "Fatal",
    "fatalities_str": "7",
    "report_no": "AIR2205",
    "completion_status": "Completed",
    "most_recent_report_type": "Final",
    "ev_id": "",
    "entry_id": "abc123",
}

_FAKE_CAROL_ROW_AC759 = {
    "ntsb_no": "DCA17IA148",
    "mkey": "95528",
    "event_date_raw": "2017-07-07T23:56:00Z",
    "city": "San Francisco",
    "state": "California",
    "country_name": "United States",
    "registration": "C-FKCK",
    "make": "Airbus",
    "model": "A320-211",
    "event_type": "Incident",
    "highest_injury": "None",
    "fatalities_str": "",
    "report_no": "AIR1801",
    "completion_status": "Completed",
    "most_recent_report_type": "Final",
    "ev_id": "",
    "entry_id": "def456",
}

_FAKE_CAROL_ROW_INWORK = {
    "ntsb_no": "DCA26MA999",
    "mkey": "999999",
    "event_date_raw": "2026-01-01T00:00:00Z",
    "city": "Test",
    "state": "NY",
    "country_name": "United States",
    "registration": "NTEST",
    "make": "Boeing",
    "model": "737",
    "event_type": "Accident",
    "highest_injury": "Fatal",
    "fatalities_str": "2",
    "report_no": None,
    "completion_status": "In work",
    "most_recent_report_type": "Preliminary",
    "ev_id": "",
    "entry_id": "zzz",
}

_FAKE_ROWS = [_FAKE_CAROL_ROW_KAUAI, _FAKE_CAROL_ROW_AC759, _FAKE_CAROL_ROW_INWORK]


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_inserts_completed_skips_inwork(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(carol, "create_session", lambda: 12345)
    monkeypatch.setattr(carol, "get_all_board_cases", lambda sess_id, **kw: iter(_FAKE_ROWS))

    n = discover(conn, session_id=12345)
    assert n == 2  # in-work is skipped

    rows = conn.execute("SELECT case_id, ntsb_no, status, fatalities_total FROM ntsbcarol_cases ORDER BY case_id").fetchall()
    assert len(rows) == 2
    case_ids = {r["case_id"] for r in rows}
    assert "ntsbcarol-anc20ma010" in case_ids
    assert "ntsbcarol-dca17ia148" in case_ids

    # Kauai: 7 fatalities, event_date
    kauai = conn.execute("SELECT * FROM ntsbcarol_cases WHERE case_id=?", ("ntsbcarol-anc20ma010",)).fetchone()
    assert kauai["fatalities_total"] == 7
    assert kauai["event_date"] == "2019-12-26"
    assert kauai["registration"] == "N985SA"
    assert kauai["report_no"] == "AIR2205"


def test_discover_idempotent(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(carol, "create_session", lambda: 12345)
    monkeypatch.setattr(carol, "get_all_board_cases", lambda sess_id, **kw: iter(_FAKE_ROWS))

    n1 = discover(conn, session_id=12345)
    n2 = discover(conn, session_id=12345)
    assert n1 == 2
    assert n2 == 0  # nothing new


# ── fetch ────────────────────────────────────────────────────────────────────

def test_fetch_downloads_pdf(monkeypatch, tmp_path):
    conn = _conn()
    monkeypatch.setattr(carol, "create_session", lambda: 12345)
    monkeypatch.setattr(carol, "get_all_board_cases", lambda sess_id, **kw: iter(_FAKE_ROWS))
    discover(conn, session_id=12345)

    # Mock download_pdf to write a fake file
    downloaded = []

    def _fake_download(report_no, dest_path, timeout=60):
        with open(dest_path, "wb") as f:
            f.write(b"fake pdf content " * 1000)
        downloaded.append(report_no)
        return True

    monkeypatch.setattr(carol, "download_pdf", _fake_download)
    monkeypatch.setattr(carol, "DELAY", 0)

    n = fetch(conn, str(tmp_path))
    assert n == 2  # both rows processed
    assert "AIR2205" in downloaded
    assert "AIR1801" in downloaded

    # Check status advanced
    rows = conn.execute("SELECT case_id, status FROM ntsbcarol_cases").fetchall()
    for row in rows:
        assert row["status"] == db.STATUS_FETCHED


# ── parse ────────────────────────────────────────────────────────────────────

_FAKE_NARRATIVE = (
    "The National Transportation Safety Board investigated this accident involving "
    "the Blue Hawaiian Helicopters Airbus Helicopters AS350B2 (N985SA) which crashed "
    "near Kekaha, Hawaii on December 26, 2019, resulting in 7 fatal injuries. "
    "The helicopter was on a commercial sightseeing tour when it impacted terrain. "
    "Investigation revealed multiple factors including weather and terrain. " * 20
)
_FAKE_PC = "PROBABLE CAUSE\n\nThe Board determines that the probable cause of this accident was the pilot's decision to continue VFR flight into IMC."


def test_parse_extracts_narrative(monkeypatch, tmp_path):
    conn = _conn()
    monkeypatch.setattr(carol, "create_session", lambda: 12345)
    monkeypatch.setattr(carol, "get_all_board_cases", lambda sess_id, **kw: iter([_FAKE_CAROL_ROW_KAUAI]))
    discover(conn, session_id=12345)

    # Write a fake PDF
    pdf_path = str(tmp_path / "air2205.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"fake")

    conn.execute(
        "UPDATE ntsbcarol_cases SET pdf_path=?, status=? WHERE case_id=?",
        (pdf_path, db.STATUS_FETCHED, "ntsbcarol-anc20ma010"),
    )
    conn.commit()

    # Mock text extraction
    import ntsbcarol_ingest.text as txt_mod
    monkeypatch.setattr(txt_mod, "extract_text", lambda path: (_FAKE_NARRATIVE + _FAKE_PC, "pdf"))

    n = parse(conn)
    assert n == 1

    row = conn.execute("SELECT narrative_text, probable_cause, status FROM ntsbcarol_cases WHERE case_id=?",
                       ("ntsbcarol-anc20ma010",)).fetchone()
    assert row["status"] == db.STATUS_PARSED
    assert len(row["narrative_text"]) > 200
    assert "PROBABLE CAUSE" in row["narrative_text"] or row["probable_cause"]


# ── build ────────────────────────────────────────────────────────────────────

def test_build_emits_accident_row(monkeypatch, tmp_path):
    conn = _conn()
    ts = db.now_ms()
    # Insert a pre-parsed case directly
    conn.execute(
        """INSERT INTO ntsbcarol_cases
           (case_id, ntsb_no, mkey, report_no, event_date, city, state, country,
            registration, make, model, fatalities_total, narrative_text, probable_cause,
            source_tier, status, discovered_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "ntsbcarol-dca95ma001",
            "DCA95MA001",
            "10992",
            "AIR9601",
            "1994-10-31",
            "Roselawn",
            "Indiana",
            "US",
            "N401",
            "ATR",
            "72-212",
            68,
            "American Eagle Flight 4184 crashed near Roselawn Indiana during " + "holding " * 100,
            "The Board determines the probable cause was icing on the wing.",
            "pdf",
            db.STATUS_PARSED,
            ts,
            ts,
        ),
    )
    conn.commit()

    n = build(conn)
    assert n == 1

    acc = conn.execute(
        "SELECT * FROM ntsbcarol_accidents WHERE case_id=?",
        ("ntsbcarol-dca95ma001",),
    ).fetchone()
    assert acc["event_date"] == "1994-10-31"
    assert acc["country"] == "US"
    assert acc["lang"] == "en"
    assert acc["report_type"] == "final"
    assert acc["source_url"] == "https://carol.ntsb.gov/event/DCA95MA001"
    assert acc["fatalities_total"] == 68
    assert len(acc["narrative_text"]) > 200
    assert acc["probable_cause"] is not None


def test_build_skips_short_narrative():
    conn = _conn()
    ts = db.now_ms()
    conn.execute(
        """INSERT INTO ntsbcarol_cases
           (case_id, ntsb_no, mkey, event_date, narrative_text, source_tier,
            status, discovered_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("ntsbcarol-short", "DCA00IA001", "99", "2000-01-01", "too short", "none",
         db.STATUS_PARSED, ts, ts),
    )
    conn.commit()

    n = build(conn)
    assert n == 0

    row = conn.execute("SELECT status FROM ntsbcarol_cases WHERE case_id=?", ("ntsbcarol-short",)).fetchone()
    assert row["status"] == db.STATUS_SKIPPED
