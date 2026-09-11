# bdca_ingest/pdf.py
import subprocess

MIN_NARRATIVE = 600
# PDFs whose extracted text is below this length are treated as scanned
# (image-only) reports with no usable text layer.
SCANNED_FLOOR = 500


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
