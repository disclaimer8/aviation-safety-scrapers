# ciaape_ingest/pdf.py
import subprocess

# Peru CIAA narratives are thinner than usual (~2.4K chars / 5 pages typical),
# which is expected and fine.  The only real gate is the scanned-PDF gate in
# the pipeline (<~500 chars of extracted text -> 'scanned').  MIN_NARRATIVE is
# kept as the tier boundary between 'pdf' and 'short'.
MIN_NARRATIVE = 600


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
