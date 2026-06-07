import subprocess
from ansv_ingest import pdf


class _Done:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


def test_extract_text_returns_stdout(monkeypatch):
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: _Done(0, b"  Full report text  "))
    assert pdf.extract_text("x.pdf") == "Full report text"


def test_extract_text_nonzero_returns_empty(monkeypatch):
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: _Done(1, b""))
    assert pdf.extract_text("x.pdf") == ""


def test_extract_text_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.extract_text("x.pdf") == ""


def test_min_narrative_is_600():
    assert pdf.MIN_NARRATIVE == 600
