# OCR fallback (fetch): a scanned doc whose text layer stays below the floor
# is OCR'd; when OCR recovers >= _NARRATIVE_FLOOR the row is promoted to
# tier='ocr' and survives build().  OCR that recovers nothing falls back to the
# summary/scanned tier and is dropped so garbage never reaches prod.
from ovv_ingest import ovv, pipeline


class _Resp:
    def __init__(self, text="", content=b""):
        self.text = text
        self.content = content

    def raise_for_status(self):
        pass


_D1 = "https://onderzoeksraad.nl/en/onderzoek/crash-ph-abc-somewhere/"
_LISTING = f'<a href="{_D1}">x</a>'
_DOC_MAIN = "https://onderzoeksraad.nl/ab12cd34ef56report_crash_en-pdf/"
_DETAIL1 = (f'<h1>Crash PH-ABC at Somewhere on 6 June 2021</h1>'
            f'<p>{"S" * 200}</p>'
            f'<a href="{_DOC_MAIN}">main</a>')


class _Client:
    def __init__(self):
        self.pages = {1: _LISTING, 2: ""}
        self.urls = {_D1: _DETAIL1, _DOC_MAIN: b"%PDF main"}

    def get(self, url, params=None):
        if params is not None and "_page" in params:
            return _Resp(text=self.pages.get(params["_page"], ""))
        val = self.urls.get(url)
        if val is None:
            raise RuntimeError("404")
        if isinstance(val, bytes):
            return _Resp(content=val)
        return _Resp(text=val)


def _discover(conn, monkeypatch):
    monkeypatch.setattr(ovv, "DELAY", 0)
    pipeline.discover(conn, _Client())


def test_scanned_pdf_promoted_to_ocr(conn, tmp_path, monkeypatch):
    _discover(conn, monkeypatch)
    # text layer is thin (scan); OCR recovers a full narrative
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline.pdf, "ocr_extract",
                        lambda p, lang="nld+eng": "L" * 1000)
    pipeline.fetch(conn, _Client(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier, narrative_text FROM ovv_reports "
        "WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert row["source_tier"] == "ocr"
    assert len(row["narrative_text"]) >= pipeline._NARRATIVE_FLOOR
    # and build() must emit it
    assert pipeline.build(conn) == 1
    built = conn.execute(
        "SELECT 1 FROM ovv_accidents WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert built is not None


def test_ocr_empty_stays_dropped(conn, tmp_path, monkeypatch):
    _discover(conn, monkeypatch)
    monkeypatch.setattr(pipeline.pdf, "extract_text", lambda p: "short")
    monkeypatch.setattr(pipeline.pdf, "ocr_extract",
                        lambda p, lang="nld+eng": "")
    pipeline.fetch(conn, _Client(), pdf_dir=str(tmp_path))
    row = conn.execute(
        "SELECT source_tier FROM ovv_reports WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert row["source_tier"] not in ("ocr", "pdf")  # -> build drops it
    assert pipeline.build(conn) == 0
    dropped = conn.execute(
        "SELECT 1 FROM ovv_accidents WHERE case_id='crash-ph-abc-somewhere'"
    ).fetchone()
    assert dropped is None
