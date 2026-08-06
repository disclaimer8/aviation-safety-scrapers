# ovv_ingest/pipeline.py
"""
discover → fetch(+parse) → build pipeline for OVV / Dutch Safety Board.

discover() walks `?_aviation_tax=uncategorized&_page=1..` (stop-on-empty),
inserting investigation slugs as case_ids.

fetch() GETs the detail page, ranks the hash-slug `-pdf/` doc links
(EN main report first), downloads candidates in order until one yields
pdftotext text >= the floor (scans/letters fall through), else keeps the
page summary (tier 'html').  Doc-less (ongoing) rows keep metadata and
stay 'new' — self-heal on the weekly cycle.

build() promotes 'parsed' rows with narrative >= _NARRATIVE_FLOOR into
ovv_accidents (country NL).
"""
import os
import sys
import time

from . import db, ovv, pdf
from .text import make_site_slug

_NARRATIVE_FLOOR = 300
_PDF_TEXT_FLOOR = 2000  # below this a doc is a scan/letter → try next doc
_MAX_DOC_TRIES = 3
# A section is short; a report is not. Two real OVV reports carry a section
# word in the filename and run past 230,000 characters.
_SECTION_MAX = 20000
OCR_LANG = "nld+eng"  # OVV reports are Dutch, many also English; tesseract langs


def discover(conn, client, full=False, max_pages=None):
    """Walk listing pages; INSERT new rows. Returns inserted count."""
    inserted = 0
    page = 1
    while True:
        if max_pages is not None and page > max_pages:
            break
        time.sleep(ovv.DELAY)
        try:
            html = ovv.fetch_listing_page(client, page)
        except Exception as e:
            # Do NOT treat this as the end of the listing. Stopping quietly
            # here turns one 502 on page 30 into a truncated crawl reported as
            # a successful one. Rows already inserted are committed, so the
            # next run resumes rather than starting over.
            conn.commit()
            raise RuntimeError(
                f"[ovv discover] page {page} failed after retries: {e} — "
                f"walk truncated at {inserted} new rows"
            ) from e
        rows = ovv.parse_listing(html)
        if not rows:
            if page == 1:
                # OVV publishes thousands of investigations, so zero anchors on
                # the first page never means "no reports". It means the markup
                # changed — which stop-on-empty would otherwise read as an
                # empty source and report as a clean run.
                raise RuntimeError(
                    "[ovv discover] page 1 yielded 0 investigation links — "
                    "listing markup has changed (_DETAIL_RE no longer matches)"
                )
            break
        for r in rows:
            if conn.execute(
                "SELECT 1 FROM ovv_reports WHERE case_id=?", (r["slug"],)
            ).fetchone():
                continue
            ts = db.now_ms()
            conn.execute(
                "INSERT INTO ovv_reports (case_id, detail_url, status, "
                "discovered_at, updated_at) VALUES (?,?,?,?,?)",
                (r["slug"], r["url"], db.STATUS_NEW, ts, ts),
            )
            inserted += 1
        conn.commit()
        page += 1
    return inserted


def fetch(conn, client, pdf_dir="pdfs", enable_ocr=True):
    """
    For each status='new' row: detail page → ranked docs → first with a
    real text layer wins; summary fallback; doc-less rows stay 'new'.

    When the best doc's text layer stays below _NARRATIVE_FLOOR the PDF is
    likely a scan/image-only report; if a PDF was downloaded we OCR it (on
    OCR_REMOTE when set) and keep the OCR text when it recovers more —
    tier='ocr'. OCR that stays thin falls through to the summary ('html') or
    'scanned' tier and is dropped by build() (quality self-filter).

    enable_ocr: when False (CI / no OCR host), the OCR fallback is skipped.
    """
    rows = conn.execute(
        "SELECT case_id, detail_url FROM ovv_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()
    os.makedirs(pdf_dir, exist_ok=True)
    for row in rows:
        case_id = row["case_id"]
        time.sleep(ovv.DELAY)
        try:
            html = ovv.fetch_page(client, row["detail_url"])
            d = ovv.parse_detail(html)
        except Exception as e:
            print(f"[ovv fetch] {case_id}: page failed: {e}", file=sys.stderr)
            continue

        base_meta = (d["title"], d["summary"], d["registration"],
                     d["event_date"])

        if not d["doc_urls"]:
            # ongoing / doc-less — keep metadata, stay 'new' (self-heal)
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, updated_at=? WHERE case_id=?",
                (*base_meta, db.now_ms(), case_id),
            )
            conn.commit()
            continue

        # Reports are tried before sections, and a section is only considered
        # when no report could be read at all. Ranking alone was not enough:
        # when the report is a scan with no text layer, a recommendations
        # chapter or an appendix would out-score it on length and become the
        # narrative — which is how sections got into 26 of the 386 built rows.
        # A scanned report belongs in OCR, not replaced by its appendix.
        ranked = d["doc_urls"][:_MAX_DOC_TRIES]
        reports = [u for u in ranked if not ovv.is_noise_doc(u)]
        sections = [u for u in ranked if ovv.is_noise_doc(u)]

        pdf_path = os.path.join(pdf_dir, f"{case_id[:60]}.pdf")
        text, used_url, lang, best_path = "", None, None, None
        got_a_document = False

        def _try(doc_urls, offset):
            """Download each in turn; keep the longest text. Returns state."""
            nonlocal text, used_url, lang, best_path, got_a_document
            for n, doc_url in enumerate(doc_urls):
                time.sleep(ovv.DELAY)
                # One file per candidate. A single shared path meant the OCR
                # fallback below could run against whichever document happened
                # to be written last rather than the one whose text we kept.
                idx = offset + n
                try_path = pdf_path if idx == 0 else f"{pdf_path[:-4]}-{idx}.pdf"
                try:
                    ovv.download_pdf(client, doc_url, try_path)
                    t = pdf.extract_text(try_path)
                except Exception as e:
                    print(f"[ovv fetch] {case_id}: doc failed: {e}", file=sys.stderr)
                    continue
                got_a_document = True
                if len(t) > len(text) or best_path is None:
                    text, used_url, lang = t, doc_url, ovv.doc_lang(doc_url)
                    best_path = try_path
                if len(t) >= _PDF_TEXT_FLOOR:
                    return True
            return False

        if not _try(reports, 0) and not got_a_document:
            # No report was readable — a section may still be a real report
            # with a misleading filename, so it is worth looking at.
            _try(sections, len(reports))

        if not got_a_document:
            # Every candidate download failed. This row has documents — we
            # simply could not reach them this time, so it is a transient
            # fault, not a report without a narrative. Keep the metadata and
            # stay 'new' so the next cycle retries it.
            #
            # The old code fell through and stamped 'parsed' with empty text;
            # build() then moved it to 'skipped', and fetch() only ever reads
            # 'new' — so a single timeout retired a report permanently and
            # without an error. Distinguishing "could not fetch" from "fetched
            # and unusable" is the whole fix.
            print(f"[ovv fetch] {case_id}: all {len(d['doc_urls'][:_MAX_DOC_TRIES])} "
                  f"document(s) failed — staying 'new' for the next cycle",
                  file=sys.stderr)
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, updated_at=? WHERE case_id=?",
                (*base_meta, db.now_ms(), case_id),
            )
            conn.commit()
            continue

        if used_url and ovv.is_noise_doc(used_url) and len(text) < _SECTION_MAX:
            # What we ended up with is a section of the investigation — a
            # recommendations chapter, a summary, a letter — rather than the
            # report of it. Ranking already pushes those last, but that cannot
            # help when a section is the only thing linked, which is common:
            # OVV publishes recommendations months before the report. 26 of the
            # 386 built rows held one, several of them Dutch names that were
            # already ranked last.
            #
            # The length test is what keeps this honest. Two of those 26 are
            # genuine 230,000-character OVV reports that merely have a section
            # word in the filename, and dropping them would be a worse error
            # than the one being fixed. A section is short; a report is not.
            #
            # Staying 'new' means a later cycle can still take the report.
            print(f"[ovv fetch] {case_id}: best document is a {len(text)}-char "
                  f"section ({used_url.rstrip('/').rsplit('/', 1)[-1][:50]}) "
                  f"— staying 'new'", file=sys.stderr)
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, updated_at=? WHERE case_id=?",
                (*base_meta, db.now_ms(), case_id),
            )
            conn.commit()
            continue

        tier = "pdf"
        if (len(text) < _NARRATIVE_FLOOR and enable_ocr
                and best_path and os.path.exists(best_path)):
            # OCR the document whose text we actually kept, not whichever file
            # was written last.
            ocr_text = pdf.ocr_extract(best_path, lang=OCR_LANG)
            if len(ocr_text) > len(text):
                text, tier = ocr_text, "ocr"
                if not used_url:
                    used_url = candidates[0]
                    lang = ovv.doc_lang(used_url)

        if len(text) < _NARRATIVE_FLOOR:
            summary = d["summary"] or ""
            if len(summary) > len(text):
                text, tier, lang = summary, "html", "en"
            else:
                tier = "scanned"

        try:
            conn.execute(
                "UPDATE ovv_reports SET title=?, summary=?, registration=?, "
                "date_of_occurrence=?, lang=?, narrative_text=?, "
                "source_tier=?, pdf_url=?, pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (*base_meta, lang, text, tier, used_url,
                 best_path if used_url else None,
                 db.STATUS_PARSED, db.now_ms(), case_id),
            )
            conn.commit()
        except Exception as e:
            print(f"[ovv fetch] {case_id}: db update failed: {e}",
                  file=sys.stderr)
    return len(rows)


def build(conn):
    """Promote 'parsed' rows with narrative >= floor into ovv_accidents."""
    rows = conn.execute(
        "SELECT case_id, detail_url, title, registration, "
        "date_of_occurrence, narrative_text FROM ovv_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()
    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE ovv_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(None, row["registration"], row["title"])
        conn.execute(
            "INSERT OR REPLACE INTO ovv_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                None,
                row["registration"],
                None,
                row["title"],
                "NL",
                narrative,
                None,
                row["detail_url"] or "https://onderzoeksraad.nl/",
                None,
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE ovv_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
