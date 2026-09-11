# nbaai_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for NBAAI (Ukraine).

discover(): walks the enquiry-sitemap.xml, fetches each /enquiry/<slug>/ detail
  page, parses title/registration/event-date/narrative-body/attached-PDF, and
  INSERTs new case_ids into nbaai_reports.  The HTML narrative body is stored in
  narrative_text immediately (most reports are HTML-only); pdf_url is set when an
  attached final-report PDF exists.  Idempotent — known case_ids are skipped.

fetch(): for each status='new' row WITH a pdf_url: downloads the PDF -> 'fetched'.
  Rows without a pdf_url (HTML-only, the majority) advance to 'fetched' with
  pdf_path=None.  Per-row try/except: a download failure keeps the row at 'new'.

parse(): determines narrative + source_tier.
  • If a PDF is present:
      pdftotext -> is_usable_text()?  (clean Unicode Ukrainian/Russian/English)
        - usable & >= MIN_NARRATIVE -> tier 'pdf'
      else (mojibake non-Unicode font, OR scanned/empty text layer) ->
        ocr_extract(pdf, "ukr+rus"):
          - OCR usable & >= _NARRATIVE_FLOOR -> tier 'ocr'
          - else fall back to the HTML body (tier 'html') if present
          - else 'short' (some text) / 'none'
  • If no PDF: use the HTML narrative body captured at discover time -> tier
    'html' when >= _NARRATIVE_FLOOR, else 'short'/'none'.

build(): emits nbaai_accidents rows; narratives shorter than _NARRATIVE_FLOOR
  are skipped.  country = 'UA'.
"""
import os
import sys
import time

from . import db, nbaai, text
from .pdf import extract_text, ocr_extract, is_usable_text, MIN_NARRATIVE

# 300 matches prod's NARRATIVE_MIN. It was 80 on the host, which
# admitted rows of 80-299 chars that prod can only ever render
# noindex. _common/tests/test_narrative_floor enforces this.
_NARRATIVE_FLOOR = 300  # chars; rows with less are treated as non-report events
_OCR_LANG = "ukr+rus"


def discover(conn, client, full=False):
    """Walk the enquiry sitemap and INSERT new case_ids into nbaai_reports.

    Returns: number of rows inserted.
    """
    sm_resp = client.get(nbaai.SITEMAP_URL)
    sm_resp.raise_for_status()
    sm_xml = (
        sm_resp.content.decode("utf-8", "replace")
        if isinstance(sm_resp.content, bytes)
        else sm_resp.content
    )
    urls = nbaai.iter_enquiry_urls(sm_xml)

    inserted = 0
    for url in urls:
        # Skip already-known reports (idempotent) without re-fetching the page
        # only when we can derive the case_id cheaply; the case_id depends on the
        # detail page (event date), so we fetch then check.
        time.sleep(nbaai.DELAY)
        try:
            resp = client.get(url)
            resp.raise_for_status()
            page = (
                resp.content.decode("utf-8", "replace")
                if isinstance(resp.content, bytes)
                else resp.content
            )
        except Exception as exc:
            print(f"[nbaai discover] {url}: {exc}", file=sys.stderr)
            continue

        row = nbaai.parse_detail(page, url)
        case_id = row["case_id"]
        if conn.execute(
            "SELECT 1 FROM nbaai_reports WHERE case_id=?", (case_id,)
        ).fetchone():
            continue

        narrative_html = row.get("narrative_html") or ""
        html_text = text.strip_html(narrative_html)

        ts = db.now_ms()
        conn.execute(
            "INSERT INTO nbaai_reports "
            "(case_id, report_url, pdf_url, title, event_class, aircraft, "
            "registration, date_of_occurrence, location, narrative_text, "
            "lang, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                case_id,
                row.get("report_url"),
                row.get("pdf_url"),
                row.get("title"),
                row.get("event_class"),
                row.get("aircraft"),
                row.get("registration"),
                row.get("date_of_occurrence"),
                row.get("location"),
                html_text,           # provisional HTML narrative (may be upgraded by parse)
                "uk",
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        inserted += 1
        conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download attached PDFs for status='new' rows and advance to 'fetched'.

    Rows without pdf_url advance with pdf_path=None.  A download failure keeps
    the row at 'new' for retry.  Returns: number of rows iterated.
    """
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM nbaai_reports WHERE status=?",
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
                time.sleep(nbaai.DELAY)
                nbaai.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[nbaai fetch] {case_id}: download {exc}", file=sys.stderr)
                continue  # stay at 'new'

        try:
            conn.execute(
                "UPDATE nbaai_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as exc:
            print(f"[nbaai fetch] {case_id}: db {exc}", file=sys.stderr)

    return len(rows)


def parse(conn):
    """Determine narrative_text + source_tier for status='fetched' rows.

    source_tier:
      'pdf'   — clean PDF text-layer (passes mojibake gate) >= MIN_NARRATIVE
      'ocr'   — text-layer was mojibake/scanned; OCR (ukr+rus) recovered text
                >= _NARRATIVE_FLOOR
      'html'  — no usable PDF; the enquiry HTML narrative body is used
      'short' — some text present but below floor
      'none'  — no usable text at all

    Returns: number of rows processed.
    """
    rows = conn.execute(
        "SELECT case_id, pdf_path, narrative_text FROM nbaai_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        html_body = (row["narrative_text"] or "").strip()

        narrative, tier = "", "none"

        if pdf_path:
            pdf_text = extract_text(pdf_path)
            if is_usable_text(pdf_text) and len(pdf_text) >= MIN_NARRATIVE:
                # clean Unicode text layer
                narrative, tier = pdf_text, "pdf"
            else:
                # mojibake (non-Unicode font) OR scanned/empty -> OCR fallback
                ocr_text = ocr_extract(pdf_path, _OCR_LANG)
                if is_usable_text(ocr_text) and len(ocr_text) >= _NARRATIVE_FLOOR:
                    narrative, tier = ocr_text, "ocr"
                elif len(html_body) >= _NARRATIVE_FLOOR:
                    narrative, tier = html_body, "html"
                elif pdf_text:
                    narrative, tier = pdf_text, "short"
                elif html_body:
                    narrative, tier = html_body, "short"
                else:
                    narrative, tier = "", "none"
        else:
            # HTML-only report (the common case)
            if len(html_body) >= _NARRATIVE_FLOOR:
                narrative, tier = html_body, "html"
            elif html_body:
                narrative, tier = html_body, "short"
            else:
                narrative, tier = "", "none"

        conn.execute(
            "UPDATE nbaai_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()

    return len(rows)


def build(conn):
    """Emit nbaai_accidents rows for status='parsed'; skip thin narratives.

    Returns: number of rows built (not skipped).
    """
    rows = conn.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM nbaai_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE nbaai_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )

        conn.execute(
            "INSERT OR REPLACE INTO nbaai_accidents "
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
                "UA",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE nbaai_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1

    return built
