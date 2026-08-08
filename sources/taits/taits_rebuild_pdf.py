#!/usr/bin/env python3
"""
taits_rebuild_pdf.py  —  Rebuild taits_accidents with full-PDF narratives.

Phase 1  (run synchronously):
  - For each taits_reports row with pdf_path:
      * pdftotext extraction
      * usability gate (>= 300 chars, printable ratio >= 0.85)
      * if usable: build/update taits_accidents row immediately
      * else: mark as needs_ocr, save to ocr_queue file

Phase 2  (background via systemd-run):
  - Run OCR on queue via remote hetzner, then rebuild those rows.

Usage:
  python3 taits_rebuild_pdf.py --phase1          # run synchronously now
  python3 taits_rebuild_pdf.py --phase2          # OCR + rebuild for queue
  python3 taits_rebuild_pdf.py --stats           # show current stats only
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# Resolve paths
INGEST_DIR = os.path.expanduser("~/taits-ingest")
DB_PATH = os.path.join(INGEST_DIR, "taits.db")
OCR_QUEUE = os.path.join(INGEST_DIR, "ocr_queue.json")
LOG_PATH = os.path.join(INGEST_DIR, "ocr-backfill.log")

# Import aaid ocr_extract (reuse proven remote OCR implementation)
sys.path.insert(0, os.path.expanduser("~/aaid-ingest"))
from aaid_ingest.pdf import ocr_extract

# Local taits modules
sys.path.insert(0, INGEST_DIR)
from taits_ingest.db import connect, now_ms
from taits_ingest.text import make_site_slug

# ── Constants ────────────────────────────────────────────────────────────────

MIN_USABLE_CHARS = 300        # pdftotext output must be >= this to be usable
PRINTABLE_RATIO_MIN = 0.85    # fraction of printable chars to catch mojibake/form-feeds
NARRATIVE_FLOOR = 80          # scraper floor; rows below this are skipped
OCR_LANG = "lit+eng"          # tesseract language for Lithuanian+English reports

# LT stopwords / diacritics used for language detection
_LT_DIACRITICS = re.compile(r'[ąčęėįšųūž]', re.IGNORECASE)
_LT_STOPWORDS = re.compile(
    r'\b(orlaivis|sklandytuvas|tyrimas|avarija|ataskaita|pilotas|'
    r'nustatytas|tyrimo|komisija|priežastis|įvyko|skrydžio|išvados)\b',
    re.IGNORECASE,
)
_EN_AVIATION = re.compile(
    r'\b(aircraft|accident|investigation|flight|pilot|landing|takeoff|'
    r'runway|altitude|engine|report|incident)\b',
    re.IGNORECASE,
)


# ── Text utilities ────────────────────────────────────────────────────────────

def extract_pdftotext(pdf_path):
    """Run pdftotext and return raw text (may be empty for scanned PDFs)."""
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


def is_usable_text(text):
    """True if text is substantive prose (not a scanned / mojibake / form-feed dump)."""
    if not text or len(text) < MIN_USABLE_CHARS:
        return False
    # Reject pure form-feed / control-char output (scanned PDFs yield 0x0c runs)
    printable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
    if printable / len(text) < PRINTABLE_RATIO_MIN:
        return False
    return True


def detect_lang(text):
    """
    Heuristic language detection: 'lt' | 'en' | 'other'.
    Counts LT diacritics/stopwords vs EN aviation keywords.
    """
    lt_score = len(_LT_DIACRITICS.findall(text)) + len(_LT_STOPWORDS.findall(text))
    en_score = len(_EN_AVIATION.findall(text))
    if lt_score > en_score:
        return "lt"
    if en_score > 0:
        return "en"
    return "en"   # default to 'en' for ambiguous (older reports in Polish/Russian etc.)


def extract_probable_cause(text):
    """
    Extract the Conclusions/Causes section from the narrative text.
    Looks for EN and LT section headers; returns the section text or None.
    """
    pattern = re.compile(
        r'(?m)^[ \t\d.]*(?:CONCLUSIONS?|CAUSES?|CONTRIBUTING FACTORS?|'
        r'IŠ?VADOS?|PRIEŽAST(?:IS|YS|I[EŲ]))\s*[:\-]?\s*$',
        re.IGNORECASE
    )
    inline_pattern = re.compile(
        r'(?m)(?:^[ \t\d.]*(?:CONCLUSIONS?|CAUSES?|IŠ?VADOS?|PRIEŽAST(?:IS|YS|I[EŲ]))'
        r'[ \t]*[:\-]\s*(.+?)$)',
        re.IGNORECASE
    )

    m = pattern.search(text)
    if m:
        start = m.end()
        chunk = text[start:start + 2000].strip()
        stop = re.search(r'\n[ \t\d.]*[A-ZĮŠŲŪŽ][A-ZĮŠŲŪŽ ]{3,}\s*\n', chunk)
        if stop:
            chunk = chunk[:stop.start()].strip()
        if len(chunk) >= 40:
            return chunk[:1200]

    im = inline_pattern.search(text)
    if im:
        val = im.group(1).strip()
        if len(val) >= 30:
            return val[:600]

    return None


# ── DB helpers ───────────────────────────────────────────────────────────────

def build_accident_row(conn, row, narrative, lang, probable_cause):
    """INSERT OR REPLACE a taits_accidents row."""
    if isinstance(row, dict):
        get = lambda k: row.get(k)
    else:
        get = lambda k: row[k]

    source_url = get("detail_url") or get("report_url") or get("pdf_url")
    site_slug = make_site_slug(get("aircraft"), get("registration"), get("location"))
    conn.execute(
        "INSERT OR REPLACE INTO taits_accidents "
        "(case_id, event_date, aircraft, registration, operator, location, "
        " country, narrative_text, probable_cause, source_url, report_type, "
        " site_slug, lang, built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            get("case_id"), get("event_date"), get("aircraft"),
            get("registration"), get("operator"), get("location"), "LT",
            narrative, probable_cause, source_url, get("report_type"),
            site_slug, lang, now_ms(),
        ),
    )
    conn.commit()


# ── Phase 1: pdftotext pass ──────────────────────────────────────────────────

def phase1(conn):
    """
    Process all PDF-mapped reports with pdftotext.
    - Usable -> build taits_accidents immediately.
    - Not usable -> add to OCR queue.
    """
    pdf_rows = conn.execute(
        "SELECT case_id, kind, event_date, aircraft, registration, operator, "
        "location, report_type, detail_url, pdf_url, report_url, pdf_path, "
        "summary_text FROM taits_reports "
        "WHERE pdf_path IS NOT NULL AND pdf_path != ''"
    ).fetchall()

    ocr_queue = []
    built = 0
    queued = 0

    print(f"\n[Phase 1] Processing {len(pdf_rows)} PDF-mapped reports...", flush=True)

    for i, row in enumerate(pdf_rows, 1):
        pdf_path = row["pdf_path"]
        case_id = row["case_id"]

        text = extract_pdftotext(pdf_path)
        usable = is_usable_text(text)

        if usable:
            lang = detect_lang(text)
            probable_cause = extract_probable_cause(text)
            build_accident_row(conn, row, text, lang, probable_cause)
            built += 1
            print(f"  [{i}/{len(pdf_rows)}] BUILT   {case_id}  len={len(text)}  lang={lang}  "
                  f"cause={'yes' if probable_cause else 'no'}", flush=True)
        else:
            ocr_queue.append({k: row[k] for k in row.keys()})
            queued += 1
            print(f"  [{i}/{len(pdf_rows)}] OCR_Q  {case_id}  pdftext_len={len(text)}", flush=True)

    # Save OCR queue
    with open(OCR_QUEUE, "w") as f:
        json.dump(ocr_queue, f, indent=2, default=str)

    print(f"\n[Phase 1] Done: built={built}, queued_for_ocr={queued}")
    print(f"[Phase 1] OCR queue saved to {OCR_QUEUE}")
    return built, queued


# ── Phase 2: OCR pass ────────────────────────────────────────────────────────

def phase2(conn):
    """
    For each entry in ocr_queue.json: run OCR, build/update taits_accidents.
    Falls back to summary_text if OCR also fails.
    """
    if not os.path.exists(OCR_QUEUE):
        print("[Phase 2] No OCR queue found. Run --phase1 first.", flush=True)
        return 0

    with open(OCR_QUEUE) as f:
        queue = json.load(f)

    print(f"\n[Phase 2] OCR processing {len(queue)} reports...", flush=True)

    ocr_built = 0
    summary_fallback = 0
    truly_skipped = 0

    for i, row_dict in enumerate(queue, 1):
        case_id = row_dict["case_id"]
        pdf_path = row_dict["pdf_path"]
        summary = (row_dict.get("summary_text") or "").strip()

        print(f"\n  [{i}/{len(queue)}] OCR {case_id}  pdf={os.path.basename(pdf_path)}", flush=True)
        sys.stdout.flush()

        ocr_text = ocr_extract(pdf_path, lang=OCR_LANG)
        print(f"         OCR result: len={len(ocr_text)}", flush=True)

        if len(ocr_text) >= NARRATIVE_FLOOR:
            lang = detect_lang(ocr_text)
            probable_cause = extract_probable_cause(ocr_text)
            build_accident_row(conn, row_dict, ocr_text, lang, probable_cause)
            ocr_built += 1
            print(f"         BUILT via OCR  len={len(ocr_text)}  lang={lang}  "
                  f"cause={'yes' if probable_cause else 'no'}", flush=True)
        elif len(summary) >= NARRATIVE_FLOOR:
            lang = detect_lang(summary)
            build_accident_row(conn, row_dict, summary, lang, None)
            summary_fallback += 1
            print(f"         BUILT via summary fallback  len={len(summary)}", flush=True)
        else:
            print(f"         SKIP (ocr={len(ocr_text)} summary={len(summary)} both < floor)", flush=True)
            truly_skipped += 1

    print(f"\n[Phase 2] Done: ocr_built={ocr_built}, summary_fallback={summary_fallback}, "
          f"truly_skipped={truly_skipped}", flush=True)
    return ocr_built


# ── Stats ────────────────────────────────────────────────────────────────────

def show_stats(conn):
    """Print current taits_accidents stats."""
    total = conn.execute("SELECT count(*) FROM taits_accidents").fetchone()[0]
    ge300 = conn.execute(
        "SELECT count(*) FROM taits_accidents WHERE length(narrative_text) >= 300"
    ).fetchone()[0]

    narr_lengths = [r[0] for r in conn.execute(
        "SELECT length(narrative_text) FROM taits_accidents ORDER BY length(narrative_text)"
    ).fetchall()]
    median_len = narr_lengths[len(narr_lengths)//2] if narr_lengths else 0

    by_lang = conn.execute(
        "SELECT lang, count(*), min(length(narrative_text)), max(length(narrative_text)) "
        "FROM taits_accidents GROUP BY lang"
    ).fetchall()

    print(f"\n=== taits_accidents stats ===")
    print(f"  Total rows:    {total}")
    print(f"  >= 300 chars:  {ge300}  (prod-viable)")
    print(f"  < 300 chars:   {total - ge300}")
    if narr_lengths:
        print(f"  Length min/median/max: {narr_lengths[0]} / {median_len} / {narr_lengths[-1]}")
    print(f"  By language:")
    for r in by_lang:
        print(f"    lang={r[0]}  count={r[1]}  min={r[2]}  max={r[3]}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase1", action="store_true")
    parser.add_argument("--phase2", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    conn = connect(DB_PATH)

    if args.stats:
        show_stats(conn)
        return

    if args.phase1:
        phase1(conn)
        show_stats(conn)
        return

    if args.phase2:
        phase2(conn)
        show_stats(conn)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
