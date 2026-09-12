# jiaacve_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for JIAAC Venezuela.

discover(): fetches the single listing page (https://www.mppt.gob.ve/jiaac/informes/),
  parses the Elementor accordion (year sections 2005-2026), extracts all DLM download
  links, resolves superseded pairs (Final beats Preliminar for same expediente number),
  and INSERTs new dl_ids into jiaacve_reports.  Idempotent — existing dl_ids skipped.

fetch(): for each status='new' row, downloads the PDF with a 1.5s politeness delay.
  Per-row try/except: a download failure keeps the row at 'new' for retry.

parse(): extracts text via pdftotext, parses PDF metadata (case_id, date, aircraft,
  operator, location, probable_cause, phase, fatalities).  source_tier:
    'pdf'     — text layer usable (>= SCANNED_THRESHOLD chars)
    'scanned' — text present but too short (image-only PDF)
    'none'    — extraction failed / no PDF

build(): emits jiaacve_accidents rows.  Rows with source_tier 'scanned'/'none' or
  narrative shorter than _NARRATIVE_FLOOR are skipped.  Superseded rows are also
  skipped (they have superseded_by set).  Rows marked superseded but with no primary
  built yet are still parsed (so the primary can reference them) but skipped in build.
"""
import os
import sys
import time
import re

from . import jiaacve, db
from .pdf import extract_text, SCANNED_THRESHOLD

_NARRATIVE_FLOOR = 300  # minimum chars to count as live


def discover(conn, client, full=False):
    """Fetch listing page and INSERT new reports.

    Returns: number of rows inserted.
    """
    time.sleep(0.5)
    try:
        resp = client.get(jiaacve.LISTING_URL)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        print(f"[jiaacve discover] listing fetch failed: {exc}", file=sys.stderr)
        return 0

    rows = jiaacve.parse_listing(html)

    if not rows:

        # 459 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[jiaacve discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    rows = jiaacve.resolve_superseded(rows)

    inserted = 0
    ts = db.now_ms()

    for row in rows:
        dl_id = row["dl_id"]
        if conn.execute(
            "SELECT 1 FROM jiaacve_reports WHERE dl_id=?", (dl_id,)
        ).fetchone():
            continue  # already known

        # Store superseded_by as dl_id reference (resolved at build time to case_id)
        superseded_by = row.get("superseded_by")

        conn.execute(
            "INSERT INTO jiaacve_reports "
            "(case_id, dl_id, pdf_url, listing_title, listing_year, "
            "report_type_raw, superseded_by, registration, "
            "status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                dl_id,
                row["pdf_url"],
                row["listing_title"],
                row["listing_year"],
                row["report_type_raw"],
                superseded_by,
                row.get("registration"),
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1

    conn.commit()
    print(f"[jiaacve discover] scraped {len(rows)} links, inserted {inserted} new")
    return inserted


def fetch(conn, client, pdf_dir, limit=None):
    """Download PDFs for status='new' rows.

    limit: optional int to cap downloads per run (useful for smoke-test).
    Returns: number of rows attempted.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    query = "SELECT dl_id, pdf_url FROM jiaacve_reports WHERE status=?"
    rows = conn.execute(query, (db.STATUS_NEW,)).fetchall()

    if limit is not None:
        rows = rows[:limit]

    attempted = 0
    for row in rows:
        dl_id = row["dl_id"]
        pdf_url = row["pdf_url"]

        safe_name = f"jiaacve_{dl_id}.pdf"
        dest = os.path.join(pdf_dir, safe_name)

        time.sleep(jiaacve.DELAY)
        try:
            jiaacve.download(client, pdf_url, dest)
        except Exception as exc:
            print(f"[jiaacve fetch] {dl_id}: download failed: {exc}", file=sys.stderr)
            attempted += 1
            continue

        try:
            conn.execute(
                "UPDATE jiaacve_reports SET pdf_path=?, status=?, updated_at=? WHERE dl_id=?",
                (dest, db.STATUS_FETCHED, db.now_ms(), dl_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[jiaacve fetch] {dl_id}: db update failed: {exc}", file=sys.stderr)

        attempted += 1

    return attempted


def parse(conn):
    """Extract text and metadata from fetched PDFs.

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT dl_id, pdf_path, listing_year, case_id AS listing_case_id "
        "FROM jiaacve_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        dl_id = row["dl_id"]
        pdf_path = row["pdf_path"]
        listing_year = row["listing_year"]
        listing_case_id = row["listing_case_id"]

        raw_text = extract_text(pdf_path) if pdf_path else ""

        if len(raw_text) >= SCANNED_THRESHOLD:
            tier = "pdf"
        elif raw_text:
            tier = "scanned"
        else:
            tier = "none"

        # Extract structured metadata from PDF text
        pdf_case_id = jiaacve.parse_case_id_from_pdf(raw_text) if raw_text else None
        # Prefer listing-derived case_id (exact from title) unless it's a fallback dlm- id.
        # PDF case_id is used only when listing parsing failed (dlm- prefix) because
        # some PDFs contain watermark text that contaminates the first regex match.
        if listing_case_id and not listing_case_id.startswith('dlm-'):
            canonical_case_id = listing_case_id
        else:
            canonical_case_id = pdf_case_id or listing_case_id

        event_date = jiaacve.parse_event_date(raw_text) if raw_text else None
        registration = jiaacve.parse_registration(raw_text) if raw_text else None
        aircraft = jiaacve.parse_aircraft(raw_text) if raw_text else None
        operator = jiaacve.parse_operator(raw_text) if raw_text else None
        location = jiaacve.parse_location(raw_text) if raw_text else None
        probable_cause = jiaacve.parse_probable_cause(raw_text) if raw_text else None
        narrative = jiaacve.extract_narrative(raw_text) if raw_text else ""
        fatalities = jiaacve.parse_fatalities(raw_text) if raw_text else None
        phase = jiaacve.parse_phase(raw_text) if raw_text else None

        conn.execute(
            "UPDATE jiaacve_reports SET "
            "case_id=?, event_date=?, aircraft=?, registration=?, operator=?, "
            "location=?, narrative_text=?, probable_cause=?, "
            "source_tier=?, fatalities_total=?, phase=?, status=?, updated_at=? "
            "WHERE dl_id=?",
            (
                canonical_case_id,
                event_date,
                aircraft,
                registration,
                operator,
                location,
                narrative if tier == "pdf" else "",
                probable_cause,
                tier,
                fatalities,
                phase,
                db.STATUS_PARSED,
                db.now_ms(),
                dl_id,
            ),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit jiaacve_accidents rows from parsed reports.

    Skip criteria:
      - source_tier 'scanned' or 'none'
      - narrative shorter than _NARRATIVE_FLOOR
      - superseded_by is set (this row has a better version)

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT dl_id, case_id, event_date, aircraft, registration, operator, "
        "location, narrative_text, probable_cause, pdf_url, "
        "report_type_raw, source_tier, superseded_by, "
        "fatalities_total, phase "
        "FROM jiaacve_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        superseded_by = row["superseded_by"]

        # Skip superseded (a better report exists for same expediente)
        if superseded_by:
            conn.execute(
                "UPDATE jiaacve_reports SET status=?, updated_at=? WHERE dl_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["dl_id"]),
            )
            conn.commit()
            continue

        if tier in ("scanned", "none") or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE jiaacve_reports SET status=?, updated_at=? WHERE dl_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["dl_id"]),
            )
            conn.commit()
            continue

        case_id = row["case_id"]
        site_slug = _make_site_slug(case_id)
        source_url = row["pdf_url"]

        # Determine canonical report_type
        rtype_raw = (row["report_type_raw"] or "").strip()
        report_type = rtype_raw if rtype_raw else "Informe"

        conn.execute(
            "INSERT OR REPLACE INTO jiaacve_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, "
            "lang, built_at, fatalities_total, phase, category) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row["event_date"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "VE",
                narrative,
                row["probable_cause"],
                source_url,
                report_type,
                site_slug,
                "es",
                db.now_ms(),
                row["fatalities_total"],
                row["phase"],
                None,   # category
            ),
        )
        conn.execute(
            "UPDATE jiaacve_reports SET status=?, updated_at=? WHERE dl_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["dl_id"]),
        )
        conn.commit()
        built += 1

    return built


def _make_site_slug(case_id: str) -> str:
    """Stable URL slug from case_id.

    '007/2024' → 'jiaacve-007-2024'
    'dlm-189736' → 'jiaacve-dlm-189736'
    """
    slug = re.sub(r'[^a-z0-9]+', '-', case_id.lower()).strip('-')
    return f"jiaacve-{slug}"
