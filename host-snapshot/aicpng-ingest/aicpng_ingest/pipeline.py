# aicpng_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for AIC PNG.

discover(): walks listing pages 0..N (stop-on-empty), inserting new case_ids
  into aicpng_reports with the rich listing metadata (class/date/registration/
  location/status).  report_url is the /investigation/{nid} detail page.
  Idempotent — existing case_ids are skipped.  English source → lang='en'.

fetch(): for each status='new' row: GET the detail page, pick the preferred
  report PDF (Final → Interim → Preliminary → …), download it, store pdf_path +
  pdf_url + event_class(report_type), advance to 'fetched'.  Rows with no PDF on
  the detail page advance to 'fetched' with pdf_path=None (build skips them).
  Per-row try/except: a failure keeps the row at 'new' for the next run.

parse(): extract text via pdftotext.  text >= MIN_NARRATIVE (600) →
  source_tier='pdf'.  Short text → 'scanned' (AIC final reports carry a text
  layer; a near-empty extraction means a scanned/image PDF) and the row is
  treated as unusable.  No text at all → 'none'.

build(): emits aicpng_accidents rows; rows whose narrative_text is shorter than
  _NARRATIVE_FLOOR (80) are skipped.  country='PG'.
"""
import os
import sys
import time

from . import aicpng, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80   # chars; below this a row is not a usable report
_SCANNED_CEIL = 500     # chars; PDF text below this == scanned/image-only
_MAX_EMPTY_PAGES = 1    # stop discovery after this many consecutive empty pages
_PAGE_CAP = 50          # hard safety ceiling on pages walked


def discover(conn, client, full=False):
    """Walk listing pages and INSERT new case_ids.  Returns rows inserted."""
    inserted = 0
    empty_streak = 0
    page = 0
    while page < _PAGE_CAP:
        try:
            html = aicpng.fetch_listing_page(client, page)
        except Exception as exc:
            print(f"[aicpng discover] page={page}: {exc}", file=sys.stderr)
            break

        rows = aicpng.parse_listing(html)
        if not rows:
            empty_streak += 1
            if empty_streak >= _MAX_EMPTY_PAGES:
                break
            page += 1
            time.sleep(aicpng.DELAY)
            continue
        empty_streak = 0

        for row in rows:
            case_id = row["case_id"]
            if conn.execute(
                "SELECT 1 FROM aicpng_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aicpng_reports "
                "(case_id, report_url, title, event_class, registration, "
                "date_of_occurrence, location, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row.get("report_url"),
                    row.get("title"),
                    row.get("event_class"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
                    row.get("location"),
                    "en",
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
        page += 1
        time.sleep(aicpng.DELAY)
    return inserted


def refetch(conn, cap=25):
    """Re-queue PDF-less 'skipped' rows to 'new' so fetch() re-visits their
    detail page.

    AIC publishes an occurrence page first and attaches the report PDF later;
    without this stage such stubs freeze at terminal 'skipped' forever and the
    late-published report is lost (same stub-freeze class as BEA/CIAIAC,
    aviation-safety-scrapers #25/#26; found here 2026-07-26 with 3 frozen
    stubs). Volume is tiny, so no cooldown — every cycle re-checks each stub
    (one detail-page request apiece), `cap` bounds the worst case.

    Returns: number of rows re-queued.
    """
    rows = conn.execute(
        "SELECT case_id FROM aicpng_reports WHERE status=? "
        "AND (pdf_url IS NULL OR pdf_url='') AND (pdf_path IS NULL OR pdf_path='') "
        "AND report_url IS NOT NULL LIMIT ?",
        (db.STATUS_SKIPPED, cap),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE aicpng_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_NEW, db.now_ms(), row["case_id"]),
        )
    conn.commit()
    return len(rows)


def fetch(conn, client, pdf_dir):
    """Visit each new row's detail page, download the preferred PDF, advance to
    'fetched'.  Returns number of rows iterated."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, report_url, event_class FROM aicpng_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        report_url = row["report_url"]

        pdf_path = None
        pdf_url = None

        if report_url:
            try:
                time.sleep(aicpng.DELAY)
                detail_html = aicpng.fetch_detail(client, report_url)
                _rep_type, pdf_url = aicpng.best_pdf(detail_html)
                if pdf_url:
                    safe = case_id.replace("/", "_").replace(" ", "_")
                    dest = os.path.join(pdf_dir, safe + ".pdf")
                    time.sleep(aicpng.DELAY)
                    aicpng.download(client, pdf_url, dest)
                    pdf_path = dest
            except Exception as exc:
                print(f"[aicpng fetch] {case_id}: {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE aicpng_reports SET pdf_path=?, pdf_url=?, "
                "status=?, updated_at=? WHERE case_id=?",
                (pdf_path, pdf_url, db.STATUS_FETCHED,
                 db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aicpng fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract PDF text for each fetched row.  Returns rows processed."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aicpng_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            tier = "pdf"
        elif 0 < len(full_text) < _SCANNED_CEIL:
            # text layer present but tiny → scanned/image-only PDF
            narrative = full_text
            tier = "scanned"
        elif full_text:
            narrative = full_text
            tier = "short"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE aicpng_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit aicpng_accidents rows.  Returns number built (not skipped)."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aicpng_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR or row["source_tier"] == "scanned":
            conn.execute(
                "UPDATE aicpng_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = aicpng.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO aicpng_accidents "
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
                "PG",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aicpng_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
