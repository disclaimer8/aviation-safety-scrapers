from nsib_ingest import pdf


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


def test_ocr_extract_reads_sidecar(monkeypatch, tmp_path):
    # ocr_extract creates its own tempfile sidecar; emulate ocrmypdf writing it
    captured = {}

    def fake_run(cmd, **k):
        # find the sidecar path arg
        sidecar = cmd[cmd.index("--sidecar") + 1]
        with open(sidecar, "w") as fh:
            fh.write("  OCR recovered text  ")
        captured["lang"] = cmd[cmd.index("--language") + 1]
        return _Done(0, b"")

    monkeypatch.setattr(pdf.subprocess, "run", fake_run)
    out = pdf.ocr_extract("scan.pdf", lang="eng")
    assert out == "OCR recovered text"
    assert captured["lang"] == "eng"


def test_ocr_extract_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_extract("scan.pdf") == ""


def test_ocr_image_returns_stdout(monkeypatch):
    monkeypatch.setattr(pdf.subprocess, "run", lambda *a, **k: _Done(0, b"  jpg report text  "))
    assert pdf.ocr_image("scan.jpg") == "jpg report text"


def test_ocr_image_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_image("scan.jpg") == ""
