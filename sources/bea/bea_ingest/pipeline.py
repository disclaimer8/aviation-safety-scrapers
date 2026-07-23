# bea_ingest/pipeline.py
"""
discover → fetch → parse → build pipeline for bea.aero notified-events.

NOTE on discover() walk strategy:
  BEA's global listing is newest-first but we do NOT use a consecutive-known
  early-break (unlike aaib).  The BEA walk requires fetching every paginator
  page regardless; we simply skip slugs already present in bea_reports.  A
  future optimisation could stop early once a long run of known slugs is seen
  (since the list IS newest-first), but keeping it simple-and-correct for now.

NOTE on build() skip threshold:
  Detail pages of delegated investigations have no /fileadmin PDF.
  get_detail_pdf_url returns None → pdf_path stays None → narrative_text is "".
  Such rows reach build() with an empty narrative; we skip them (narrative < 80
  chars floor) because there is nothing meaningful to surface to users.
  The 80-char floor also filters extremely short parse artefacts.
  Rows with a substantive narrative are always built regardless of whether
  registration/aircraft_type parsed successfully from the title.
"""
import os
import sys

from . import bea, db, header, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False):
    """
    Walk the BEA global event list and INSERT new slugs into bea_reports.

    full: accepted for API parity with aaib; currently has no extra effect
          (the whole list is always walked; per-slug skip handles idempotency).

    Returns: number of rows inserted.
    """
    inserted = 0
    for e in bea.iter_events(client):
        slug = e["slug"]
        if conn.execute("SELECT 1 FROM bea_reports WHERE slug=?", (slug,)).fetchone():
            continue  # already known — skip, keep walking (no early-break)
        parsed = text.parse_event_title(e["title"])
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO bea_reports "
            "(slug, detail_url, title, event_class, aircraft_type, registration, "
            "date_of_occurrence, location, operator, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug,
                e["detail_url"],
                e["title"],
                parsed["event_class"],
                parsed["aircraft_type"],
                parsed["registration"],
                parsed["date_iso"],
                parsed["location"],
                parsed["operator"],
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


REFETCH_BATCH = 250
REFETCH_COOLDOWN_MS = 30 * 86_400_000  # re-check a given stub at most monthly
REFETCH_MAX_AGE = "-3 years"           # older delegated events rarely gain a BEA PDF


def refetch(conn, limit=REFETCH_BATCH):
    """
    Re-queue 'skipped' stub rows (empty narrative — the detail page had no PDF
    when last checked) back to status='new' so this cycle's fetch() re-resolves
    their detail page.

    Why: BEA lists a notified event immediately as a PDF-less stub and attaches
    the report PDF to the SAME page months later.  discover() skips known slugs
    and nothing else ever revisits a terminal 'skipped' row, so every report
    published after its stub was first seen was silently lost — found
    2026-07-23 after 7 straight weekly cycles of "built: 0" while discover/
    parse kept succeeding (3 239 frozen stubs vs 2 938 built).

    Cohort control keeps the weekly crawl polite: only stubs with a recent (or
    unknown) occurrence date, not re-checked within the cooldown, oldest-checked
    first.  Stubs that still have no PDF flow back to 'skipped' through the
    normal fetch→parse→build chain, with last_refetch_at holding them out of
    rotation for the next month.

    Returns: number of rows re-queued.
    """
    now = db.now_ms()
    rows = conn.execute(
        "SELECT slug FROM bea_reports "
        "WHERE status=? AND (narrative_text IS NULL OR narrative_text='') "
        "AND (date_of_occurrence IS NULL OR date_of_occurrence >= date('now', ?)) "
        "AND COALESCE(last_refetch_at, 0) <= ? "
        "ORDER BY COALESCE(last_refetch_at, 0), date_of_occurrence DESC "
        "LIMIT ?",
        (db.STATUS_SKIPPED, REFETCH_MAX_AGE, now - REFETCH_COOLDOWN_MS, limit),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE bea_reports SET status=?, last_refetch_at=?, updated_at=? WHERE slug=?",
            (db.STATUS_NEW, now, now, row["slug"]),
        )
    conn.commit()
    return len(rows)


def fetch(conn, client, pdf_dir):
    """
    For each status='new' row: resolve the detail-page PDF URL, download it,
    then advance the row to status='fetched'.

    Per-row try/except ensures one bad report never aborts the batch.
    Rows that fail the detail-GET stay at status='new' for retry on the next run.
    Rows whose detail page has no PDF (delegated investigations) are still
    advanced to 'fetched' with pdf_path=None; parse() will produce an empty
    narrative and build() will skip them.

    Returns: number of rows iterated (not just successful ones).
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT slug, detail_url FROM bea_reports WHERE status=?", (db.STATUS_NEW,)
    ).fetchall()
    for row in rows:
        slug = row["slug"]
        detail_url = row["detail_url"]

        # Resolve PDF URL from the detail page — failure keeps row at 'new' for retry.
        try:
            pdf_url = bea.get_detail_pdf_url(client, detail_url)
        except Exception as e:
            # A 404 detail page is gone for good (BEA occasionally renames a
            # slug — e.g. fixing a typo — and the event re-enters via discover
            # under the new slug).  Park it back at 'skipped' so it rotates on
            # the monthly refetch cooldown instead of retrying every cycle
            # forever at 'new'.
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 404:
                conn.execute(
                    "UPDATE bea_reports SET status=?, last_refetch_at=?, updated_at=? WHERE slug=?",
                    (db.STATUS_SKIPPED, db.now_ms(), db.now_ms(), slug),
                )
                conn.commit()
            print(f"[bea fetch] {slug}: detail {e}", file=sys.stderr)
            continue

        # Download is best-effort — failure doesn't abort; pdf_path stays None.
        pdf_path = None
        if pdf_url:
            try:
                candidate = os.path.join(pdf_dir, slug + ".pdf")
                bea.download(client, pdf_url, candidate)
                pdf_path = candidate
            except Exception as e:
                print(f"[bea fetch] {slug}: pdf {e}", file=sys.stderr)

        try:
            conn.execute(
                "UPDATE bea_reports SET pdf_url=?, pdf_path=?, status=?, updated_at=? WHERE slug=?",
                (pdf_url, pdf_path, db.STATUS_FETCHED, db.now_ms(), slug),
            )
            conn.commit()
        except Exception as e:
            print(f"[bea fetch] {slug}: db {e}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """
    For each status='fetched' row: extract text from the PDF (if present).
    If PDF text meets MIN_NARRATIVE threshold → tier='pdf' and header metadata
    is parsed from the narrative and merged (header wins, title-derived fallback).
    Otherwise narrative is empty and tier='none' (BEA has no body fallback).

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT slug, pdf_path, aircraft_type, registration, date_of_occurrence, location "
        "FROM bea_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()
    for row in rows:
        full_text = extract_text(row["pdf_path"]) if row["pdf_path"] else ""
        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
            h = header.parse_header(narrative)
            ac  = h.get("aircraft")     or row["aircraft_type"]
            reg = h.get("registration") or row["registration"]
            dt  = h.get("date_iso")     or row["date_of_occurrence"]
            loc = h.get("location")     or row["location"]
        else:
            narrative, tier = "", "none"
            ac  = row["aircraft_type"]
            reg = row["registration"]
            dt  = row["date_of_occurrence"]
            loc = row["location"]
        conn.execute(
            "UPDATE bea_reports "
            "SET narrative_text=?, source_tier=?, aircraft_type=?, registration=?, "
            "date_of_occurrence=?, location=?, status=?, updated_at=? "
            "WHERE slug=?",
            (narrative, tier, ac, reg, dt, loc, db.STATUS_PARSED, db.now_ms(), row["slug"]),
        )
        conn.commit()
    return len(rows)


def build(conn):
    """
    For each status='parsed' row: emit a bea_accidents record or skip.

    Skip criteria (status → 'skipped'):
      • narrative_text shorter than _NARRATIVE_FLOOR chars (delegated/non-report
        events whose detail page has no PDF produce an empty narrative).
      Registration and aircraft_type are NOT required to build a row: a full
      narrative is sufficient.  NULL metadata is acceptable in bea_accidents
      until a reparse (scripts/reparse_rebuild.py) fills it in.

    source_url: detail_url is already absolute from bea.iter_events; if it
    somehow arrives as a bare path, prefix bea.BASE.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT slug, aircraft_type, registration, location, date_of_occurrence, "
        "narrative_text, event_class, detail_url, operator "
        "FROM bea_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        # Skip only when there is no substantive narrative (delegated/non-report
        # events whose detail page has no PDF).  We intentionally do NOT require
        # registration or aircraft_type here: a row with a full 20K-60K-char
        # French narrative is a real report even if title parsing failed to
        # extract metadata.  The metadata columns will simply be NULL in
        # bea_accidents until a reparse corrects them.
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE bea_reports SET status=?, updated_at=? WHERE slug=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["slug"]),
            )
            conn.commit()
            continue

        detail_url = row["detail_url"] or ""
        source_url = detail_url if detail_url.startswith("http") else bea.BASE + detail_url
        site_slug = text.make_site_slug(row["aircraft_type"], row["registration"], row["location"])

        conn.execute(
            "INSERT OR REPLACE INTO bea_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["slug"],
                row["date_of_occurrence"],
                row["aircraft_type"],
                row["registration"],
                row["operator"],
                row["location"],
                "FR",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE bea_reports SET status=?, updated_at=? WHERE slug=?",
            (db.STATUS_BUILT, db.now_ms(), row["slug"]),
        )
        conn.commit()
        built += 1
    return built
