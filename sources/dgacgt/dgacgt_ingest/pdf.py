# dgacgt_ingest/pdf.py
import subprocess

MIN_NARRATIVE = 600
# pdftotext output shorter than this => almost certainly a scanned/image PDF.
SCANNED_THRESHOLD = 500


def extract_text(pdf_path):
    if not pdf_path:
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(pdf_path), "-"],
            capture_output=True, timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()
