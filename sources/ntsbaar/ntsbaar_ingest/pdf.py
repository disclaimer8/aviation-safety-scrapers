# ntsbaar_ingest/pdf.py
#
# VENDORED from _common/pdf.py — do not edit here.
# Edit the canonical file and run `python -m _common.sync`; a test fails if a
# vendored copy drifts.
import os
import shlex
import subprocess
import tempfile
import uuid

# A PDF whose extracted text is shorter than this is not a usable narrative.
MIN_NARRATIVE = 600

# At or below this, pdftotext found essentially no text layer, so the PDF is a
# scan and OCR is the only way in. (Named for what it is: a ceiling on the
# scanned case, not a floor on the good one.)
SCANNED_MAX = 500


def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote (more powerful) host via ssh.

    Ships the PDF to <host>:/tmp, runs ocrmypdf there under nice/ionice so it
    never starves the remote box's foreground work, emits the OCR text to a
    remote sidecar tempfile and cats it back over ssh stdout, then cleans up.
    Returns "" on any failure. Enabled by env OCR_REMOTE=<host> (e.g.
    user@ocr-host.example) — keeps heavy OCR off a small ingest machine.
    """
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


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


def ocr_extract(pdf_path, lang="eng"):
    """
    OCR a scanned (image-only) PDF and return the recognised text.

    Reusable OCR fallback for image-only reports whose text layer is empty or
    degenerate. When OCR_REMOTE is set the heavy ocrmypdf runs on that host;
    otherwise it runs locally. --force-ocr re-OCRs even a degenerate text layer;
    --output-type none skips rewriting the PDF; --sidecar captures the text.

    lang passes straight to tesseract via --language, so multi-language strings
    like "deu+fra+ita" work. Any failure/timeout/missing binary returns "".
    """
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                [
                    "ocrmypdf",
                    "--force-ocr",
                    "--language", lang,
                    "--sidecar", sidecar,
                    "--output-type", "none",
                    str(pdf_path),
                    "-",
                ],
                capture_output=True, timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    finally:
        try:
            os.unlink(sidecar)
        except OSError:
            pass


def ocr_image(image_path, lang="eng"):
    """
    OCR a standalone image (JPG/PNG) report scan and return recognised text.

    Some bureaus published final reports as scanned images rather than PDFs.
    For an image input we run tesseract directly to stdout. Graceful: missing
    binary, timeout, or non-zero exit returns "".
    """
    if not image_path:
        return ""
    try:
        out = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", lang],
            capture_output=True, timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace").strip()
