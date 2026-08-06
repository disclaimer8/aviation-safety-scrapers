# nsib_ingest/pdf.py
import os
import shlex
import subprocess
import tempfile
import uuid

MIN_NARRATIVE = 600


def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote (more powerful) host via ssh.

    Ships the PDF to <host>:/tmp, runs ocrmypdf there under nice/ionice so it
    never starves the remote box's foreground work (the prod web server), emits
    the OCR text to a remote sidecar tempfile and cats it back over ssh stdout,
    then cleans up. Returns "" on any failure. Enabled by env OCR_REMOTE=<host>
    (e.g. user@ocr-host.example) — keeps heavy OCR off the loaded mini-PC.
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
    degenerate.  Runs ocrmypdf with --force-ocr (re-OCR even over an existing
    degenerate text layer), --output-type none (skip writing a rewritten PDF —
    we only want the text), and --sidecar <tmp.txt> (emit OCR text to a file we
    then read back).  Output PDF target is '-' (stdout, discarded with
    --output-type none).

    lang is passed straight to tesseract via ocrmypdf --language, so callers can
    use multi-language strings, e.g. "ukr+rus" (Ukraine) or "eng" (Kenya/Nigeria).

    Graceful: any ocrmypdf failure, timeout, or missing binary returns "".
    Generous 600s per-PDF timeout (large multi-page scans are slow).
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

    NSIB historically published some final reports as scanned JPGs (not PDFs).
    For an image input we run tesseract directly to stdout.  Graceful: missing
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
