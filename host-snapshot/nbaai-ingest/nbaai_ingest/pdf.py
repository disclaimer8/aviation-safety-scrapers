# nbaai_ingest/pdf.py
"""PDF text extraction + a mojibake / scanned detection gate + OCR fallback.

Ukraine NBAAI (НБРТ / НБРЦА) report PDFs come in several forms after pdftotext:
  1. Clean Unicode Cyrillic (Ukrainian / Russian) — real text, keep (tier 'pdf').
  2. Clean Latin / English (foreign-state co-reports) — real text, keep.
  3. ⚠️ A non-Unicode embedded Cyrillic font: pdftotext emits GARBAGE (mojibake)
     that *looks* Latin by codepoint and easily clears a raw char-count gate.
     These must be detected and skipped/OCR'd so they never become a narrative.
  4. Scanned image PDFs (e.g. l-410_ur-two.pdf, a 17 MB scan): pdftotext yields
     ~empty output (a few dozen chars).

is_usable_text() is the reusable mojibake/scanned gate (ported from cins_ingest,
with Ukrainian aviation stems added so a CLEAN Ukrainian text-layer is never
falsely rejected).  Forms 3 and 4 score zero/near-zero markers and fail it;
genuine clean reports score well above the threshold.

ocr_extract() is the OCR fallback (ported verbatim from aaid_ingest, proven on
Kenya): ocrmypdf --force-ocr over a degenerate/garbled text layer, lang
"ukr+rus" for Ukraine.  Runs ONLY on a host with tesseract+ocrmypdf+ukr+rus.
"""
import os
import subprocess
import tempfile

MIN_NARRATIVE = 600

# Minimum distinct marker words a usable report must contain.
MIN_MARKERS = 2

# Marker words spanning the legitimate text forms.  Chosen so that mojibake-font
# output (which mangles every glyph) matches NONE of them.
_MARKERS = (
    # Ukrainian aviation stems (NBAAI is Ukrainian; older reports Russian)
    "літак", "авіа", "розслідуванн", "розслідуван", "екіпаж", "аеродром",
    "катастроф", "інцидент", "повітрян", "політ", "пілот", "звіт",
    "реєстрац", "вертол",
    # Russian aviation stems (older Soviet-era / Russian-language reports)
    "самолет", "самолёт", "расследован", "экипаж", "аэродром",
    "катастроф", "инцидент", "воздушн", "полет", "полёт", "пилот",
    "отчет", "отчёт", "регистрац", "вертол",
    # Cyrillic generic
    "комісія", "комиссия", "судно", "подія", "событие",
    # English (foreign-state reports)
    "report", "aircraft", "accident", "incident", "investigation",
    "registration", "airport", "crew", "flight",
)


def count_markers(text):
    """Number of distinct marker words present (case-insensitive)."""
    if not text:
        return 0
    low = text.lower()
    return sum(1 for m in _MARKERS if m in low)


def is_usable_text(text):
    """True when extracted text is real Ukrainian/Russian/English report prose.

    Rejects mojibake (garbled embedded-font output) and scanned/empty PDFs by
    requiring at least MIN_MARKERS recognizable aviation marker words.  This is
    intentionally script-agnostic: it accepts Cyrillic (Ukrainian + Russian)
    and English alike, and only codepoint-garbage mojibake / empty scans fail.
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


def ocr_extract(pdf_path, lang="eng"):
    """
    OCR a scanned (image-only) or mojibake-font PDF and return recognised text.

    Reusable OCR fallback for image-only reports whose text layer is empty or
    degenerate.  Runs ocrmypdf with --force-ocr (re-OCR even over an existing
    degenerate text layer), --output-type none (skip writing a rewritten PDF —
    we only want the text), and --sidecar <tmp.txt> (emit OCR text to a file we
    then read back).  Output PDF target is '-' (stdout, discarded with
    --output-type none).

    lang is passed straight to tesseract via ocrmypdf --language, so callers can
    use multi-language strings, e.g. "ukr+rus" (Ukraine) or "eng" (Kenya/Nigeria).

    Graceful: any ocrmypdf failure, timeout, or missing binary returns "".
    Generous 600s per-PDF timeout (large multi-page scans are slow).
    """
    if not pdf_path:
        return ""
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                [
                    "ocrmypdf",
                    "--force-ocr",
                    "--language", lang,
                    "--sidecar", sidecar,
                    "--output-type", "none",
                    str(pdf_path),
                    "-",
                ],
                capture_output=True, timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    finally:
        try:
            os.unlink(sidecar)
        except OSError:
            pass
