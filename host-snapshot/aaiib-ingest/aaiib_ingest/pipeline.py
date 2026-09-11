# aaiib_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for AAIIB (Philippines).

  discover(): walks /reports/ → every /{YEAR}-accidents/ listing page, collects
  accident final-report PDF hrefs (registration mark + accident keyword, minus
  site-wide boilerplate), inserts new case_ids into aaiib_reports.  case_id is
  AAIIB-{YEAR}-{REG}.  Idempotent — existing case_ids are skipped.

  fetch(): for each status='new' row with a pdf_url: downloads the PDF and
  advances to 'fetched'.  Per-row try/except — a download failure keeps the row
  at 'new' for the next run (the web.caaplocal.ph mirror sometimes 0-bytes).

  parse(): extracts text via pdftotext.  scanned-aware: text length >=
  MIN_NARRATIVE (600) → source_tier='pdf'; non-empty but short → 'scanned'
  (skip build); empty → 'none'.  Also upgrades metadata (date / location /
  operator / aaiib_ref) from the structured PDF header when available.

  build(): emits aaiib_accidents rows.  Rows whose narrative_text is shorter
  than _NARRATIVE_FLOOR (80 chars), or whose source_tier is 'scanned'/'none',
  are skipped.
"""
import os
import sys
import time

from . import aaiib, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """Walk the AAIIB per-year listing pages and INSERT new case_ids.

    full: accepted for API parity; no extra effect (the whole index is always
          walked; per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    index_resp = client.get(aaiib.INDEX_URL)
    index_resp.raise_for_status()
    index_html = (
        index_resp.content.decode("utf-8", "replace")
        if isinstance(index_resp.content, bytes) else index_resp.content
    )

    year_urls = aaiib.iter_year_urls(index_html)

    inserted = 0
    for year_url in year_urls:
        ym = __import__("re").search(r"/(\d{4})-accidents", year_url)
        year = ym.group(1) if ym else ""
        time.sleep(aaiib.DELAY)
        try:
            year_resp = client.get(year_url)
            year_resp.raise_for_status()
            year_html = (
                year_resp.content.decode("utf-8", "replace")
                if isinstance(year_resp.content, bytes) else year_resp.content
            )
        except Exception as exc:
            print(f"[aaiib discover] {year_url}: {exc}", file=sys.stderr)
            continue

        rows = aaiib.parse_listing(year_html, year)
        for row in rows:
            case_id = row["case_id"]
            if conn.execute(
                "SELECT 1 FROM aaiib_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue  # already known

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaiib_reports "
                "(case_id, report_url, pdf_url, title, event_class, "
                "registration, date_of_occurrence, lang, status, "
                "discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    year_url,
                    row.get("pdf_url"),
                    row.get("title"),
                    row.get("event_class"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
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
    """Download the PDF for each status='new' row and advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM aaiib_reports WHERE status=?",
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
                time.sleep(aaiib.DELAY)
                aaiib.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaiib fetch] {case_id}: download {exc}", file=sys.stderr)
                # stay at 'new' for retry — do NOT advance
                continue

        try:
            conn.execute(
                "UPDATE aaiib_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aaiib fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text + metadata for each status='fetched' row.

    source_tier:
      'pdf'     — text length >= MIN_NARRATIVE (600 chars)
      'scanned' — text present but below threshold (scanned/low-text PDF)
      'none'    — no text at all (no PDF or empty extraction)

    Metadata (date_of_occurrence / location / operator / aaiib_ref) is upgraded
    from the structured PDF header when present and not already set.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, date_of_occurrence, location, operator, aircraft "
        "FROM aaiib_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            tier = "pdf"
        elif full_text:
            narrative = full_text
            tier = "scanned"
        else:
            narrative = ""
            tier = "none"

        meta = aaiib.extract_pdf_metadata(full_text)
        date_iso = row["date_of_occurrence"] or meta.get("date_iso")
        location = row["location"] or meta.get("location")
        operator = row["operator"] or meta.get("operator")
        aircraft = row["aircraft"] or meta.get("aircraft")
        aaiib_ref = meta.get("aaiib_ref")

        conn.execute(
            "UPDATE aaiib_reports "
            "SET narrative_text=?, source_tier=?, date_of_occurrence=?, "
            "location=?, operator=?, aircraft=?, aaiib_ref=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, date_iso, location, operator, aircraft, aaiib_ref,
             db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit an aaiib_accidents record per status='parsed' row, or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars
      • source_tier in ('scanned', 'none')

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM aaiib_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR or row["source_tier"] in ("scanned", "none"):
            conn.execute(
                "UPDATE aaiib_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO aaiib_accidents "
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
                "PH",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiib_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
