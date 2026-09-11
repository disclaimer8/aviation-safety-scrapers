# ainhr_ingest/pdf.py
import subprocess

MIN_NARRATIVE = 600

# Below this char count a text-layer PDF is almost certainly a scanned image
# (only cover/header OCR leaks through) -> treat as 'scanned' and skip.
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
