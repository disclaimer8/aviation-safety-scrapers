# ntsbcarol_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for ntsbcarol.

Stages:
  discover(): Query CAROL for all completed board-level aviation investigations
              (DCA/ANC/ERA/CEN prefixes, Mode=Aviation, ReportType=Report).
              Inserts new rows into ntsbcarol_cases with status='new'.
              Idempotent — existing case_ids are skipped.

  fetch():    Download the final report PDF for each status='new' row that has
              a report_no.  Advances status to 'fetched'.

  parse():    Extract narrative text and probable cause from the PDF.
              Advances status to 'parsed'.

  build():    Emit rows into ntsbcarol_accidents (the 14-col + extras contract).
              Rows with insufficient narrative are marked 'skipped'.
"""
import os
import sys
import time

from . import carol, db, text

# Minimum narrative to include in accidents table
_NARRATIVE_FLOOR = 200  # chars


# ── discover ─────────────────────────────────────────────────────────────────

def discover(conn, session_id=None, full=False):
    """Query CAROL and insert new board-level cases into ntsbcarol_cases.

    Args:
        conn: open sqlite3 connection
        session_id: existing CAROL session ID (created if None)
        full: unused (accepted for API parity with other ingest repos)

    Returns:
        int: number of new rows inserted
    """
    if session_id is None:
        session_id = carol.create_session()
        print(f"[ntsbcarol discover] session={session_id}", flush=True)

    inserted = 0

    def _progress(prefix, offset, n, total):
        print(
            f"[ntsbcarol discover] {prefix} offset={offset} got={n} total={total}",
            flush=True,
        )

    for row in carol.get_all_board_cases(session_id, progress_cb=_progress):
        ntsb_no = row["ntsb_no"]
        if not ntsb_no:
            continue

        cid = carol.case_id(ntsb_no)

        # Skip if already known
        if conn.execute("SELECT 1 FROM ntsbcarol_cases WHERE case_id=?", (cid,)).fetchone():
            continue

        # Skip completed=False cases (in-work investigations)
        if row.get("completion_status", "").lower() == "in work":
            print(f"[ntsbcarol discover] skip in-work {ntsb_no}", flush=True)
            continue

        event_date = carol.parse_event_date(row.get("event_date_raw"))

        try:
            fat = int(row.get("fatalities_str") or 0) or None
        except (ValueError, TypeError):
            fat = None

        pdf_u = carol.pdf_url(row.get("report_no"))

        ts = db.now_ms()
        conn.execute(
            """INSERT INTO ntsbcarol_cases
               (case_id, ntsb_no, mkey, report_no, event_date,
                city, state, country, registration, make, model,
                event_type, highest_injury, fatalities_total,
                pdf_url, source_tier, status, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid,
                ntsb_no,
                row.get("mkey", ""),
                row.get("report_no", "") or None,
                event_date,
                row.get("city") or None,
                row.get("state") or None,
                "US",   # all board cases are US jurisdiction
                row.get("registration") or None,
                row.get("make") or None,
                row.get("model") or None,
                row.get("event_type") or None,
                row.get("highest_injury") or None,
                fat,
                pdf_u,
                None,          # source_tier set by parse()
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1

    conn.commit()
    print(f"[ntsbcarol discover] inserted={inserted}", flush=True)
    return inserted


# ── fetch ────────────────────────────────────────────────────────────────────

def fetch(conn, pdf_dir):
    """Download PDFs for status='new' rows that have a pdf_url.

    Args:
        conn: open sqlite3 connection
        pdf_dir: directory to save PDFs

    Returns:
        int: number of rows processed
    """
    os.makedirs(pdf_dir, exist_ok=True)

    rows = conn.execute(
        "SELECT case_id, ntsb_no, report_no, pdf_url "
        "FROM ntsbcarol_cases WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    processed = 0
    for row in rows:
        case_id = row["case_id"]
        pdf_u = row["pdf_url"]
        report_no = row["report_no"]

        pdf_path = None
        if pdf_u and report_no:
            safe_name = f"{report_no.lower()}.pdf"
            dest = os.path.join(pdf_dir, safe_name)
            if os.path.exists(dest) and os.path.getsize(dest) > 10000:
                # Already downloaded
                pdf_path = dest
            else:
                try:
                    time.sleep(carol.DELAY)
                    carol.download_pdf(report_no, dest)
                    pdf_path = dest
                    print(
                        f"[ntsbcarol fetch] {case_id}: downloaded {os.path.getsize(dest) // 1024}KB",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[ntsbcarol fetch] {case_id}: download error {exc}", file=sys.stderr, flush=True)
                    # Stay at 'new' for retry
                    continue

        conn.execute(
            "UPDATE ntsbcarol_cases SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
            (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()
        processed += 1

    print(f"[ntsbcarol fetch] processed={processed}", flush=True)
    return processed


# ── parse ────────────────────────────────────────────────────────────────────

def parse(conn):
    """Extract narrative text + probable cause from fetched PDFs.

    Advances status to 'parsed'.

    Returns:
        int: number of rows processed
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ntsbcarol_cases WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    processed = 0
    for row in rows:
        pdf_path = row["pdf_path"]

        if pdf_path and os.path.exists(pdf_path):
            full_text, tier = text.extract_text(pdf_path)
        else:
            full_text, tier = "", "none"

        narrative = text.cap_narrative(full_text)
        probable_cause = text.extract_probable_cause(full_text)

        conn.execute(
            """UPDATE ntsbcarol_cases
               SET narrative_text=?, probable_cause=?, source_tier=?,
                   status=?, updated_at=?
               WHERE case_id=?""",
            (narrative, probable_cause, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        processed += 1

    print(f"[ntsbcarol parse] processed={processed}", flush=True)
    return processed


# ── build ────────────────────────────────────────────────────────────────────

def build(conn):
    """Emit rows into ntsbcarol_accidents from parsed cases.

    Rows with narrative < _NARRATIVE_FLOOR are marked 'skipped'.

    Returns:
        int: number of rows built (inserted/replaced)
    """
    rows = conn.execute(
        """SELECT case_id, ntsb_no, event_date, city, state, country,
                  registration, make, model, fatalities_total, phase, category,
                  narrative_text, probable_cause, source_tier, highest_injury
           FROM ntsbcarol_cases WHERE status=?""",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ntsbcarol_cases SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # Compose location string
        parts = [p for p in [row["city"], row["state"]] if p]
        location = ", ".join(parts) if parts else None

        # Aircraft type string (make + model)
        aircraft_parts = [p for p in [row["make"], row["model"]] if p]
        aircraft = " ".join(aircraft_parts) if aircraft_parts else None

        # site_slug: lowercased ntsb_no used for URL routing
        site_slug = row["ntsb_no"].lower()

        # source_url: canonical carol.ntsb.gov/event URL
        source_url = f"https://carol.ntsb.gov/event/{row['ntsb_no']}"

        ts = db.now_ms()
        conn.execute(
            """INSERT OR REPLACE INTO ntsbcarol_accidents
               (case_id, event_date, aircraft, registration, operator,
                location, country, narrative_text, probable_cause,
                source_url, report_type, site_slug,
                fatalities_total, phase, category, lang, built_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row["case_id"],
                row["event_date"],
                aircraft,
                row["registration"] or None,
                None,    # operator: not available in CAROL summary
                location,
                row["country"] or "US",
                narrative,
                row["probable_cause"] or None,
                source_url,
                "final",
                site_slug,
                row["fatalities_total"],
                row["phase"] or None,
                row["category"] or None,
                "en",
                ts,
            ),
        )
        conn.execute(
            "UPDATE ntsbcarol_cases SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, ts, row["case_id"]),
        )
        conn.commit()
        built += 1

    print(f"[ntsbcarol build] built={built}", flush=True)
    return built
