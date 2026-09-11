# dgcakw_ingest/pdf.py
"""PDF extraction utilities for dgcakw-ingest.

Reports are bilingual (English + Arabic). We strip Arabic script blocks and
keep only the English body. This gives clean narratives without needing OCR
for these specific text-layer PDFs.

OCR path (via OCR_REMOTE) is available for future scanned reports.
"""
import os
import re
import subprocess

MIN_NARRATIVE = 300     # task floor
SCANNED_FLOOR = 100     # below this → treat as scanned

# Arabic Unicode blocks
_ARABIC_RE = re.compile(
    r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]+[\s؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]*",
    re.UNICODE,
)


def extract_text(pdf_path):
    """Extract text from PDF using pdftotext, then strip Arabic."""
    if not pdf_path or not os.path.exists(pdf_path):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    raw = out.stdout.decode("utf-8", "replace").strip()
    # Strip Arabic script
    clean = _ARABIC_RE.sub(" ", raw)
    # Collapse excess whitespace
    clean = re.sub(r" {3,}", "  ", clean)
    clean = re.sub(r"\n{4,}", "\n\n\n", clean)
    return clean.strip()


def ocr_remote(pdf_path, ocr_remote_host, dest_path=None):
    """OCR a scanned PDF via OCR_REMOTE host (must run as user a1, not root).

    ocr_remote_host: e.g. 'user@prod.example'
    dest_path: where to write OCR'd PDF (default: pdf_path.replace('.pdf','_ocr.pdf'))
    Returns extracted text or empty string on failure.
    """
    if dest_path is None:
        dest_path = pdf_path.replace(".pdf", "_ocr.pdf")
    remote_in = f"/tmp/dgcakw_ocr_in_{os.getpid()}.pdf"
    remote_out = f"/tmp/dgcakw_ocr_out_{os.getpid()}.pdf"
    try:
        subprocess.run(["scp", "-q", pdf_path, f"{ocr_remote_host}:{remote_in}"], check=True, timeout=120)
        subprocess.run(
            ["ssh", ocr_remote_host,
             f"nice -n 19 ionice -c 3 ocrmypdf --language eng --skip-text "
             f"'{remote_in}' '{remote_out}' && cp '{remote_out}' '{remote_in}'"
             ],
            check=True, timeout=600, shell=False,
        )
        subprocess.run(["scp", "-q", f"{ocr_remote_host}:{remote_in}", dest_path], check=True, timeout=120)
        subprocess.run(["ssh", ocr_remote_host, f"rm -f '{remote_in}' '{remote_out}'"], timeout=30)
    except Exception as e:
        print(f"  [ocr_remote] failed: {e}")
        return ""
    return extract_text(dest_path)
