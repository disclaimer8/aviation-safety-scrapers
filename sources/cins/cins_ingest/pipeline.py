# cins_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for CINS (Serbia).

discover(): GET the single aviation listing page, parse_listing() its <tr>
  rows, INSERT new case_ids into cins_reports with full listing metadata
  (aircraft / event_class / registration / date / location).  Idempotent —
  existing case_ids are skipped.  lang is left None (set at parse time from
  the extracted text's script).

fetch(): for each status='new' row with a pdf_url, download the PDF and
  advance to 'fetched'.  Per-row try/except: a download failure keeps the row
  at 'new' for the next run.

parse(): extract text via pdftotext, then apply the mojibake/scanned gate.
  source_tier:
    'pdf'     usable text >= MIN_NARRATIVE chars
    'short'   usable text but below threshold
    'scanned' text present but FAILS is_usable_text (mojibake or scanned-OCR
              garbage) — narrative is cleared so build() skips it
    'none'    no text at all
  lang is set to 'sr' for Cyrillic/Latin Serbian text and 'en' when the text
  is predominantly Latin English; informational only.

build(): emit cins_accidents rows.  Rows whose narrative_text is shorter than
  _NARRATIVE_FLOOR (80 chars) are skipped (covers 'scanned'/'none'/'short').
"""
import os
import sys
import time

from . import cins, db, text
from .pdf import extract_text, is_usable_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def _detect_lang(t):
    """Rough lang tag: 'sr' if any Cyrillic, else 'en' for mostly-Latin text."""
    if not t:
        return None
    if any(0x400 <= ord(ch) <= 0x4ff for ch in t):
        return "sr"
    return "en"


def discover(conn, client, full=False):
    """Walk the single CINS listing page and INSERT new case_ids.

    full: accepted for API parity; has no extra effect (the whole page is
          always parsed; per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    resp = client.get(cins.INDEX_URL)
    resp.raise_for_status()
    body = resp.content
    index_html = body.decode("utf-8", "replace") if isinstance(body, bytes) else body

    rows = cins.parse_listing(index_html)

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM cins_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO cins_reports "
            "(case_id, report_url, pdf_url, pdf_url_es, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("pdf_url_es"),
                row.get("pdf_url_en"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
                None,
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows and advance to 'fetched'.

    A download failure keeps the row at 'new' for retry.  Returns the number
    of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM cins_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe_case_id = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe_case_id + ".pdf")
            try:
                time.sleep(cins.DELAY)
                cins.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[cins fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE cins_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[cins fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract + gate text for status='fetched' rows. Returns rows processed."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM cins_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier, lang = "", "none", None
        elif not is_usable_text(full_text):
            # mojibake (garbled embedded font) or scanned-OCR garbage — drop
            narrative, tier, lang = "", "scanned", None
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier, lang = full_text, "pdf", _detect_lang(full_text)
        else:
            narrative, tier, lang = full_text, "short", _detect_lang(full_text)

        conn.execute(
            "UPDATE cins_reports "
            "SET narrative_text=?, source_tier=?, lang=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, lang, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit cins_accidents rows for status='parsed'. Returns rows built."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM cins_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE cins_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO cins_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "RS",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE cins_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
