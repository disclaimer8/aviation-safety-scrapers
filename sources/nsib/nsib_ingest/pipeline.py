# nsib_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for NSIB (Nigeria).

discover(): walk every air-reports listing page, parse the HTML table, and
  INSERT new rows into nsib_reports.  Only rows that carry a downloadable PDF
  are inserted (the ~86 final-report rows and other PDF-less rows have no
  narrative body, so there is nothing to ingest).  case_id is INTRINSIC:
  structured PDF-path ref, else cleaned registration + event date, namespaced
  by report type.  Idempotent — existing case_ids are skipped.

  Optionally (--wp-rest flag / wp_rest=True): also enumerate the WordPress REST
  API for final-report posts.  Posts with a PDF link follow the normal
  fetch→parse→build flow.  Posts without PDF use HTML content as narrative
  directly (source_tier='html', status='parsed'), skip the fetch/parse steps.

fetch(): for each status='new' row download the PDF and advance to 'fetched'.
  Per-row try/except: a download failure keeps the row at 'new' for retry.

parse(): pdftotext the PDF.  If text >= MIN_NARRATIVE -> tier='pdf'.  If the
  text layer is thin/empty (a scanned report), fall back to ocr_extract (PDF)
  -> tier='ocr'.  OCR only runs where ocrmypdf/tesseract exist (mini-PC).

build(): emit nsib_accidents rows; skip rows whose narrative is shorter than
  _NARRATIVE_FLOOR.
"""
import os
import sys
import time

from . import nsib, db, dates, text
from .pdf import extract_text, ocr_extract, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; rows with less are treated as non-report events


def discover(conn, client, full=False, wp_rest=False):
    """
    Walk NSIB air-reports listing pages and INSERT new case_ids.

    full: accepted for API parity (the whole listing is always walked;
          per-case_id skip handles idempotency).

    wp_rest: if True, also enumerate the WordPress REST API for final-report
             posts that don't appear in the listing table (HTML-only summaries
             for 85+ historic finals without downloadable PDF).

    Returns: number of rows inserted.
    """
    index_resp = client.get(nsib.INDEX_URL)
    index_resp.raise_for_status()
    index_html = index_resp.text

    page_urls = nsib.iter_page_urls(index_html)

    inserted = 0
    seen_ids = set()
    for i, page_url in enumerate(page_urls):
        if i == 0:
            page_html = index_html
        else:
            time.sleep(nsib.DELAY)
            try:
                r = client.get(page_url)
                r.raise_for_status()
                page_html = r.text
            except Exception as exc:
                print(f"[nsib discover] {page_url}: {exc}", file=sys.stderr)
                continue

        page_rows = nsib.parse_listing(page_html)
        # Append the API rows to the final page so they flow through the very
        # same insert body below — identical dedup (seen_ids + the case_id
        # lookup), identical status filter, no parallel code path.
        if i == len(page_urls) - 1:
            api_rows = nsib.fetch_api_rows(client)
            print(f"[nsib discover] API contributed {len(api_rows)} aviation rows")
            page_rows = page_rows + api_rows

        for row in page_rows:
            pdf_url = row.get("pdf_url")
            if not pdf_url:
                continue  # no downloadable report body -> nothing to ingest

            # Only ingest genuine investigation reports.  Skip blank form/
            # template rows (status 'Report' = the NSIB Form-001 template).
            status = (row.get("status") or "").lower()
            if not any(k in status for k in ("preliminary", "interim", "final")):
                continue

            struct_ref = text.struct_ref_from_path(pdf_url)
            # registration: prefer clean listing cell, else from filename
            reg = text.clean_registration(row.get("reg_cell")) or \
                text.clean_registration(pdf_url.rsplit("/", 1)[-1])
            case_id = text.make_case_id(
                struct_ref, reg, row.get("date"), row.get("status")
            )
            if not case_id or case_id in seen_ids:
                continue
            seen_ids.add(case_id)

            if conn.execute(
                "SELECT 1 FROM nsib_reports WHERE case_id=?", (case_id,)
            ).fetchone():
                continue

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO nsib_reports "
                "(case_id, report_url, pdf_url, title, event_class, aircraft, "
                "registration, operator, date_of_occurrence, location, "
                "report_type, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    row.get("post_url"),
                    pdf_url,
                    row.get("title"),
                    row.get("event_class"),
                    None,
                    reg,
                    row.get("operator"),
                    row.get("date"),
                    None,
                    row.get("status"),
                    "en",
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()

    # Optionally extend via WP REST API (final reports without PDF in table)
    if wp_rest:
        from .wp_rest import discover_wp_rest
        wp_stats = discover_wp_rest(conn, client, db)
        print(
            f"[nsib discover wp-rest] inserted_with_pdf={wp_stats['inserted_with_pdf']} "
            f"inserted_html_only={wp_stats['inserted_html_only']} "
            f"already_known={wp_stats['already_known']}",
            file=sys.stderr,
        )
        inserted += wp_stats["inserted_with_pdf"] + wp_stats["inserted_html_only"]

    return inserted


def fetch(conn, client, pdf_dir):
    """
    Download the PDF for each status='new' row and advance to 'fetched'.

    Per-row try/except: a download failure keeps the row at 'new' for retry.
    Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM nsib_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        safe = case_id.replace("/", "_").replace(" ", "_")
        dest = os.path.join(pdf_dir, safe + ".pdf")
        try:
            time.sleep(nsib.DELAY)
            nsib.download(client, pdf_url, dest)
        except Exception as exc:
            print(f"[nsib fetch] {case_id}: download {exc}", file=sys.stderr)
            continue  # stay at 'new' for retry

        try:
            conn.execute(
                "UPDATE nsib_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[nsib fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn, enable_ocr=True):
    """
    Extract text for each status='fetched' row.

    tier:
      'pdf'   — pdftotext text >= MIN_NARRATIVE
      'ocr'   — pdftotext thin/empty AND OCR recovered >= MIN_NARRATIVE
      'short' — some text but below threshold
      'none'  — no text at all

    enable_ocr: when False (CI / non-mini-PC), the OCR fallback is skipped.
    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM nsib_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""
        tier = "pdf"

        if len(full_text) < MIN_NARRATIVE and pdf_path and enable_ocr:
            ocr_text = ocr_extract(pdf_path, lang="eng")
            if len(ocr_text) > len(full_text):
                full_text = ocr_text
                tier = "ocr"

        if len(full_text) >= MIN_NARRATIVE:
            narrative = full_text
            if tier != "ocr":
                tier = "pdf"
        elif full_text:
            narrative = full_text
            tier = "short"
        else:
            narrative = ""
            tier = "none"

        conn.execute(
            "UPDATE nsib_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """
    Emit a nsib_accidents row per status='parsed' row, or skip thin ones.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, report_type "
        "FROM nsib_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE nsib_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        # NSIB publishes no occurrence date, so date_of_occurrence is None for
        # every API-discovered row. Recover it from the case id and the report
        # prose (see dates.py); this is the point where both are in hand.
        event_date = row["date_of_occurrence"]
        if not event_date:
            event_date, _basis = dates.recover_event_date(
                row["case_id"], narrative
            )

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO nsib_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                event_date,
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "NG",
                narrative,
                None,
                source_url,
                row["report_type"] or row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE nsib_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
