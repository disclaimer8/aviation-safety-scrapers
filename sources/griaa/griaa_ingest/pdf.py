# griaa_ingest/pdf.py
import subprocess

# Reports whose extracted text is below this length are treated as scanned
# (image-only) PDFs with no usable text layer.  Many older GRIAA Final reports
# (pre-2012) are scans of paper documents and yield only a few characters.
SCANNED_THRESHOLD = 500


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
