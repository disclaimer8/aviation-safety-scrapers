import subprocess
from nbaai_ingest import pdf


class _Done:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


# ── extract_text ─────────────────────────────────────────────────────────────

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


# ── mojibake / scanned gate (is_usable_text) ─────────────────────────────────

CLEAN_UK = (
    "21.10.2019 при виконанні приватного польоту на вертольоті Robinson R-44 "
    "сталася катастрофа. Розслідування події проводить комісія НБРЦА. "
    "Екіпаж загинув. Тип ПС: вертоліт."
)
CLEAN_RU = (
    "Расследование катастрофы самолёта. Экипаж воздушного судна. "
    "Регистрация борта. Отчёт комиссии по расследованию инцидента."
)
CLEAN_EN = (
    "Final report of the investigation into the accident of the aircraft. "
    "The crew, the registration and the flight to the airport."
)
# Non-Unicode embedded-font output: garbled Latin codepoints, NO marker words.
MOJIBAKE = "PEIIYEJII4KA CPBI4JA HECPEhA Y CAOEPAhAJY 3BJIITP KOMHCH " * 15
SCANNED = "  \x0c  "  # pdftotext on an image PDF: a couple of whitespace chars


def test_clean_ukrainian_is_usable():
    assert pdf.is_usable_text(CLEAN_UK) is True


def test_clean_russian_is_usable():
    assert pdf.is_usable_text(CLEAN_RU) is True


def test_clean_english_is_usable():
    assert pdf.is_usable_text(CLEAN_EN) is True


def test_mojibake_is_not_usable():
    # Garbled non-Unicode font output passes a raw char-count but must FAIL the gate.
    assert len(MOJIBAKE) > pdf.MIN_NARRATIVE
    assert pdf.is_usable_text(MOJIBAKE) is False


def test_scanned_empty_is_not_usable():
    assert pdf.is_usable_text(SCANNED) is False
    assert pdf.is_usable_text("") is False


def test_count_markers_counts_distinct():
    assert pdf.count_markers(CLEAN_UK) >= pdf.MIN_MARKERS


# ── ocr_extract ──────────────────────────────────────────────────────────────

def test_ocr_extract_reads_sidecar(monkeypatch, tmp_path):
    recovered = "РОЗСЛІДУВАННЯ КАТАСТРОФИ ЛІТАКА. ЕКІПАЖ. АЕРОДРОМ."

    def fake_run(cmd, **kw):
        # ocrmypdf writes OCR text to the --sidecar path
        idx = cmd.index("--sidecar")
        with open(cmd[idx + 1], "w", encoding="utf-8") as fh:
            fh.write(recovered)
        return _Done(0, b"")

    monkeypatch.setattr(pdf.subprocess, "run", fake_run)
    assert pdf.ocr_extract("scan.pdf", "ukr+rus") == recovered


def test_ocr_extract_missing_binary_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_extract("scan.pdf", "ukr+rus") == ""


def test_ocr_extract_timeout_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ocrmypdf", timeout=600)
    monkeypatch.setattr(pdf.subprocess, "run", boom)
    assert pdf.ocr_extract("scan.pdf", "ukr+rus") == ""
