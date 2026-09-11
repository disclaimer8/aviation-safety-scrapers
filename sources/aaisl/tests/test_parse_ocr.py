# OCR fallback: a thin-text (scanned) row whose OCR recovers >= MIN_NARRATIVE
# is promoted to tier='ocr' and survives build(); OCR that recovers nothing
# stays dropped (scanned) so garbage never reaches prod. No ssh — ocr_extract
# is monkeypatched on the pipeline namespace (mirrors extract_text).
from aaisl_ingest import db, pipeline
from aaisl_ingest.pdf import MIN_NARRATIVE


def _seed_fetched(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaisl_reports (case_id, status, pdf_path, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("4R-OCR-20240101", db.STATUS_FETCHED, "/tmp/x.pdf", ts, ts),
    )
    conn.commit()
    return conn


def test_thin_text_promoted_to_ocr(tmp_path, monkeypatch):
    conn = _seed_fetched(tmp_path)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "L" * 1000)

    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier, status, narrative_text FROM aaisl_reports "
        "WHERE case_id='4R-OCR-20240101'"
    ).fetchone()
    assert row["source_tier"] == "ocr"
    assert row["status"] == db.STATUS_PARSED
    assert len(row["narrative_text"]) >= MIN_NARRATIVE

    # build() must emit the OCR-recovered report.
    assert pipeline.build(conn) == 1
    built = conn.execute(
        "SELECT country FROM aaisl_accidents WHERE case_id='4R-OCR-20240101'"
    ).fetchone()
    assert built is not None
    assert built["country"] == "LK"


def test_ocr_empty_stays_dropped(tmp_path, monkeypatch):
    conn = _seed_fetched(tmp_path)
    monkeypatch.setattr(pipeline, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "")

    assert pipeline.parse(conn) == 1
    row = conn.execute(
        "SELECT source_tier FROM aaisl_reports WHERE case_id='4R-OCR-20240101'"
    ).fetchone()
    # "short" (5 chars) < _SCANNED_FLOOR -> 'scanned' tier, dropped by build().
    assert row["source_tier"] not in ("ocr", "pdf")

    assert pipeline.build(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM aaisl_accidents WHERE case_id='4R-OCR-20240101'"
    ).fetchone()[0] == 0
