# ciaiauy_ingest/pdf.py
import subprocess

# Threshold below which an extracted PDF is treated as a scanned (image-only)
# report: pdftotext yields little/no text layer.  Such rows are tiered
# 'scanned' and skipped at build time (no usable narrative).
MIN_NARRATIVE = 600
SCANNED_MAX = 500  # pdftotext output <= this many chars => scanned image PDF


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
