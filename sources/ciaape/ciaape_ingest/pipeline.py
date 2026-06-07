# ciaape_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for CIAA Peru (gob.pe, multi-hop).

discover(): walks the collection sheets (?sheet=1..N) until a sheet yields no
  CIAA reports (or MAX_SHEETS).  Inserts new case_ids into ciaape_reports with
  the metadata embedded in the anchor link text (case_id, registration, date,
  event_class, report_type).  report_url = the individual report PAGE (the
  PDF-bearing hop).  Idempotent: existing case_ids are skipped.

fetch(): for each status='new' row, GETs the report page, extracts the real
  cdn.www.gob.pe PDF href (parse_report_page), downloads it, advances to
  'fetched' with pdf_url + pdf_path set.  Rows whose report page has no PDF
  advance to 'fetched' with pdf_path=None (build() drops them).  Per-row
  try/except: a failure keeps the row at 'new' for the next run.

parse(): pdftotext on the PDF.  Scanned gate: extracted text < SCANNED_MIN
  (~500 chars) -> source_tier='scanned' (skipped at build).  Otherwise
  'pdf' (>= MIN_NARRATIVE) or 'short' (some text but below MIN_NARRATIVE);
  both are buildable -- Peru narratives are legitimately thin.

build(): emits ciaape_accidents rows; skips 'scanned' rows and rows below
  _NARRATIVE_FLOOR.  country = 'PE'.
"""
import os
import sys
import time

from . import ciaape, db
from .pdf import extract_text, MIN_NARRATIVE
from .text import make_site_slug

SCANNED_MIN = 500   # < this many chars of extracted text => scanned PDF, skip
_NARRATIVE_FLOOR = 80


def _stored_is_final(report_url):
    """Re-derive a stored row's report tier from its report_url slug.

    ciaape_reports has no report_type column, but the gob.pe report-page slug
    reliably embeds the kind (informe-final / informe-preliminar /
    declaracion-provisional).  Used by discover() to decide whether an
    incoming Informe Final should upgrade an existing provisional row.
    """
    low = (report_url or "").lower()
    return "informe-final" in low or "informe_final" in low


def discover(conn, client, full=False):
    """Walk collection sheets and INSERT new case_ids into ciaape_reports.

    full: accepted for API parity (the whole collection is always walked;
          per-case_id skip handles idempotency).

    Returns: number of rows inserted.
    """
    inserted = 0
    for n in range(1, ciaape.MAX_SHEETS + 1):
        url = ciaape.sheet_url(n)
        time.sleep(ciaape.DELAY)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            html = resp.content.decode("utf-8", "replace") if isinstance(
                resp.content, bytes
            ) else resp.content
        except Exception as exc:
            print(f"[ciaape discover] {url}: {exc}", file=sys.stderr)
            break

        rows = ciaape.parse_collection(html)
        if not rows:
            break  # past the last populated sheet

        for row in rows:
            case_id = row["case_id"]
            existing = conn.execute(
                "SELECT report_url FROM ciaape_reports WHERE case_id=?", (case_id,)
            ).fetchone()
            if existing:
                # Cross-sheet / cross-run upgrade: a newly published Informe
                # Final must replace a stored provisional/preliminar row (the
                # within-sheet preference in parse_collection cannot see across
                # sheets or runs).  Reset to 'new' so fetch/parse/build re-run.
                incoming_final = (row.get("report_type") or "").startswith(
                    "Informe Final"
                )
                if incoming_final and not _stored_is_final(existing[0]):
                    conn.execute(
                        "UPDATE ciaape_reports SET report_url=?, title=?, "
                        "event_class=?, registration=?, date_of_occurrence=?, "
                        "status=?, updated_at=? WHERE case_id=?",
                        (
                            row.get("report_url"),
                            row.get("title"),
                            row.get("event_class"),
                            row.get("registration"),
                            row.get("date_of_occurrence"),
                            db.STATUS_NEW,
                            db.now_ms(),
                            case_id,
                        ),
                    )
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO ciaape_reports "
                "(case_id, report_url, title, event_class, registration, "
                "date_of_occurrence, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row.get("report_url"),
                    row.get("title"),
                    row.get("event_class"),
                    row.get("registration"),
                    row.get("date_of_occurrence"),
                    "es",
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """For each status='new' row: report-page hop -> cdn PDF -> download.

    Advances to 'fetched' (pdf_url + pdf_path).  No-PDF report pages advance
    with pdf_path=None.  Download/page failures keep the row at 'new'.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, report_url FROM ciaape_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        report_url = row["report_url"]

        pdf_url = None
        pdf_path = None
        try:
            time.sleep(ciaape.DELAY)
            page = client.get(report_url)
            page.raise_for_status()
            page_html = page.content.decode("utf-8", "replace") if isinstance(
                page.content, bytes
            ) else page.content
            pdf_url = ciaape.parse_report_page(page_html)

            if pdf_url:
                safe = case_id.replace("/", "_").replace(" ", "_")
                dest = os.path.join(pdf_dir, safe + ".pdf")
                time.sleep(ciaape.DELAY)
                ciaape.download(client, pdf_url, dest)
                pdf_path = dest
        except Exception as exc:
            print(f"[ciaape fetch] {case_id}: {exc}", file=sys.stderr)
            continue  # stay 'new' for retry

        try:
            conn.execute(
                "UPDATE ciaape_reports "
                "SET pdf_url=?, pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_url, pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ciaape fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """For each status='fetched' row: pdftotext + scanned gate.

    source_tier:
      'scanned' -- 0 < text < SCANNED_MIN  OR  no text at all from a PDF
      'none'    -- no pdf_path
      'short'   -- SCANNED_MIN <= text < MIN_NARRATIVE  (buildable; Peru is thin)
      'pdf'     -- text >= MIN_NARRATIVE

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ciaape_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if not pdf_path:
            narrative, tier = "", "none"
        elif len(full_text) < SCANNED_MIN:
            narrative, tier = full_text, "scanned"
        elif len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = full_text, "short"

        conn.execute(
            "UPDATE ciaape_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """For each status='parsed' row: emit a ciaape_accidents record or skip.

    Skip (status -> 'skipped') when source_tier='scanned'/'none' or the
    narrative is below _NARRATIVE_FLOOR chars.  source_url = pdf_url or report_url.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM ciaape_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        if tier in ("scanned", "none") or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ciaape_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO ciaape_accidents "
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
                "PE",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ciaape_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
