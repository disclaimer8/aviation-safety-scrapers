# mzisi_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for MzI (Slovenia) aviation reports.

discover(): fetches the single server-rendered index page, keeps only FINAL
  reports + summaries, and inserts new staging rows (case_id = 'mzisi-YYYY-slug')
  into mzisi_reports.  Idempotent — existing case_ids are skipped.

fetch(): for each status='new' row downloads the PDF and advances to 'fetched'.
  Per-row try/except: a download failure keeps the row at 'new' for retry.

parse(): extracts text via pdftotext and tags source_tier:
    'pdf'     — text length >= MIN_NARRATIVE (600 chars)
    'short'   — some text but below MIN_NARRATIVE and >= SCANNED_FLOOR
    'scanned' — text below SCANNED_FLOOR (~500 chars): image-only / OCR-needed
    'none'    — no text at all (no PDF / empty extraction)
  Also extracts the official document number into source_event_id (raw, with
  slash) and detects language ('sl' default / 'en').

build(): emits mzisi_accidents rows.  Rows whose narrative_text is shorter than
  _NARRATIVE_FLOOR (80 chars) are skipped (covers 'scanned'/'none').
  source_event_id (raw case number) is preferred as case_id when present, else
  the staging case_id is used.
"""
import os
import sys
import time

from . import mzisi, db, text
from .pdf import extract_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300   # chars; rows with less are non-report events
SCANNED_FLOOR = 500     # chars; below this an extraction is treated as scanned


def discover(conn, client, full=False):
    """
    Fetch the MzI index page and INSERT new FINAL-report case_ids.

    full: accepted for API parity; has no extra effect (the single index page
          is always walked; per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    resp = client.get(mzisi.INDEX_URL)
    resp.raise_for_status()
    html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content

    rows = mzisi.parse_index(html)


    if not rows:

        # 66 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[mzisi discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM mzisi_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO mzisi_reports "
            "(case_id, pdf_url, title, event_class, registration, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("pdf_url"),
                row.get("title"),
                row.get("report_type"),
                row.get("registration"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """
    For each status='new' row: download the PDF and advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM mzisi_reports WHERE status=?",
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
                time.sleep(mzisi.DELAY)
                mzisi.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[mzisi fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE mzisi_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[mzisi fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text, tag tier, pull source_event_id,
    detect language.  Returns number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM mzisi_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        n = len(full_text)
        if n >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif n >= SCANNED_FLOOR:
            narrative, tier = full_text, "short"
        elif n > 0:
            # below the scanned floor — image-only / OCR-needed
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        source_event_id = mzisi.extract_case_no(full_text)
        lang = mzisi.detect_lang(full_text)

        # C1: the gov.si index is a flat PDF-link list with NO structured row
        # fields, so event_date / location / aircraft / operator come from the
        # PDF TEXT here and are persisted on the staging row → mzisi_accidents.
        event_date = mzisi.extract_event_date(full_text, row["case_id"])
        location = mzisi.extract_location(full_text)
        aircraft = mzisi.extract_aircraft(full_text)
        operator = mzisi.extract_operator(full_text)

        conn.execute(
            "UPDATE mzisi_reports "
            "SET narrative_text=?, source_tier=?, source_event_id=?, lang=?, "
            "date_of_occurrence=?, location=?, aircraft=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, source_event_id, lang,
             event_date, location, aircraft, operator,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a mzisi_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars (covers scanned/none).

    case_id of the emitted accident row = the FROZEN accident_case_id: on first
    build it is computed as source_event_id (raw official number, slash
    preserved) when present else the staging case_id, then persisted to
    mzisi_reports.accident_case_id and ALWAYS reused thereafter.

    I2 INVARIANT — the accident key is IMMUTABLE once assigned.  A later re-parse
    that newly extracts a 3720X official number must NOT change the key of an
    already-built row (that would INSERT OR REPLACE under a new case_id and
    orphan the original mzisi_accidents row / its prod article-join).  Hence we
    never recompute acc_case_id when accident_case_id is already set.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, source_event_id, event_class, aircraft, registration, "
        "operator, location, date_of_occurrence, narrative_text, pdf_url, "
        "report_url, accident_case_id FROM mzisi_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    miss = 0   # I3: built rows with NO official 3720X number (staging-slug key)
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE mzisi_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # I2: reuse the frozen key if present; only compute (and persist) it once.
        acc_case_id = row["accident_case_id"]
        if not acc_case_id:
            acc_case_id = row["source_event_id"] or row["case_id"]
            conn.execute(
                "UPDATE mzisi_reports SET accident_case_id=? WHERE case_id=?",
                (acc_case_id, row["case_id"]),
            )
        if not row["source_event_id"]:
            miss += 1
        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        # Parsed from the narrative, which already holds the full extracted PDF
        # text. mzisi_reports has no probable_cause column and adding one
        # would mean migrating a live database for no gain.
        #
        # Measured against all 69 PDFs on the host: 35 yield a cause, 28 of
        # them long enough to count toward the score that decides
        # indexability.
        probable_cause = mzisi.parse_probable_cause(narrative)

        conn.execute(
            "INSERT OR REPLACE INTO mzisi_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                acc_case_id,
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "SI",
                narrative,
                probable_cause,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE mzisi_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    # I3: report how many built rows had no extractable 3720X official number
    # (these fall back to the staging-slug accident key).
    if built:
        print(
            f"[mzisi build] {built} built; {miss} with no 3720X number "
            f"(staging-slug key)",
            file=sys.stderr,
        )
    return built
