# ttcaa_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for TTCAA (Trinidad & Tobago).

discover(): checks the hardcoded seed list of confirmed report PDFs plus
  scans safety-related pages for additional PDF anchors matching investigation
  filename patterns.  Inserts new rows into ttcaa_reports.  Idempotent.

  Supersession: the 2022 preliminary for 9Y-TJU is not publicly available
  (all URL guesses returned 404).  Only the 2023 final report is ingested.
  No supersession logic is needed at this time.  If a preliminary URL is
  found in future, the superseded_by column is available.

  case_id stability: the case_id is derived from the URL filename and is
  stable across runs (e.g. 'TTCAA-9Y-TJU').  No year-refinement is done
  at parse time — the case_id from discover is the permanent key.

fetch(): downloads each status='new' row's PDF; advances to 'fetched'.

parse(): extracts text via pdftotext; harvests cover-block fields.

build(): emits ttcaa_accidents rows.  floor=300 chars.  country='TT'.
"""
import os
import sys
import time

from . import ttcaa, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_FLOOR

_NARRATIVE_FLOOR = 300   # spec floor


def discover(conn, client, full=False):
    """Enumerate confirmed investigation PDFs and insert new rows.

    Returns: number of rows inserted.
    """
    pdf_list = ttcaa.discover_pdf_urls(client)
    print(f"[ttcaa discover] found {len(pdf_list)} candidate PDFs")

    inserted = 0
    for source_url, report_type in pdf_list:
        fname = source_url.split("/")[-1]
        stem = fname.rsplit(".", 1)[0]

        # Stable case_id: derived from URL (registration only, no year)
        # so it is consistent across discover runs.
        registration = None
        reg_m = ttcaa._REG_IN_URL_RE.search(fname)
        if reg_m:
            registration = reg_m.group(1).upper()

        case_id = ttcaa.make_case_id(source_url, registration=registration, year=None)

        if conn.execute(
            "SELECT 1 FROM ttcaa_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            print(f"[ttcaa discover] skip existing: {case_id}")
            continue

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO ttcaa_reports "
            "(case_id, report_url, pdf_url, title, event_class, registration, "
            "lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                source_url,
                source_url,
                stem.replace("-", " "),
                report_type,
                registration,
                "en",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        conn.commit()
        inserted += 1
        print(f"[ttcaa discover] inserted: {case_id}")

    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows; advance to 'fetched'."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ttcaa_reports WHERE status=?",
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
                time.sleep(ttcaa.DELAY)
                print(f"[ttcaa fetch] {case_id} ...")
                ttcaa.download(client, pdf_url, dest)
                pdf_path = dest
                print(f"[ttcaa fetch] OK: {dest}")
            except Exception as exc:
                print(f"[ttcaa fetch] {case_id}: download error: {exc}", file=sys.stderr)
                continue

        try:
            conn.execute(
                "UPDATE ttcaa_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ttcaa fetch] {case_id}: db error: {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract text and classify; harvest cover fields.

    source_tier:
      'pdf'     — len >= MIN_NARRATIVE (600)
      'short'   — SCANNED_FLOOR <= len < MIN_NARRATIVE
      'scanned' — 0 < len < SCANNED_FLOOR
      'none'    — no pdf / empty

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, pdf_url, registration, aircraft, location, operator, "
        "date_of_occurrence FROM ttcaa_reports WHERE status=?",
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

        event_date = row["date_of_occurrence"] or ttcaa.extract_event_date(full_text)
        aircraft = row["aircraft"] or ttcaa.extract_aircraft(full_text)
        location = row["location"] or ttcaa.extract_location(full_text)
        operator = row["operator"] or ttcaa.extract_operator(full_text)
        registration = row["registration"] or ttcaa.extract_registration(full_text)

        case_id = row["case_id"]
        print(
            f"[ttcaa parse] {case_id}: tier={tier} "
            f"len={len(full_text)} date={event_date} reg={registration}"
        )

        conn.execute(
            "UPDATE ttcaa_reports "
            "SET narrative_text=?, source_tier=?, registration=?, "
            "date_of_occurrence=?, aircraft=?, location=?, operator=?, "
            "status=?, updated_at=? WHERE case_id=?",
            (
                narrative, tier, registration,
                event_date, aircraft, location, operator,
                db.STATUS_PARSED, db.now_ms(), case_id,
            ),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit ttcaa_accidents rows.  floor=300 chars.  country='TT'."""
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, superseded_by "
        "FROM ttcaa_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        # Skip superseded rows (e.g. preliminary replaced by final)
        if row["superseded_by"]:
            conn.execute(
                "UPDATE ttcaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            print(f"[ttcaa build] skipped (superseded by {row['superseded_by']}): {row['case_id']}")
            continue

        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ttcaa_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            print(f"[ttcaa build] skipped (short): {row['case_id']} ({len(narrative)} chars)")
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["aircraft"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO ttcaa_accidents "
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
                "TT",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ttcaa_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
        print(f"[ttcaa build] built: {row['case_id']}")

    return built
