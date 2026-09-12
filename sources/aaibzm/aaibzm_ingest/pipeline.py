# aaibzm_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for AAIB Zambia (single index page).

discover(): GET the one https://aaib.org.zm/ index page, parse the list-group
  cards and INSERT new case_ids (intrinsic 'aaibzm-<reg-slug>') into
  aaibzm_reports.  The CARD is the authoritative source for aircraft, event
  date, location, registration and event class (the PDF body has no labelled
  cover block), so those are stored at discover time.  pdf_url is the derived
  'reports/<REG>.pdf', report_url the 'pages/<REG>.php' detail page.  lang is
  always 'en'.  Idempotent.

fetch(): for each status='new' row, download the PDF and advance to 'fetched'.
  When the derived reports/<REG>.pdf is unavailable the authoritative link is
  resolved from the detail page (covers 9J-RHE).  Per-row try/except: a
  download failure keeps the row at 'new' for the next run.

parse(): extract text via pdftotext.  Scanned-aware:
    text length <  _SCANNED_FLOOR (500) -> tier 'scanned'  (image-only PDF)
    text length >= MIN_NARRATIVE (600)  -> tier 'pdf'
    otherwise                           -> tier 'short'
  Card fields win; the PDF only back-fills fields the card left empty.

build(): emit aaibzm_accidents rows (country 'ZM').  Rows whose narrative is
  shorter than _NARRATIVE_FLOOR (80) -- which includes every 'scanned' row --
  are skipped.  report_type carries the event_class.
"""
import os
import sys
import time

from . import aaibzm, db, text
from .pdf import extract_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300   # chars; rows with less are non-report events
_SCANNED_FLOOR = 500    # chars; below this an image-only (scanned) PDF is assumed


def discover(conn, client, full=False):
    """
    GET the AAIB Zambia index page and INSERT new case_ids into aaibzm_reports.

    full: accepted for API parity (the whole single page is always walked).
    lang is always 'en'.  Returns: number of rows inserted.
    """
    resp = client.get(aaibzm.INDEX_URL)
    resp.raise_for_status()
    index_html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.text

    rows = aaibzm.parse_listing(index_html)


    if not rows:

        # 7 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[aaibzm discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    inserted = 0
    for row in rows:
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM aaibzm_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaibzm_reports "
            "(case_id, report_url, pdf_url, title, event_class, aircraft, "
            "registration, date_of_occurrence, location, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("title"),
                row.get("event_class") or "Accident",
                row.get("aircraft"),
                row.get("registration"),
                row.get("event_date"),
                row.get("location"),
                "en",
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
        "SELECT case_id, pdf_url, report_url FROM aaibzm_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        detail_url = row["report_url"]

        pdf_path = None
        used_url = pdf_url
        if pdf_url:
            dest = os.path.join(pdf_dir, case_id + ".pdf")
            try:
                time.sleep(aaibzm.DELAY)
                used_url = aaibzm.download(client, pdf_url, dest, detail_url=detail_url) or pdf_url
                pdf_path = dest
            except Exception as exc:
                print(f"[aaibzm fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE aaibzm_reports SET pdf_path=?, pdf_url=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, used_url, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaibzm fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text and classify.

    source_tier:
      'scanned' -- text below _SCANNED_FLOOR (image-only PDF)
      'pdf'     -- text length >= MIN_NARRATIVE
      'short'   -- some text, below MIN_NARRATIVE but >= _SCANNED_FLOOR
      'none'    -- no PDF / empty extraction

    Card fields are authoritative; the PDF only back-fills the registration and
    cover-block fields (event date, aircraft, location, operator) the card left
    empty (idempotent).  Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, aircraft, location, operator, "
        "date_of_occurrence FROM aaibzm_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier = "", "none"
        elif len(full_text) < _SCANNED_FLOOR:
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        # Card fields win; PDF only back-fills what is missing (idempotent).
        registration = row["registration"] or aaibzm.find_registration(full_text)
        event_date = row["date_of_occurrence"] or aaibzm.extract_event_date(full_text)
        aircraft = row["aircraft"] or aaibzm.extract_aircraft(full_text)
        location = row["location"] or aaibzm.extract_location(full_text)
        operator = row["operator"] or aaibzm.extract_operator(full_text)

        conn.execute(
            "UPDATE aaibzm_reports "
            "SET narrative_text=?, source_tier=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, registration,
             event_date, aircraft, location, operator,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit an aaibzm_accidents record or skip.

    Skip (status -> 'skipped') when narrative_text < _NARRATIVE_FLOOR chars
    (this covers every 'scanned' / 'none' row).

    report_type carries the event_class.  Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM aaibzm_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaibzm_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])
        report_type = row["event_class"]

        conn.execute(
            "INSERT OR REPLACE INTO aaibzm_accidents "
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
                "ZM",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaibzm_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
