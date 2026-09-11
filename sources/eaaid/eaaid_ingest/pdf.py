# eaaid_ingest/pdf.py
"""PDF extraction for EAAID (Egypt) reports.

Reports are bilingual (English + Arabic). We strip Arabic script and keep
only the English body.

OCR path available via OCR_REMOTE for scanned PDFs.
"""
import os
import re
import subprocess

MIN_NARRATIVE = 300     # floor to count as usable narrative
SCANNED_FLOOR = 100     # below this -> treat as scanned

# Arabic Unicode blocks (same pattern as dgcakw)
_ARABIC_RE = re.compile(
    r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+[\s؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]*",
    re.UNICODE,
)


def extract_text(pdf_path):
    """Extract text from PDF using pdftotext, strip Arabic script."""
    if not pdf_path or not os.path.exists(pdf_path):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    raw = out.stdout.decode("utf-8", "replace").strip()
    # Strip Arabic script blocks
    clean = _ARABIC_RE.sub(" ", raw)
    # Collapse excess whitespace
    clean = re.sub(r" {4,}", "  ", clean)
    clean = re.sub(r"\n{5,}", "\n\n\n", clean)
    return clean.strip()


def is_usable_text(text):
    """True if text has enough English content to be useful."""
    return len(text) >= MIN_NARRATIVE


def ocr_remote(pdf_path, ocr_remote_host, dest_path=None):
    """OCR a scanned PDF via OCR_REMOTE host (run as user a1, not root).

    ocr_remote_host: e.g. 'user@prod.example'
    Returns extracted text or empty string on failure.
    """
    if dest_path is None:
        dest_path = pdf_path.replace(".pdf", "_ocr.pdf")
    remote_in  = f"/tmp/eaaid_ocr_in_{os.getpid()}.pdf"
    remote_out = f"/tmp/eaaid_ocr_out_{os.getpid()}.pdf"
    try:
        subprocess.run(
            ["scp", "-q", pdf_path, f"{ocr_remote_host}:{remote_in}"],
            check=True, timeout=120,
        )
        subprocess.run(
            ["ssh", ocr_remote_host,
             f"nice -n 19 ionice -c 3 ocrmypdf --language eng --skip-text "
             f"'{remote_in}' '{remote_out}' && cp '{remote_out}' '{remote_in}'"],
            check=True, timeout=600,
        )
        subprocess.run(
            ["scp", "-q", f"{ocr_remote_host}:{remote_in}", dest_path],
            check=True, timeout=120,
        )
        subprocess.run(
            ["ssh", ocr_remote_host, f"rm -f '{remote_in}' '{remote_out}'"],
            timeout=30,
        )
    except Exception as exc:
        print(f"  [ocr_remote] failed: {exc}")
        return ""
    return extract_text(dest_path)
