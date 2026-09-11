# aaiasb_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for AAIASB (Greece).

Transport: plain httpx GET (not bot-protected). parse() and build() are
network-free and operate only on the SQLite database.
"""
import os
import sys
import time

from . import aaiasb, db
from .pdf import extract_text, MIN_NARRATIVE
from .text import make_site_slug

_NARRATIVE_FLOOR = 80  # chars; rows below this are non-text/scanned PDFs


def discover(conn):
    """Walk the 4 listing pages, visit each detail page for PDF links,
    INSERT new case_ids into aaiasb_reports. Idempotent."""
    inserted = 0
    for start in aaiasb.STARTS:
        try:
            html = aaiasb.get_listing_html(start)
        except Exception as exc:
            print(f"[aaiasb discover] listing start={start}: {exc}", file=sys.stderr)
            continue
        time.sleep(aaiasb.DELAY)

        rows = aaiasb.parse_listing(html)
        if not rows:
            print(f"[aaiasb discover] listing start={start}: 0 rows", file=sys.stderr)
            continue

        for row in rows:
            case_id = row["case_id"]
            if not case_id:
                continue
            try:
                exists = conn.execute(
                    "SELECT 1 FROM aaiasb_reports WHERE case_id=?", (case_id,)
                ).fetchone()
                if exists:
                    continue

                # visit detail page for PDF links
                pdf_url_en = pdf_url_el = None
                try:
                    dhtml = aaiasb.get(row["report_url"]).text
                    time.sleep(aaiasb.DELAY)
                    pdf_url_en, pdf_url_el = aaiasb.parse_detail_pdfs(dhtml)
                except Exception as exc:
                    print(
                        f"[aaiasb discover] detail {case_id}: {exc}",
                        file=sys.stderr,
                    )

                chosen, lang = aaiasb.make_pdf_choice(pdf_url_en, pdf_url_el)

                conn.execute(
                    "INSERT INTO aaiasb_reports "
                    "(case_id, report_url, pdf_url, pdf_url_en, pdf_url_el, lang, "
                    " report_type, aircraft, registration, operator, "
                    " date_of_occurrence, location, status, discovered_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        case_id,
                        row["report_url"],
                        chosen,
                        pdf_url_en,
                        pdf_url_el,
                        lang,
                        row["report_type"],
                        row["aircraft"],
                        row["registration"],
                        None,
                        row["date_of_occurrence"],
                        row["location"],
                        db.STATUS_NEW,
                        db.now_ms(),
                        db.now_ms(),
                    ),
                )
                conn.commit()
                inserted += 1
            except Exception as exc:
                print(
                    f"[aaiasb discover] row {case_id}: {exc}", file=sys.stderr
                )
    return inserted


def fetch(conn, pdf_dir):
    """Download chosen PDFs for status='new' rows -> 'fetched'."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url, pdf_url_en, pdf_url_el "
        "FROM aaiasb_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    processed = 0
    for row in rows:
        case_id = row["case_id"]
        candidates = []
        for u in (row["pdf_url"], row["pdf_url_en"], row["pdf_url_el"]):
            if u and u not in candidates:
                candidates.append(u)

        pdf_path = None
        if candidates:
            safe = case_id.replace("/", "_").replace(" ", "_")
            dest = os.path.join(pdf_dir, safe + ".pdf")
            last_exc = None
            for u in candidates:
                try:
                    time.sleep(aaiasb.DELAY)
                    r = aaiasb.get(u)
                    ct = r.headers.get("content-type", "")
                    if "pdf" not in ct.lower() and not r.content[:4] == b"%PDF":
                        raise ValueError(f"not a pdf (ct={ct})")
                    with open(dest, "wb") as fh:
                        fh.write(r.content)
                    pdf_path = dest
                    break
                except Exception as exc:
                    last_exc = exc
            if pdf_path is None:
                print(f"[aaiasb fetch] {case_id}: {last_exc}", file=sys.stderr)
                continue  # leave 'new' for retry

        try:
            conn.execute(
                "UPDATE aaiasb_reports SET pdf_path=?, status=?, updated_at=? "
                "WHERE case_id=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), case_id),
            )
            conn.commit()
            processed += 1
        except Exception as exc:
            print(f"[aaiasb fetch] {case_id}: db {exc}", file=sys.stderr)
    return processed


def parse(conn):
    """pdftotext extraction for fetched rows -> 'parsed'."""
    rows = conn.execute(
        "SELECT case_id, pdf_path FROM aaiasb_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()

    for row in rows:
        pdf_path = row["pdf_path"]
        full_text = extract_text(pdf_path) if pdf_path else ""

        if len(full_text) >= MIN_NARRATIVE:
            tier = "pdf"
        elif len(full_text) >= _NARRATIVE_FLOOR:
            tier = "pdf"  # real text, shorter than preferred
        elif pdf_path:
            tier = "scanned"
        else:
            tier = "none"

        conn.execute(
            "UPDATE aaiasb_reports "
            "SET narrative_text=?, source_tier=?, status=?, updated_at=? "
            "WHERE case_id=?",
            (full_text, tier, db.STATUS_PARSED, db.now_ms(), row["case_id"]),
        )
        conn.commit()
    return len(rows)


def build(conn):
    """Emit aaiasb_accidents for parsed rows with tier='pdf' and
    narrative >= 80 chars. Everything else -> 'skipped'."""
    rows = conn.execute(
        "SELECT case_id, report_type, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, source_tier, "
        "report_url, lang "
        "FROM aaiasb_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row["narrative_text"] or ""
        tier = row["source_tier"] or ""

        if tier != "pdf" or len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["case_id"]),
            )
            conn.commit()
            continue

        site_slug = make_site_slug(
            row["aircraft"], row["registration"], row["location"]
        )
        conn.execute(
            "INSERT OR REPLACE INTO aaiasb_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, "
            "country, narrative_text, probable_cause, source_url, report_type, "
            "site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                row["aircraft"],
                row["registration"],
                row["operator"],
                row["location"],
                "GR",
                narrative,
                None,  # probable_cause not extracted for aaiasb
                row["report_url"],
                row["report_type"],
                site_slug,
                row["lang"],
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aaiasb_reports SET status=?, updated_at=? WHERE case_id=?",
            (db.STATUS_BUILT, db.now_ms(), row["case_id"]),
        )
        conn.commit()
        built += 1
    return built
