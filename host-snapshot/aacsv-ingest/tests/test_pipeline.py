"""Pipeline tests: discover from fixture (FakeClient) + final-over-prelim dedup."""
import os

from aacsv_ingest import aacsv, db, pipeline

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def test_discover_inserts_rows(tmp_path, make_client):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    client = make_client({aacsv.INDEX_URL: type("R", (), {
        "content": _fixture("aacsv_informes.html").encode("utf-8"),
        "text": _fixture("aacsv_informes.html"),
        "raise_for_status": lambda self: None,
    })()})
    n = pipeline.discover(conn, client)
    assert n == 45
    # idempotent
    assert pipeline.discover(conn, client) == 0


def test_parse_scanned_tier_none(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, pdf_path, status, discovered_at, updated_at) "
        "VALUES ('scan','AAC-AIG-X', NULL, ?, ?, ?)",
        (db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()
    pipeline.parse(conn)
    row = conn.execute("SELECT source_tier, status FROM aacsv_reports WHERE slug='scan'").fetchone()
    assert row["source_tier"] == "none"
    assert row["status"] == db.STATUS_PARSED


def test_build_skips_short_and_scanned(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('scan','AAC-AIG-SCAN','YS-05P','2025-02-19','', 'final', ?, ?, ?)",
        (db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    assert pipeline.build(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM aacsv_accidents").fetchone()[0] == 0


def test_build_final_supersedes_prelim(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    long_txt = "x" * 1200
    # same occurrence: YS-331PE / 2023 — one prelim, one final
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('prelim','AAC-AIG-P','YS-331P','2023-06-26', ?, 'preliminary', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('final','AAC-AIG-F','YS-331PE','2023-06-26', ?, 'final', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    built = pipeline.build(conn)
    assert built == 1
    acc = conn.execute("SELECT case_id, report_type FROM aacsv_accidents").fetchall()
    assert len(acc) == 1
    assert acc[0]["case_id"] == "AAC-AIG-F"
    # prelim row marked skipped
    st = conn.execute("SELECT status FROM aacsv_reports WHERE slug='prelim'").fetchone()
    assert st["status"] == db.STATUS_SKIPPED


def test_build_distinct_occurrences_both_kept(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    long_txt = "y" * 1200
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('a','AAC-AIG-A','YS-289P','2023-07-23', ?, 'final', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('b','AAC-AIG-B','N9417T','2025-10-25', ?, 'final', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    assert pipeline.build(conn) == 2


def test_yearless_same_airframe_not_deduped(tmp_path):
    """Two finals for the same airframe but with NO parseable year must both be
    kept (different occurrences), not collapsed into one."""
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    long_txt = "z" * 1200
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('a','AAC-AIG-009-YS-327A-2021','YS-327A',NULL, ?, 'final', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.execute(
        "INSERT INTO aacsv_reports (slug, case_id, registration, date_of_occurrence, "
        "narrative_text, report_type, status, discovered_at, updated_at) "
        "VALUES ('b','AAC-AIG-002-YS-327A-2016','YS-327A',NULL, ?, 'final', ?, ?, ?)",
        (long_txt, db.STATUS_PARSED, ts, ts),
    )
    conn.commit()
    assert pipeline.build(conn) == 2
    assert conn.execute("SELECT COUNT(*) FROM aacsv_accidents").fetchone()[0] == 2
