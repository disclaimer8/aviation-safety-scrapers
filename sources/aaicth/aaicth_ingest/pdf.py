# aaicth_ingest/pdf.py
"""PDF text extraction for AAIC Thailand reports.

Two-path strategy:
1. pdftotext: works for native-text PDFs (most EN reports; some TH).
2. OCR (tesseract tha+eng via ocrmypdf on remote hetzner): for scanned/
   image-only PDFs or Thai PDFs that yield mojibake/empty text.

Thai PDF heuristic:
  - If pdftotext returns >= SCANNED_THRESHOLD bytes of text, accept it.
  - If pdftotext returns < SCANNED_THRESHOLD and lang='th', try OCR with
    tesseract-ocr-tha (installed on hetzner).
  - If lang='en' and pdftotext fails, try OCR with eng.
"""
import os
import subprocess
import tempfile

SCANNED_THRESHOLD = 300  # chars; below this we try OCR


def extract_text(pdf_path: str) -> str:
    """Extract text from pdf_path via pdftotext.  Returns '' on failure."""
    if not pdf_path:
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()


def _ocr_remote(pdf_path: str, lang: str = "tha+eng") -> str:
    """Run OCR on hetzner via OCR_REMOTE env var (mirrors existing ocr_extract pattern).

    Copies pdf to remote, runs ocrmypdf, retrieves result, extracts text.
    Returns '' on any failure.
    """
    ocr_remote = os.environ.get("OCR_REMOTE", "")
    if not ocr_remote:
        return ""
    try:
        remote_in = f"/tmp/aaicth_ocr_in_{os.getpid()}.pdf"
        remote_out = f"/tmp/aaicth_ocr_out_{os.getpid()}.pdf"

        # scp source pdf to remote
        r = subprocess.run(
            ["scp", "-q", str(pdf_path), f"{ocr_remote}:{remote_in}"],
            capture_output=True, timeout=120,
        )
        if r.returncode != 0:
            return ""

        # Run ocrmypdf on remote
        r = subprocess.run(
            ["ssh", ocr_remote,
             f"nice -n 10 ionice -c 3 ocrmypdf -q --language {lang} "
             f"--skip-text --output-type pdf {remote_in} {remote_out} && "
             f"pdftotext -enc UTF-8 {remote_out} - ; "
             f"rm -f {remote_in} {remote_out}"],
            capture_output=True, timeout=300,
        )
        if r.returncode not in (0, 6):  # 6 = already has text layer (ok)
            # Try pdftotext directly on remote_out anyway
            pass
        text = r.stdout.decode("utf-8", "replace").strip()
        return text
    except Exception:
        return ""


def extract_with_ocr_fallback(pdf_path: str, lang: str = "en") -> tuple[str, str]:
    """Extract text, falling back to OCR if pdftotext yields too little.

    Returns (text, tier):
      tier = 'pdf'     — native text layer used
      tier = 'ocr'     — OCR used
      tier = 'scanned' — text < threshold even after OCR attempt
      tier = 'none'    — no PDF / empty
    """
    if not pdf_path:
        return "", "none"

    raw = extract_text(pdf_path)
    if len(raw) >= SCANNED_THRESHOLD:
        return raw, "pdf"

    # Try OCR fallback
    ocr_lang = "tha+eng" if lang == "th" else "eng"
    ocr_text = _ocr_remote(pdf_path, lang=ocr_lang)
    if len(ocr_text) >= SCANNED_THRESHOLD:
        return ocr_text, "ocr"

    # Return whatever we have (could be original pdftotext partial text)
    best = ocr_text if len(ocr_text) >= len(raw) else raw
    if best:
        return best, "scanned"
    return "", "none"
