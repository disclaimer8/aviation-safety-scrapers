# ipiaam_ingest/pdf.py
import subprocess

MIN_NARRATIVE = 600  # chars; below = treat as scanned or short


def extract_text(pdf_path):
    """Run pdftotext on pdf_path; return decoded text or empty string."""
    if not pdf_path:
        return ''
    try:
        out = subprocess.run(
            ['pdftotext', '-q', str(pdf_path), '-'],
            capture_output=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ''
    if out.returncode != 0:
        return ''
    return out.stdout.decode('utf-8', 'replace').strip()
