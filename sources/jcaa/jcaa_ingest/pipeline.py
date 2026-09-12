# jcaa_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for JCAA Jamaica.

discover(): hits the JCAA accident-investigations page and the WP REST API
  media endpoint to enumerate accident/final-report PDFs.  Inserts new rows
  (intrinsic case_id from filename) into jcaa_reports.  Idempotent.

fetch(): downloads each status='new' row's PDF; advances to 'fetched'.
  Per-row try/except: download failures stay at 'new'.

parse(): extracts text via pdftotext; classifies source_tier; harvests
  cover-block fields (event_date, aircraft, location, operator, registration).

build(): emits jcaa_accidents rows.  Skip criteria: narrative_text shorter
  than _NARRATIVE_FLOOR (300) chars — this is the floor given in the spec.
  country='JM'.
"""
import os
import sys
import time

from . import jcaa, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

_NARRATIVE_FLOOR = 300   # spec says floor 300


def discover(conn, client, full=False):
    """Enumerate accident PDFs and insert new rows into jcaa_reports.

    Returns: number of rows inserted.
    """
    pdf_list = jcaa.discover_pdf_urls(client)
    if not pdf_list:
        # 5 rows from this source are already in production, so an
        # empty listing is the markup changing — not the authority
        # publishing nothing. Returning 0 here is indistinguishable
        # from a clean run, which is how a dead scraper stays quiet.
        raise RuntimeError(
            "[jcaa discover] listing parsed to zero rows. The markup has"
            " probably changed; refusing to report an empty run as success."
        )

    print(f"[jcaa discover] found {len(pdf_list)} candidate PDFs")

    inserted = 0
    for source_url, title in pdf_list:
        fname = source_url.split("/")[-1]
        stem = fname.rsplit(".", 1)[0]

        case_id = jcaa.make_case_id(stem)
        if conn.execute(
            "SELECT 1 FROM jcaa_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            print(f"[jcaa discover] skip existing: {case_id}")
            continue

        year, event_date, registration, _ = jcaa._extract_from_filename(source_url)
        # Determine report_type from filename
        fname_upper = fname.upper()
        if "PRELIMINARY" in fname_upper:
            report_type = "Preliminary"
        elif "FINAL" in fname_upper:
            report_type = "Final"
        else:
            report_type = "Investigation"

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO jcaa_reports "
            "(case_id, report_url, pdf_url, title, event_class, registration, "
            "date_of_occurrence, lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                source_url,
                source_url,
                title or stem.replace("-", " "),
                report_type,
                registration,
                event_date,
                "en",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        conn.commit()
        inserted += 1
        print(f"[jcaa discover] inserted: {case_id}  ({event_date or 'no-date'})")

    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows; advance to 'fetched'.

    Returns: number of rows iterated (including failures).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM jcaa_reports WHERE status=?",
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
                time.sleep(jcaa.DELAY)
                print(f"[jcaa fetch] {case_id} ...")
                jcaa.download(client, pdf_url, dest)
                pdf_path = dest
                print(f"[jcaa fetch] OK: {dest}")
            except Exception as exc:
                print(f"[jcaa fetch] {case_id}: download error: {exc}", file=sys.stderr)
                continue  # stay at 'new'

        try:
            conn.execute(
                "UPDATE jcaa_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[jcaa fetch] {case_id}: db error: {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text from PDFs; classify source_tier; harvest cover fields.

    source_tier:
      'pdf'     — len >= MIN_NARRATIVE (600)
      'short'   — SCANNED_FLOOR <= len < MIN_NARRATIVE
      'scanned' — 0 < len < SCANNED_FLOOR
      'none'    — no pdf / empty

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, aircraft, location, operator, "
        "date_of_occurrence FROM jcaa_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not full_text:
            narrative, tier = "", "none"
        elif len(full_text) < SCANNED_FLOOR:
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        # Harvest cover-block fields if not already set
        event_date = row["date_of_occurrence"] or jcaa.extract_event_date(full_text)
        aircraft = row["aircraft"] or jcaa.extract_aircraft(full_text)
        location = row["location"] or jcaa.extract_location(full_text)
        operator = row["operator"] or jcaa.extract_operator(full_text)
        registration = row["registration"] or jcaa.extract_registration(full_text)

        print(
            f"[jcaa parse] {row['case_id']}: tier={tier} "
            f"len={len(full_text)} date={event_date} reg={registration}"
        )

        conn.execute(
            "UPDATE jcaa_reports "
            "SET narrative_text=?, source_tier=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (
                narrative, tier, registration,
                event_date, aircraft, location, operator,
                db.STATUS_PARSED, db.now_ms(), row["case_id"],
            ),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit jcaa_accidents rows for buildable parsed reports.

    Skip rows with narrative_text shorter than _NARRATIVE_FLOOR (300) chars.
    country='JM'.  Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM jcaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE jcaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            print(f"[jcaa build] skipped (short): {row['case_id']} ({len(narrative)} chars)")
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO jcaa_accidents "
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
                "JM",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE jcaa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
        print(f"[jcaa build] built: {row['case_id']}")

    return built
