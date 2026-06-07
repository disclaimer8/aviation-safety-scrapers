# ciaado_ingest/pipeline.py
"""discover → fetch → parse → build pipeline for CIAA (Dominican Republic).

discover(): walks the three top Phoca categories (final → preliminary →
  provisional, the dedup priority order), then each per-year subcategory,
  inserting new case_ids into ciaado_reports.  Idempotent — a case_id already
  present is skipped, so a Final report walked first wins over a later
  Preliminary/Provisional document for the same case number.

fetch(): downloads each status='new' row's gated Phoca PDF (Referer = the
  subcategory page) and advances to 'fetched'.  Per-row try/except: a download
  failure keeps the row at 'new' for the next run.

parse(): pdftotext extraction.  text >= MIN_NARRATIVE (600) → 'pdf';
  shorter but non-empty → 'short'; empty (no text layer / scanned) → 'scanned'.

build(): emits ciaado_accidents rows (country='DO').  Rows whose narrative is
  shorter than _NARRATIVE_FLOOR (80 chars) are skipped (scanned PDFs, etc.).
"""
import os
import sys
import time

from . import ciaado, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; shorter narratives are treated as non-reports


def discover(conn, client, full=False):
    """Walk the Phoca category tree and INSERT new case_ids into ciaado_reports.

    full: accepted for API parity; the whole tree is always walked and
          per-case_id skip provides idempotency.

    Returns: number of rows inserted.
    """
    inserted = 0
    for top in ciaado.TOP_CATEGORIES:
        top_url = ciaado.BASE + "/index.php/informesf/category/" + top
        try:
            resp = client.get(top_url)
            resp.raise_for_status()
            top_html = _decode(resp)
        except Exception as exc:
            print(f"[ciaado discover] top {top_url}: {exc}", file=sys.stderr)
            continue

        subcat_urls = ciaado.iter_subcategory_urls(top_html)
        for sub_url in subcat_urls:
            time.sleep(ciaado.DELAY)
            try:
                sresp = client.get(sub_url)
                sresp.raise_for_status()
                sub_html = _decode(sresp)
            except Exception as exc:
                print(f"[ciaado discover] sub {sub_url}: {exc}", file=sys.stderr)
                continue

            rows = ciaado.parse_listing(sub_html, sub_url)
            for row in rows:
                case_id = row["case_id"]
                if conn.execute(
                    "SELECT 1 FROM ciaado_reports WHERE case_id=?", (case_id,)
                ).fetchone():
                    continue  # already known (Final wins over later docs)

                ts = db.now_ms()
                conn.execute(
                    "INSERT INTO ciaado_reports "
                    "(case_id, report_url, pdf_url, pdf_url_es, title, "
                    "event_class, registration, date_of_occurrence, lang, "
                    "status, discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        row.get("report_url"),
                        row.get("pdf_url"),
                        row.get("pdf_url"),   # pdf_url_es mirror (source is ES)
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


def _decode(resp):
    c = resp.content
    return c.decode("utf-8", "replace") if isinstance(c, bytes) else c


def fetch(conn, client, pdf_dir):
    """Download each status='new' row's PDF and advance to 'fetched'.

    Referer = the row's report_url (subcategory page) so the gated Phoca
    download accepts the request.  Per-row try/except keeps failures at 'new'.

    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url, report_url FROM ciaado_reports WHERE status=?",
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
                time.sleep(ciaado.DELAY)
                ciaado.download(client, pdf_url, dest, referer=row["report_url"])
                pdf_path = dest
            except Exception as exc:
                print(f"[ciaado fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE ciaado_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[ciaado fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Extract PDF text for each status='fetched' row.

    source_tier:
      'pdf'     — text length >= MIN_NARRATIVE (600)
      'short'   — some text, below threshold
      'scanned' — no extractable text (image-only / scanned PDF)

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM ciaado_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif full_text:
            narrative, tier = full_text, "short"
        else:
            narrative, tier = "", "scanned"

        conn.execute(
            "UPDATE ciaado_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit ciaado_accidents rows for status='parsed' reports.

    Skip (status → 'skipped') when narrative_text < _NARRATIVE_FLOOR (80) chars.
    source_url: pdf_url if present, else report_url.  country='DO'.

    Returns: number of rows built.
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM ciaado_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ciaado_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO ciaado_accidents "
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
                "DO",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ciaado_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
