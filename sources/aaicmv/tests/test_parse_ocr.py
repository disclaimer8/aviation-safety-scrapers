# OCR fallback: a thin-text (scanned) row whose OCR recovers >= MIN_NARRATIVE
# is promoted to tier='ocr' and survives build(); OCR that recovers nothing
# stays dropped (scanned) so garbage never reaches prod. No ssh/ocrmypdf runs:
# extract_text and pdf.ocr_extract are monkeypatched.
import aaicmv_ingest.pipeline as P
from aaicmv_ingest import db


def _seed(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_schema(conn)
    ts = db.now_ms()
    conn.execute(
        "INSERT INTO aaicmv_reports (case_id, pdf_path, status, "
        "discovered_at, updated_at) VALUES (?,?,?,?,?)",
        ("X-1", "/tmp/x.pdf", db.STATUS_FETCHED, ts, ts),
    )
    conn.commit()
    return conn


def test_thin_text_promoted_to_ocr(tmp_path, monkeypatch):
    conn = _seed(tmp_path)
    monkeypatch.setattr(P, "extract_text", lambda p: "short")
    monkeypatch.setattr(P.pdf, "ocr_extract", lambda p, lang="eng": "L" * 1000)
    P.parse(conn)
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM aaicmv_reports WHERE case_id='X-1'"
    ).fetchone()
    assert row["source_tier"] == "ocr"
    assert len(row["narrative_text"]) >= P.MIN_NARRATIVE
    # and build() must emit it
    P.build(conn)
    built = conn.execute(
        "SELECT 1 FROM aaicmv_accidents WHERE case_id='X-1'"
    ).fetchone()
    assert built is not None


def test_ocr_empty_stays_dropped(tmp_path, monkeypatch):
    conn = _seed(tmp_path)
    monkeypatch.setattr(P, "extract_text", lambda p: "short")
    monkeypatch.setattr(P.pdf, "ocr_extract", lambda p, lang="eng": "")
    P.parse(conn)
    row = conn.execute(
        "SELECT source_tier FROM aaicmv_reports WHERE case_id='X-1'"
    ).fetchone()
    assert row["source_tier"] not in ("ocr", "pdf")  # -> build drops it
    P.build(conn)
    built = conn.execute(
        "SELECT 1 FROM aaicmv_accidents WHERE case_id='X-1'"
    ).fetchone()
    assert built is None
