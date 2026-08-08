# OCR fallback: a thin-text (scanned) row whose OCR recovers >= _NARRATIVE_FLOOR
# is promoted to tier='ocr' and survives build(); OCR that recovers nothing
# stays dropped (scanned) so garbage never reaches prod.  No ssh/ocrmypdf is
# touched — ocr_extract is monkeypatched.
import pytest

from sacaa_ingest import db, pipeline, sacaa


class FakeResp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


_ROW = ('<tr><td>2020</td><td>1 May</td><td>C172</td><td>Cape Town</td>'
        '<td>1234</td><td>ZS-ABC</td>'
        '<td><a href="https://x.blob.core.windows.net/c/1234.pdf">D</a></td></tr>')
_MAIN = "<table>" + _ROW + "</table>"


class FakeClient:
    def get(self, url, params=None):
        if url == sacaa.MAIN_URL:
            return FakeResp(text=_MAIN)
        if url == sacaa.ARCHIVE_URL:
            return FakeResp(text="<table></table>")
        return FakeResp(content=b"%PDF-1.4 x")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(sacaa, "DELAY", 0)


def _seed(conn):
    pipeline.discover(conn, FakeClient())


def test_thin_text_promoted_to_ocr(conn, tmp_path, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "L" * 1000)
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM sacaa_reports WHERE case_id='1234'"
    ).fetchone()
    assert row["source_tier"] == "ocr"
    assert len(row["narrative_text"]) >= pipeline._NARRATIVE_FLOOR
    # and build() must emit it
    pipeline.build(conn)
    built = conn.execute(
        "SELECT 1 FROM sacaa_accidents WHERE case_id='1234'"
    ).fetchone()
    assert built is not None


def test_ocr_empty_stays_dropped(conn, tmp_path, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline, "ocr_extract", lambda p, lang="eng": "")
    pipeline.fetch(conn, FakeClient(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier FROM sacaa_reports WHERE case_id='1234'"
    ).fetchone()
    assert row["source_tier"] not in ("ocr", "pdf")  # -> build drops it
    pipeline.build(conn)
    built = conn.execute(
        "SELECT 1 FROM sacaa_accidents WHERE case_id='1234'"
    ).fetchone()
    assert built is None
