from ciaiauy_ingest import pdf


class _Done:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


def test_extract_text_returns_stdout(monkeypatch):
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: _Done(0, b"  Texto del informe  "))
    assert pdf.extract_text("x.pdf") == "Texto del informe"


def test_extract_text_nonzero_returns_empty(monkeypatch):
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: _Done(1, b""))
    assert pdf.extract_text("x.pdf") == ""


def test_extract_text_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.extract_text("x.pdf") == ""


def test_extract_text_empty_path():
    assert pdf.extract_text(None) == ""
    assert pdf.extract_text("") == ""


def test_thresholds():
    assert pdf.MIN_NARRATIVE == 600
    assert pdf.SCANNED_MAX == 500
    assert pdf.SCANNED_MAX < pdf.MIN_NARRATIVE
