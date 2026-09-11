# tests/test_pipeline.py
"""Pipeline tests for BAGAIA ingest (offline, no network)."""
import pytest
from bagaia_ingest import bagaia, db
from bagaia_ingest.pipeline import seed, discover, fetch, parse, build


# ── seed ──────────────────────────────────────────────────────────────────────

def test_seed_inserts_known_reports(conn):
    n = seed(conn)
    assert n == len(bagaia.SEED_REPORTS)
    row = conn.execute(
        "SELECT * FROM bagaia_reports WHERE case_id='bagaia-ur-ckc-2017'"
    ).fetchone()
    assert row is not None
    assert row["registration"] == "UR-CKC"
    assert row["aircraft"] == "AN-74TK-100"
    assert row["date_of_occurrence"] == "2017-07-29"
    assert row["status"] == "new"
    assert row["lang"] == "en"


def test_seed_idempotent(conn):
    assert seed(conn) == len(bagaia.SEED_REPORTS)
    assert seed(conn) == 0  # already present


# ── discover (fake dashboard response) ────────────────────────────────────────

_DASHBOARD_JSON = {
    "recordsTotal": 3,
    "data": [
        # Nigeria → MEMBER_STATE_COVERED → skip
        {
            "country": "Nigeria",
            "date": "29/07/2017",
            "registration_number": "UR-CKC",
            "aircraft_type": "AN-74TK-100",
            "aircraft_operator": "CAVOK Airlines",
            "occurence": "Accident",
            "report_link": "https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/CVK/2017/07/29/F.pdf",
        },
        # Ghana → MEMBER_STATE_COVERED → skip
        {
            "country": "Ghana",
            "date": "15/04/2022",
            "registration_number": "ZS-SXM",
            "aircraft_type": "A330-300",
            "aircraft_operator": "SAA",
            "occurence": "Incident",
            "report_link": "https://aibghana.gov.gh/wp-content/uploads/2023/12/COMPLETE-SAA-REPORT.pdf",
        },
        # Liberia → not covered → new candidate
        {
            "country": "Liberia",
            "date": "17/08/2024",
            "registration_number": "CN-RGW",
            "aircraft_type": "Boeing 737-800",
            "aircraft_operator": "Royal Air Maroc",
            "occurence": "Incident",
            "report_link": "https://drive.google.com/file/d/1NSQpYBot-CEk0haKul5XMtWHiVPeCjpF/view",
        },
    ],
}


class FakeClient:
    def __init__(self, payload=_DASHBOARD_JSON):
        self._payload = payload

    def get(self, url, **kwargs):
        return FakeResponse(self._payload)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_discover_skips_member_states(conn):
    n = discover(conn, FakeClient())
    # Only Liberia row is a non-member-state candidate
    assert n == 1
    row = conn.execute(
        "SELECT * FROM bagaia_reports WHERE registration='CN-RGW'"
    ).fetchone()
    assert row is not None
    assert row["date_of_occurrence"] == "2024-08-17"


def test_discover_idempotent(conn):
    assert discover(conn, FakeClient()) == 1
    assert discover(conn, FakeClient()) == 0


def test_discover_dashboard_error_graceful(conn):
    class ErrorClient:
        def get(self, url, **kw):
            raise RuntimeError("network error")

    assert discover(conn, ErrorClient()) == 0


# ── build ──────────────────────────────────────────────────────────────────────

def test_build_promotes_rich_narrative(conn, tmp_path, monkeypatch):
    seed(conn)
    # Manually advance to parsed with a rich narrative
    long_text = "X" * 5000
    conn.execute(
        "UPDATE bagaia_reports SET status='fetched', pdf_path=? WHERE case_id='bagaia-ur-ckc-2017'",
        (str(tmp_path / "dummy.pdf"),),
    )
    conn.commit()

    import bagaia_ingest.pipeline as pl
    monkeypatch.setattr(pl, "extract_text", lambda p: long_text)
    parse(conn)
    built = build(conn)
    assert built == 1
    acc = conn.execute(
        "SELECT * FROM bagaia_accidents WHERE case_id='bagaia-ur-ckc-2017'"
    ).fetchone()
    assert acc is not None
    assert acc["registration"] == "UR-CKC"
    assert acc["country"] == "ST"
    assert len(acc["narrative_text"]) == 5000


def test_build_skips_short_narrative(conn, tmp_path, monkeypatch):
    seed(conn)
    conn.execute(
        "UPDATE bagaia_reports SET status='fetched', pdf_path=? WHERE case_id='bagaia-ur-ckc-2017'",
        (str(tmp_path / "dummy.pdf"),),
    )
    conn.commit()
    import bagaia_ingest.pipeline as pl
    monkeypatch.setattr(pl, "extract_text", lambda p: "tiny")
    parse(conn)
    built = build(conn)
    assert built == 0
    row = conn.execute(
        "SELECT status FROM bagaia_reports WHERE case_id='bagaia-ur-ckc-2017'"
    ).fetchone()
    assert row["status"] == "skipped"
