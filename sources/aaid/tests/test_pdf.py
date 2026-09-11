import os
import subprocess

from aaid_ingest import pdf


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


def test_extract_text_empty_path():
    assert pdf.extract_text("") == ""
    assert pdf.extract_text(None) == ""


def test_min_narrative_is_600():
    assert pdf.MIN_NARRATIVE == 600


# ── ocr_extract (ocrmypdf mocked — no tesseract/ocrmypdf needed) ──────────────

def test_ocr_extract_reads_sidecar(monkeypatch):
    """ocrmypdf writes the sidecar; ocr_extract reads & strips it."""
    def _run(cmd, **kwargs):
        sidecar = cmd[cmd.index("--sidecar") + 1]
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write("  OCR recovered narrative  \n")
        return _Done(0, b"")
    monkeypatch.setattr(pdf.subprocess, "run", _run)
    assert pdf.ocr_extract("scan.pdf") == "OCR recovered narrative"


def test_ocr_extract_passes_lang_and_force_flags(monkeypatch):
    seen = {}
    def _run(cmd, **kwargs):
        seen["cmd"] = cmd
        sidecar = cmd[cmd.index("--sidecar") + 1]
        open(sidecar, "w").write("text")
        return _Done(0, b"")
    monkeypatch.setattr(pdf.subprocess, "run", _run)
    pdf.ocr_extract("scan.pdf", lang="ukr+rus")
    cmd = seen["cmd"]
    assert cmd[0] == "ocrmypdf"
    assert "--force-ocr" in cmd
    assert cmd[cmd.index("--language") + 1] == "ukr+rus"
    assert cmd[cmd.index("--output-type") + 1] == "none"
    assert "--sidecar" in cmd


def test_ocr_extract_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_extract("scan.pdf") == ""


def test_ocr_extract_timeout_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ocrmypdf", timeout=600)
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_extract("scan.pdf") == ""


def test_ocr_extract_empty_path():
    assert pdf.ocr_extract("") == ""
    assert pdf.ocr_extract(None) == ""


def test_ocr_extract_no_sidecar_written_returns_empty(monkeypatch):
    """ocrmypdf failed before writing sidecar (file emptied) -> ''."""
    def _run(cmd, **kwargs):
        # leave sidecar empty (mkstemp created it empty)
        return _Done(1, b"")
    monkeypatch.setattr(pdf.subprocess, "run", _run)
    assert pdf.ocr_extract("scan.pdf") == ""


def test_ocr_extract_cleans_up_sidecar(monkeypatch):
    """The temp sidecar file is removed after the call."""
    captured = {}
    def _run(cmd, **kwargs):
        captured["sidecar"] = cmd[cmd.index("--sidecar") + 1]
        open(captured["sidecar"], "w").write("data")
        return _Done(0, b"")
    monkeypatch.setattr(pdf.subprocess, "run", _run)
    pdf.ocr_extract("scan.pdf")
    assert not os.path.exists(captured["sidecar"])
