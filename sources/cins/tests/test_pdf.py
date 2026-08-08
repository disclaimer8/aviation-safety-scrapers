import os
from cins_ingest import pdf

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class _Done:
    def __init__(self, rc, out):
        self.returncode = rc
        self.stdout = out


# ── extract_text ──────────────────────────────────────────────────────────

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


# ── mojibake / garbage gate ───────────────────────────────────────────────

def test_gate_accepts_clean_cyrillic():
    txt = open(os.path.join(_FIX, "clean_cyrillic_sample.txt"), encoding="utf-8").read()
    assert pdf.count_markers(txt) >= 2
    assert pdf.is_usable_text(txt) is True


def test_gate_rejects_mojibake_fixture():
    """Real embedded-font garbage from CINS case 01-24 must be rejected."""
    txt = open(os.path.join(_FIX, "mojibake_sample.txt"), encoding="utf-8").read()
    # It has plenty of characters (would pass a naive char-count gate)...
    assert len(txt) > 600
    # ...but it is gibberish: zero markers, classified unusable.
    assert pdf.count_markers(txt) == 0
    assert pdf.is_usable_text(txt) is False


def test_gate_rejects_synthetic_garbage():
    garbage = "PEIIYEJII4KA CPBI4JA TIEHTAP 34 14CTPAxtI4BAISE HECPEhA Y CAOEPAhAJY " * 30
    assert pdf.is_usable_text(garbage) is False


def test_gate_rejects_empty_and_whitespace():
    assert pdf.is_usable_text("") is False
    assert pdf.is_usable_text("   \n  \t ") is False
    assert pdf.is_usable_text(None) is False


def test_gate_accepts_english_report():
    eng = (
        "FINAL REPORT of the investigation of a serious incident with an "
        "aircraft. Registration YU-ABC. The accident occurred at the airport."
    )
    assert pdf.is_usable_text(eng) is True


def test_gate_accepts_serbian_latin():
    sr = (
        "IZVESTAJ o istrazi udesa vazduhoplova. Avion je registrovan kao "
        "YU-DOT. Komisija je utvrdila uzroke nezgode na aerodromu."
    )
    assert pdf.is_usable_text(sr) is True
