# baaid_ingest/pdf.py
import subprocess

MIN_NARRATIVE = 600

# Scanned/image-only reports yield little or no extractable text.  Anything at
# or below this length is treated as a scan we cannot ingest (tier 'scanned').
SCANNED_CEILING = 500


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
