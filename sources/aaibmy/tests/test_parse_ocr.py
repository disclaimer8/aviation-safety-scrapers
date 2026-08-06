# OCR fallback: a thin-text (scanned) row whose OCR recovers >= _NARRATIVE_FLOOR
# is promoted to tier='ocr' and survives build(); OCR that recovers nothing
# stays 'scanned' and is dropped by build() (quality self-filter). No ssh: the
# OCR call is monkeypatched on pipeline.pdf.ocr_extract.
from aaibmy_ingest import db, pipeline


def _seed(conn):
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaibmy_reports "
        "(pdf_url, case_id, page_url, status, discovered_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("http://x/a.pdf", "X-1", "http://x/2024", db.STATUS_NEW, ts, ts),
    )
    conn.commit()


def test_thin_text_promoted_to_ocr(conn, tmp_path, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(pipeline.aaibmy, "DELAY", 0)
    monkeypatch.setattr(pipeline.aaibmy, "download_pdf", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline.pdf, "ocr_extract", lambda p, lang="eng": "L" * 9000)
    pipeline.fetch(conn, client=None, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM aaibmy_reports WHERE case_id='X-1'"
    ).fetchone()
    assert row["source_tier"] == "ocr"
    assert len(row["narrative_text"]) >= pipeline._NARRATIVE_FLOOR
    # and build() must emit it (no source_tier gate, narrative clears the floor)
    assert pipeline.build(conn) == 1
    built = conn.execute(
        "SELECT 1 FROM aaibmy_accidents WHERE case_id='X-1'"
    ).fetchone()
    assert built is not None


def test_ocr_empty_stays_dropped(conn, tmp_path, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(pipeline.aaibmy, "DELAY", 0)
    monkeypatch.setattr(pipeline.aaibmy, "download_pdf", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline.pdf, "ocr_extract", lambda p, lang="eng": "")
    pipeline.fetch(conn, client=None, pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier FROM aaibmy_reports WHERE case_id='X-1'"
    ).fetchone()
    assert row["source_tier"] not in ("ocr", "pdf")  # -> 'scanned', build drops it
