# dgacgt_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for DGAC Guatemala (UIA).

discover(): walk the INFORMES FINALES autoindex, list every per-year PDF, and
  INSERT new rows into dgacgt_reports.  case_id is derived from the filename
  (registration + ISO occurrence date).  Idempotent — known case_ids skipped.
  Because two files in the same year could collapse to the same case_id only
  if they share registration AND date (effectively the same accident), a
  duplicate case_id is treated as already-known and skipped.

fetch(): download each status='new' PDF -> 'fetched'.  Per-row try/except: a
  download failure keeps the row at 'new' for retry.

parse(): pdftotext extraction.  SCANNED-AWARE — when the extracted text is
  shorter than SCANNED_THRESHOLD (~500 chars) the report is almost certainly
  an image scan: source_tier='scanned' and build() will skip it.  Otherwise
  tier is 'pdf' (>= MIN_NARRATIVE) or 'short'.  We also opportunistically
  enrich aircraft/location/date and capture the official report_no from the
  PDF header text.

build(): emit dgacgt_accidents rows for tiers that carry a real narrative;
  scanned / too-short rows are marked 'skipped'.
"""
import os
import sys
import time

from . import dgacgt, db, text
from .pdf import extract_text, MIN_NARRATIVE, SCANNED_THRESHOLD

_NARRATIVE_FLOOR = 80  # chars; below this a row is not a real report body


def discover(conn, client, full=False):
    index_resp = client.get(dgacgt.INDEX_URL)
    index_resp.raise_for_status()
    index_html = index_resp.text

    year_urls = dgacgt.iter_year_urls(index_html)

    inserted = 0
    for year_url in year_urls:
        # year is the trailing path segment
        try:
            year = int(year_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            year = 0

        time.sleep(dgacgt.DELAY)
        try:
            yr = client.get(year_url)
            yr.raise_for_status()
            year_html = yr.text
        except Exception as exc:
            print(f"[dgacgt discover] {year_url}: {exc}", file=sys.stderr)
            continue

        for pdf_url in dgacgt.iter_pdf_urls(year_html):
            name = dgacgt.filename_from_url(pdf_url)
            registration = dgacgt.parse_registration_from_name(name)
            date_iso = dgacgt.parse_date_from_name(name)
            case_id = dgacgt.make_case_id(registration, date_iso, year, name)

            if conn.execute(
                "SELECT 1 FROM dgacgt_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue  # already known (idempotent)

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO dgacgt_reports "
                "(case_id, report_url, pdf_url, title, registration, "
                "date_of_occurrence, year, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    year_url,
                    pdf_url,
                    name,
                    registration,
                    date_iso,
                    year,
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
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM dgacgt_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]

        pdf_path = None
        if pdf_url:
            safe = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe + ".pdf")
            try:
                time.sleep(dgacgt.DELAY)
                dgacgt.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[dgacgt fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay 'new' for retry

        try:
            conn.execute(
                "UPDATE dgacgt_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[dgacgt fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    rows = conn.execute(
        "SELECT case_id, pdf_path, registration, date_of_occurrence "
        "FROM dgacgt_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif len(full_text) >= SCANNED_THRESHOLD:
            narrative, tier = full_text, "short"
        elif full_text:
            # very little text => scanned image PDF
            narrative, tier = full_text, "scanned"
        else:
            narrative, tier = "", "none"

        # opportunistic metadata enrichment from PDF header
        report_no = dgacgt.extract_report_no(full_text)
        meta = dgacgt.extract_pdf_metadata(full_text)
        aircraft = meta.get("aircraft")
        location = meta.get("location")
        date_iso = row["date_of_occurrence"] or meta.get("date_iso")

        conn.execute(
            "UPDATE dgacgt_reports "
            "SET narrative_text=?, source_tier=?, report_no=?, aircraft=COALESCE(aircraft,?), "
            "location=COALESCE(location,?), date_of_occurrence=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (
                narrative, tier, report_no, aircraft, location, date_iso,
                db.STATUS_PARSED, db.now_ms(), row["case_id"],
            ),
        )
        conn.commit()

    return len(rows)


def build(conn):
    rows = conn.execute(
        "SELECT case_id, report_no, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, pdf_url, report_url "
        "FROM dgacgt_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"]
        # Skip scanned PDFs and rows with no real narrative body.
        if tier == "scanned" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE dgacgt_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(row["case_id"])
        report_type = row["report_no"] or "Informe Final"

        conn.execute(
            "INSERT OR REPLACE INTO dgacgt_accidents "
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
                "GT",
                narrative,
                None,
                source_url,
                report_type,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE dgacgt_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
