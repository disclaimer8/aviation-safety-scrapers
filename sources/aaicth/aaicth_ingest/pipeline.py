# aaicth_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for Thailand AAIC.

Listing pages (3):
  id=7  → final accident reports
  id=75 → interim reports (some superseded when final also exists on id=7)
  id=76 → serious incident reports

case_id scheme:
  Final reports:          aaicth-{seq}/{year}
  Interim reports:        aaicth-{seq}/{year}-interim
  Serious incident:       aaicth-{seq}/{year}-serious_incident

Superseded_by logic (mirrors aaiahk-ingest):
  discover() walks id=7 first (final), then id=75 (interim), then id=76 (SI).
  When the same File No appears on id=7 AND id=75, the interim row gets
  superseded_by = <final case_id>.
  build() skips rows with superseded_by set and deletes any already-built
  accident rows for them.

Date refinement:
  parse() tries to extract an exact date from the PDF text (EN date pattern
  or Thai BE date) and updates event_date from YYYY-01-01 to YYYY-MM-DD.
"""
import os
import sys
import time

from . import aaicth, db, text
from .pdf import extract_with_ocr_fallback

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars


def discover(conn, client, full=False):
    """Walk all three listing pages and INSERT new rows.

    Walk order: final first (id=7), then interim (id=75), then SI (id=76).
    Superseded_by is set when the same File No appears in a final AND interim/SI page.

    Returns: number of rows inserted.
    """
    inserted = 0
    # Track final case_ids by (seq_no, year) to mark superseded interim/SI rows
    final_by_key: dict[tuple, str] = {}

    for report_type, url in aaicth.LISTING_URLS.items():
        time.sleep(aaicth.DELAY)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.content
        except Exception as exc:
            print(f"[aaicth discover] {url}: {exc}", file=sys.stderr)
            continue

        rows = aaicth.parse_listing(html, report_type)
        for row in rows:
            case_id = row["case_id"]
            key = (row["seq_no"], row["year"])

            # Determine superseded_by for interim/SI rows
            superseded_by = None
            if report_type in ("interim", "serious_incident") and key in final_by_key:
                superseded_by = final_by_key[key]

            # Update existing row's superseded_by if needed
            if superseded_by:
                conn.execute(
                    "UPDATE aaicth_reports SET superseded_by=?, updated_at=? "
                    "WHERE case_id=? AND (superseded_by IS NULL OR superseded_by != ?)",
                    (superseded_by, db.now_ms(), case_id, superseded_by),
                )
                conn.commit()

            if report_type == "final":
                final_by_key[key] = case_id

            if conn.execute(
                "SELECT 1 FROM aaicth_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue  # already known

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO aaicth_reports "
                "(case_id, seq_no, year, report_type, report_url, "
                "pdf_url, pdf_url_th, pdf_url_en, "
                "title, aircraft, registration, event_date, lang, "
                "superseded_by, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row["seq_no"],
                    row["year"],
                    row["report_type"],
                    row.get("report_url"),
                    row.get("pdf_url"),
                    row.get("pdf_url_th"),
                    row.get("pdf_url_en"),
                    row.get("title"),
                    row.get("aircraft"),
                    row.get("registration"),
                    row.get("event_date"),
                    row.get("lang"),
                    superseded_by,
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows → advance to 'fetched'.

    Rows with no pdf_url advance with pdf_path=None.
    Per-row try/except: download failure keeps row at 'new' for retry.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url, lang FROM aaicth_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe_id = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe_id + ".pdf")
            try:
                time.sleep(aaicth.DELAY)
                aaicth.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aaicth fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        conn.execute(
            "UPDATE aaicth_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
            (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()

    return len(rows)


def parse(conn):
    """Extract + clean PDF text for status='fetched' rows.

    Also attempts to refine event_date from YYYY-01-01 to YYYY-MM-DD
    by parsing the PDF text for an exact date (EN or Thai BE format).

    source_tier:
      'pdf'     — native text layer, >= SCANNED_THRESHOLD chars
      'ocr'     — OCR path used
      'scanned' — text < threshold even after OCR attempt
      'none'    — no PDF / empty

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, lang, event_date "
        "FROM aaicth_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        lang = row["lang"] or "en"
        narrative, tier = extract_with_ocr_fallback(pdf_path, lang=lang)

        # Attempt to refine date from PDF text
        event_date = row["event_date"]
        if narrative and event_date and event_date.endswith("-01-01"):
            refined = aaicth.parse_thai_date(narrative)
            if refined:
                event_date = refined

        conn.execute(
            "UPDATE aaicth_reports "
            "SET narrative_text=?, source_tier=?, event_date=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, event_date, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit aaicth_accidents records from status='parsed' rows.

    Skip criteria (→ 'skipped'):
      * superseded_by is set (a final report exists for this case)
      * source_tier is 'none' and narrative is empty
      * narrative shorter than _NARRATIVE_FLOOR chars

    After building, delete aaicth_accidents rows for any superseded case_ids.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, year, aircraft, registration, "
        "event_date, narrative_text, source_tier, pdf_url, report_url, "
        "report_type, lang, superseded_by "
        "FROM aaicth_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        superseded_by = row["superseded_by"]

        # Skip superseded rows
        if superseded_by:
            conn.execute(
                "UPDATE aaicth_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # Skip rows with no usable narrative
        if tier == "none" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaicth_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])

        conn.execute(
            "INSERT OR REPLACE INTO aaicth_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "lang, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["event_date"],
                row["aircraft"],
                row["registration"],
                None,   # operator — not in listing; extracted by Phase 3
                None,   # location — not in listing; extracted by Phase 3
                "TH",
                narrative,
                None,
                source_url,
                row["report_type"],
                row["lang"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaicth_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    # Delete stale superseded accident rows
    superseded_rows = conn.execute(
        "SELECT case_id FROM aaicth_reports WHERE superseded_by IS NOT NULL"
    ).fetchall()
    for sr in superseded_rows:
        conn.execute(
            "DELETE FROM aaicth_accidents WHERE case_id=?", (sr["case_id"],)
        )
    if superseded_rows:
        conn.commit()

    return built
