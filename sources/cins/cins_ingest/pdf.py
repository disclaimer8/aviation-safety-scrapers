# cins_ingest/pdf.py
"""PDF text extraction + a mojibake / scanned detection gate.

CINS reports come in three forms after pdftotext:
  1. Clean Unicode Cyrillic (most reports) — real text, keep.
  2. Clean Latin / English (foreign-state reports) — real text, keep.
  3. ⚠️ A non-Unicode embedded Cyrillic font: pdftotext emits GARBAGE that
     *looks* Latin by codepoint and easily clears a raw char-count gate, e.g.
     the header "РЕПУБЛИКА СРБИЈА … НЕСРЕЋА У САОБРАЋАЈУ" extracts as
     "PEIIYEJII4KA CPBI4JA … HECPEhA Y CAOEPAhAJY".  These must be detected
     and skipped so they never become a narrative.
  4. Scanned image PDFs: pdftotext yields ~empty output.

is_usable_text() is the reusable gate.  A report is usable only if its text
contains enough recognizable Serbian-Cyrillic / Serbian-Latin / English
aviation marker words.  Mojibake (form 3) and scanned (form 4) score zero
markers and are rejected; every genuine report observed in the live scout
scored >= 6.
"""
import subprocess

MIN_NARRATIVE = 600

# Minimum distinct marker words a usable report must contain.
MIN_MARKERS = 2

# Marker words spanning the three legitimate text forms.  Chosen so that the
# mojibake-font output (which mangles every glyph) matches NONE of them.
_MARKERS = (
    # Serbian Cyrillic
    "извештај", "ваздухоплов", "удес", "нез", "саобраћај", "авион",
    "република", "комисија", "регист", "аеродром",
    # Serbian Latin
    "izveštaj", "izvestaj", "vazduhoplov", "udes", "nezgod",
    "saobraćaj", "saobracaj", "avion", "komisija", "registar", "aerodrom",
    # English (foreign-state reports)
    "report", "aircraft", "accident", "incident", "investigation",
    "registration", "airport",
)


def count_markers(text):
    """Number of distinct marker words present (case-insensitive)."""
    if not text:
        return 0
    low = text.lower()
    return sum(1 for m in _MARKERS if m in low)


def is_usable_text(text):
    """True when extracted text is real Serbian/English report prose.

    Rejects mojibake (garbled embedded-font output) and scanned/empty PDFs by
    requiring at least MIN_MARKERS recognizable aviation marker words.  This
    is intentionally script-agnostic: it accepts Cyrillic, Serbian-Latin and
    English alike, and only the codepoint-garbage mojibake fails it.
    """
    if not text or not text.strip():
        return False
    return count_markers(text) >= MIN_MARKERS


def extract_text(pdf_path):
    if not pdf_path:
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()
