# ainhr_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for AIN.HR (Croatia) aviation reports.

  discover(): fetches the aviation-investigations category page, lists every
  /istrage/<slug>/ post, then GETs each post page to resolve its PDF link(s)
  and metadata, inserting new case_ids into ainhr_reports. case_id == the
  intrinsic, order-independent post slug. HR-preference: pdf_url is the HR
  narrative PDF (final > preliminary > other) when present, else the EN PDF.
  Idempotent — known case_ids are skipped (their post page is not re-fetched).

  fetch(): for each status='new' row with a pdf_url, downloads the PDF and
  advances to 'fetched'. Rows without a pdf_url (open investigations with no
  report yet) advance to 'fetched' with pdf_path=None. Per-row try/except: a
  download failure keeps the row at 'new' for the next run.

  parse(): extracts text via pdftotext. Tiers:
    'pdf'     — text length >= MIN_NARRATIVE (600 chars)
    'scanned' — there is some text but below SCANNED_FLOOR (~image-only scan)
    'short'   — text between SCANNED_FLOOR and MIN_NARRATIVE
    'none'    — no text at all (no PDF / empty extraction)
  Registration is extracted from the report text header at this point.

  build(): emits ainhr_accidents rows. Skips rows whose tier is 'scanned' or
  'none', or whose narrative is shorter than _NARRATIVE_FLOOR (80 chars).
  narrative_text stays Croatian (HR→EN happens downstream in P3).
"""
import os
import sys
import time

from . import ainhr, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """Walk the AIN.HR aviation category page and INSERT new case_ids.

    full: accepted for API parity; the whole category is always walked and
          per-case_id skip handles idempotency.

    Returns: number of rows inserted.
    """
    index_resp = client.get(ainhr.INDEX_URL)
    index_resp.raise_for_status()
    index_html = (
        index_resp.content.decode("utf-8", "replace")
        if isinstance(index_resp.content, bytes) else index_resp.content
    )

    slugs = ainhr.iter_post_slugs(index_html)

    inserted = 0
    for slug in slugs:
        case_id = ainhr.make_case_id(slug)
        if conn.execute(
            "SELECT 1 FROM ainhr_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue  # already known — do not re-fetch the post page

        time.sleep(ainhr.DELAY)
        try:
            post_resp = client.get(ainhr.post_url(slug))
            post_resp.raise_for_status()
            post_html = (
                post_resp.content.decode("utf-8", "replace")
                if isinstance(post_resp.content, bytes) else post_resp.content
            )
        except Exception as exc:
            print(f"[ainhr discover] {slug}: {exc}", file=sys.stderr)
            continue

        row = ainhr.parse_post(post_html, slug)
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ainhr_reports "
            "(case_id, report_url, pdf_url, pdf_url_hr, pdf_url_en, "
            "title, event_class, aircraft, registration, date_of_occurrence, "
            "location, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("pdf_url_hr"),
                row.get("pdf_url_en"),
                row.get("title"),
                row.get("event_class"),
                None,                       # aircraft — filled at build (from title)
                None,                       # registration — filled at parse (from PDF)
                row.get("date_of_occurrence"),
                None,                       # location — not reliably structured
                row.get("lang"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows and advance to 'fetched'."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ainhr_reports WHERE status=?",
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
                time.sleep(ainhr.DELAY)
                ainhr.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[ainhr fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE ainhr_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ainhr fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text for status='fetched' rows; set tier + registration."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ainhr_reports WHERE status=?",
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
            # has some text but below the scanned floor → image-only scan
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        registration = ainhr.registration_from_text(narrative)

        conn.execute(
            "UPDATE ainhr_reports "
            "SET narrative_text=?, source_tier=?, registration=COALESCE(?, registration), "
            "status=?, updated_at=? WHERE case_id=?",
            (narrative, tier, registration, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit ainhr_accidents rows from status='parsed' rows, or skip.

    Skip (status → 'skipped') when:
      • source_tier is 'scanned' or 'none', or
      • narrative_text shorter than _NARRATIVE_FLOOR chars.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url, title "
        "FROM ainhr_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        if tier in ("scanned", "none") or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ainhr_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO ainhr_accidents "
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
                "HR",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ainhr_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
