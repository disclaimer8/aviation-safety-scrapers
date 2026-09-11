# aacsv_ingest/pdf.py
import subprocess

# Spanish text-layer reports run long; scanned PDFs (e.g. informe-final-8,
# broken XRef) yield ~0 chars from pdftotext and must be skipped by the
# scanned gate.  MIN_NARRATIVE doubles as that gate (parse → tier 'short'/'none'
# below it; build skips below _NARRATIVE_FLOOR).
MIN_NARRATIVE = 500


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
