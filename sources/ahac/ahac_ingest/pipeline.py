# ahac_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for Honduras AHAC.

discover(): GET the static archive page, parse all DOCUMENTO/ PDF links,
  INSERT new case_ids into ahac_reports. Idempotent -- existing case_ids skipped.

fetch(): for each status='new' row, download the PDF (with Referer), advance
  to 'fetched'. Per-row try/except: a download failure keeps row at 'new'.

parse(): extract text via pdftotext.
  'pdf'     -- text >= MIN_NARRATIVE (600 chars)
  'short'   -- text between SCANNED_MAX and MIN_NARRATIVE
  'scanned' -- text below SCANNED_MAX (image-only / scanned)
  'none'    -- no PDF / empty extraction

build(): emit ahac_accidents rows. Rows with narrative_text < _NARRATIVE_FLOOR
  are skipped. country='HN'.
"""
import os
import sys
import time

from . import ahac, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_MAX

_NARRATIVE_FLOOR = 80  # chars; rows with less text are non-usable


def discover(conn, client, full=False):
    """Walk the AHAC listing page and INSERT new case_ids.

    full: accepted for API parity (ignored; the whole listing is always parsed).
    Returns: number of rows inserted.
    """
    resp = client.get(ahac.INDEX_URL)
    resp.raise_for_status()
    html_content = (
        resp.content.decode("utf-8", "replace")
        if isinstance(resp.content, bytes)
        else resp.content
    )

    rows = ahac.parse_listing(html_content)
    inserted = 0

    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM ahac_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ahac_reports "
            "(case_id, pdf_url, title, event_class, aircraft, registration, "
            "date_of_occurrence, location, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row["pdf_url"],
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
                "es",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download the PDF for each status='new' row and advance to 'fetched'.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ahac_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        safe_case_id = case_id.replace("/", "_").replace(" ", "_")
        dest = os.path.join(pdf_dir, safe_case_id + ".pdf")

        try:
            time.sleep(ahac.DELAY)
            ahac.download(client, pdf_url, dest)
            pdf_path = dest
        except Exception as exc:
            print(f"[ahac fetch] {case_id}: download failed: {exc}", file=sys.stderr)
            continue

        try:
            conn.execute(
                "UPDATE ahac_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ahac fetch] {case_id}: db error: {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text from each status='fetched' row's PDF.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ahac_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= SCANNED_MAX:
            narrative, tier = full_text, "short"
        elif full_text:
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        conn.execute(
            "UPDATE ahac_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit an ahac_accidents record for each buildable status='parsed' row.

    Skip: narrative_text shorter than _NARRATIVE_FLOOR chars (covers 'scanned'/'none').
    country='HN'.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, location, "
        "date_of_occurrence, narrative_text, pdf_url "
        "FROM ahac_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ahac_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO ahac_accidents "
            "(case_id, event_date, aircraft, registration, location, country, "
            "narrative_text, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["location"],
                "HN",
                narrative,
                row["pdf_url"],
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ahac_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
