# pkbwl_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for PKBWL Poland.

discover() walks the listing https://pkbwl.gov.pl/raporty/page/N/ page by page
(10 slugs/page) until a page returns HTTP 404 (clean past-the-end stop), GETs
each NEW report's detail page, parses the inline bilingual <dl> metadata
(registration / date / class / operator / location), picks the preferred
narrative PDF (Final > Interim > Preliminary > Resolution; EN variant preferred
within the chosen type), and INSERTs a row keyed on case_id (= slug YYYY-NNNN).
Numbering is NOT contiguous → we walk pages, never guess slugs. Reports with no
report PDF at all are skipped at insert time.

fetch() downloads each new narrative PDF + pdftotext (tier 'pdf'). ⚠️ If the
chosen EN variant extracts as a degenerate letter-spaced render
("P R E L IM IN A RY"), it falls back to the Polish PDF of the same report type
and records lang='pl'. A PDF with no usable text layer is tiered 'scanned'.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
pkbwl_accidents (country PL; lang = the variant actually kept).
"""
import os
import re
import sys
import time

from . import pkbwl, db, pdf
from .text import make_site_slug

# Every value below is derived from the source's own HTML/JSON, so it must not
# be trusted as a path component: os.path.join with a value containing "/" or
# ".." writes outside pdf_dir. BFU already carried this guard; most packages
# did not.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    """Reduce an untrusted identifier to a single, safe path component."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", str(name or ""))
    cleaned = cleaned.lstrip(".") or "unnamed"
    return cleaned[:120]


_NARRATIVE_FLOOR = 300
_MAX_PAGES = 400  # safety cap; real last page ~236, 404 stops earlier


def discover(conn, client, full=False, max_pages=_MAX_PAGES):
    """Walk listing pages until 404; GET each new detail page; INSERT. Returns count."""
    inserted = 0
    page = 1
    while page <= max_pages:
        time.sleep(pkbwl.DELAY)
        try:
            status, listing_html = pkbwl.fetch_listing(client, page)
        except Exception as e:
            # A transport error is NOT the end of the listing. Stopping here
            # turned one 502 on page 30 into a truncated crawl reported as a
            # successful one. Rows already inserted are committed, so the next
            # run resumes rather than starting over.
            conn.commit()
            raise RuntimeError(
                f"[pkbwl discover] page {page} failed after retries: {e} — "
                f"walk truncated at {inserted} new rows"
            ) from e
        if status == 404:
            break  # walked past the last page → clean stop

        slugs = pkbwl.extract_slugs(listing_html)
        if not slugs:
            if page == 1:
                # PKBWL publishes ~2,300 reports, so zero slugs on page 1 never
                # means "no reports". It means the listing markup changed —
                # which stop-on-empty would report as a clean, empty run.
                conn.commit()
                raise RuntimeError(
                    "[pkbwl discover] page 1 yielded 0 report slugs — "
                    "listing markup has changed (extract_slugs no longer matches)"
                )
            break  # a later empty page ends the walk

        for slug in slugs:
            if conn.execute(
                "SELECT 1 FROM pkbwl_reports WHERE case_id=?", (slug,)
            ).fetchone():
                continue  # already discovered

            time.sleep(pkbwl.DELAY)
            try:
                detail_html = pkbwl.fetch_page(client, pkbwl.detail_url(slug))
            except Exception as e:
                print(f"[pkbwl discover] {slug}: detail failed: {e}",
                      file=sys.stderr)
                continue

            meta = pkbwl.parse_detail(detail_html, slug)
            chosen = pkbwl.pick_narrative(meta["documents"])
            if not chosen:
                continue  # no report PDF (interim-only/empty) → nothing to ingest
            pdf_url, lang, report_type = chosen

            if conn.execute(
                "SELECT 1 FROM pkbwl_reports WHERE pdf_url=?", (pdf_url,)
            ).fetchone():
                continue

            ts = db.now_ms()
            conn.execute(
                "INSERT INTO pkbwl_reports "
                "(case_id, pdf_url, page_url, report_type, lang, aircraft, "
                "registration, operator, occurrence_class, injury_level, "
                "investigation_status, date_of_occurrence, location, status, "
                "discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    slug,
                    pdf_url,
                    pkbwl.detail_url(slug),
                    report_type,
                    lang,
                    meta["aircraft"],
                    meta["registration"],
                    meta["operator"],
                    meta["occurrence_class"],
                    meta["injury_level"],
                    meta["investigation_status"],
                    meta["event_date"],
                    meta["location"],
                    db.STATUS_NEW,
                    ts,
                    ts,
                ),
            )
            inserted += 1
        conn.commit()
        page += 1
    return inserted


def fetch(conn, client, pdf_dir="pdfs"):
    """
    For each status='new' row: download the chosen PDF + pdftotext. If the
    chosen variant is EN and extracts degenerate (letter-spaced), re-download
    the Polish PDF of the same report type and keep that instead (lang='pl').
    """
    rows = conn.execute(
        "SELECT case_id, pdf_url, page_url, report_type, lang "
        "FROM pkbwl_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        pdf_url = row["pdf_url"]
        lang = row["lang"]
        pdf_path = os.path.join(pdf_dir, _safe_filename(case_id) + ".pdf")
        time.sleep(pkbwl.DELAY)
        try:
            pkbwl.download_pdf(client, pdf_url, pdf_path)
            text = pdf.extract_text(pdf_path)
        except Exception as e:
            print(f"[pkbwl fetch] {case_id}: pdf failed: {e}", file=sys.stderr)
            continue  # stays 'new', retried next cycle

        # ⚠️ Spaced-letter EN fallback: if the EN render is degenerate, re-fetch
        # the Polish file of the same report type and keep whichever is usable.
        if lang == "en" and pkbwl.is_degenerate(text, _NARRATIVE_FLOOR):
            pl_url = _pl_fallback_url(client, row)
            if pl_url and pl_url != pdf_url:
                try:
                    pkbwl.download_pdf(client, pl_url, pdf_path)
                    pl_text = pdf.extract_text(pdf_path)
                    if not pkbwl.is_degenerate(pl_text, _NARRATIVE_FLOOR):
                        text, pdf_url, lang = pl_text, pl_url, "pl"
                except Exception as e:
                    print(f"[pkbwl fetch] {case_id}: PL fallback failed: {e}",
                          file=sys.stderr)

        tier = "pdf"
        if len(text) < _NARRATIVE_FLOOR:
            tier = "scanned"  # no usable text layer

        try:
            conn.execute(
                "UPDATE pkbwl_reports SET narrative_text=?, source_tier=?, "
                "pdf_url=?, lang=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (text, tier, pdf_url, lang, pdf_path, db.STATUS_PARSED,
                 db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[pkbwl fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def _pl_fallback_url(client, row):
    """Re-read the detail page to find the PL sibling of the chosen report type."""
    try:
        detail_html = pkbwl.fetch_page(client, row["page_url"])
    except Exception:
        return None
    meta = pkbwl.parse_detail(detail_html, row["case_id"])
    return pkbwl.pl_fallback(meta["documents"], row["report_type"])


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into pkbwl_accidents."""
    rows = conn.execute(
        "SELECT case_id, pdf_url, page_url, report_type, lang, aircraft, "
        "registration, operator, location, date_of_occurrence, narrative_text "
        "FROM pkbwl_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE pkbwl_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO pkbwl_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, lang, narrative_text, probable_cause, source_url, "
            "report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "PL",
                row["lang"],
                narrative,
                None,
                row["page_url"] or pkbwl.BASE,
                row["report_type"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE pkbwl_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
