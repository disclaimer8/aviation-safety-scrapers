# ntsbcarol_ingest/text.py
"""Text extraction from NTSB PDF reports (pdftotext; OCR via OCR_REMOTE)."""
import os
import subprocess
import sys

MIN_NARRATIVE = 500   # chars; below this the PDF is treated as scanned/empty
MAX_NARRATIVE = 12000  # cap at projection stage (spec says 12K)

# Typical NTSB report section headers (probable cause is near the end)
_PC_HEADERS = [
    "PROBABLE CAUSE",
    "Probable Cause",
    "probable cause",
    "THE NATIONAL TRANSPORTATION SAFETY BOARD DETERMINES",
    "The National Transportation Safety Board determines",
]


def extract_text_pdftotext(pdf_path):
    """Extract text using pdftotext.  Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace")
        return ""
    except Exception as e:
        print(f"[ntsbcarol text] pdftotext error {pdf_path}: {e}", file=sys.stderr)
        return ""


def extract_text_ocr(pdf_path, lang="eng"):
    """Run OCR via OCR_REMOTE (<ocr-host>) as a1.

    Only called when pdftotext yields < MIN_NARRATIVE chars.
    Requires OCR_REMOTE env var to be set.
    """
    remote = os.environ.get("OCR_REMOTE")
    if not remote:
        print(f"[ntsbcarol text] OCR_REMOTE not set, skipping OCR for {pdf_path}", file=sys.stderr)
        return ""
    try:
        # scp pdf to remote, run ocrmypdf, scp back text
        remote_pdf = f"/tmp/ocr_ntsbcarol_{os.path.basename(pdf_path)}"
        remote_txt = remote_pdf.replace(".pdf", ".txt")
        # Copy to remote
        subprocess.run(["scp", "-q", pdf_path, f"{remote}:{remote_pdf}"], check=True, timeout=120)
        # Run ocrmypdf + pdftotext on remote as a1
        cmd = (
            f"sudo -u a1 nice -n 19 ocrmypdf --rotate-pages -l {lang} "
            f"--force-ocr {remote_pdf} {remote_pdf}.ocr.pdf 2>/dev/null && "
            f"pdftotext -layout {remote_pdf}.ocr.pdf {remote_txt}"
        )
        subprocess.run(["ssh", remote, cmd], check=True, timeout=300)
        # Fetch result
        result = subprocess.run(
            ["ssh", remote, f"cat {remote_txt}"],
            capture_output=True, timeout=60, check=True,
        )
        text = result.stdout.decode("utf-8", "replace")
        # Cleanup
        subprocess.run(["ssh", remote, f"rm -f {remote_pdf} {remote_pdf}.ocr.pdf {remote_txt}"], timeout=30)
        return text
    except Exception as e:
        print(f"[ntsbcarol text] OCR error {pdf_path}: {e}", file=sys.stderr)
        return ""


def extract_text(pdf_path):
    """Extract text from PDF (pdftotext first, OCR fallback).

    Returns (text, tier) where tier is 'pdf', 'ocr', or 'none'.
    """
    text = extract_text_pdftotext(pdf_path)
    if len(text) >= MIN_NARRATIVE:
        return text, "pdf"
    # Try OCR fallback
    ocr_text = extract_text_ocr(pdf_path)
    if len(ocr_text) >= MIN_NARRATIVE:
        return ocr_text, "ocr"
    if text:
        return text, "short"
    return "", "none"


def cap_narrative(text, max_chars=MAX_NARRATIVE):
    """Cap narrative text to max_chars for DB storage."""
    if not text:
        return ""
    return text[:max_chars]


def extract_probable_cause(text):
    """Extract the probable cause section from NTSB report text.

    Looks for standard header phrases; returns the paragraph following.
    Returns empty string if not found.
    """
    if not text:
        return ""
    for header in _PC_HEADERS:
        idx = text.find(header)
        if idx < 0:
            continue
        # Take text after the header
        after = text[idx + len(header):]
        # Strip leading whitespace and colons
        after = after.lstrip(" :\n\r")
        # Take up to the next major section or 3000 chars
        # Major section indicators
        cutoffs = ["\n\n\n", "FINDINGS", "SAFETY RECOMMENDATIONS", "CONTRIBUTING FACTORS"]
        end = len(after)
        for cut in cutoffs:
            ci = after.find(cut)
            if 0 < ci < end:
                end = ci
        pc = after[:min(end, 3000)].strip()
        if len(pc) > 50:
            return pc
    return ""
