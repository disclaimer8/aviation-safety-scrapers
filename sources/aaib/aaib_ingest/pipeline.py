# aaib_ingest/pipeline.py
import os
import sys

from . import db, govuk, text
from .pdf import extract_text, MIN_NARRATIVE

_KNOWN_TOLERANCE = 5  # delta: stop after this many consecutive already-seen slugs


def discover(conn, client, full=False, page_size=100):
    inserted = 0
    consecutive_known = 0
    for r in govuk.iter_search(client, page_size=page_size):
        slug = govuk.slug_from_link(r.get("link", ""))
        if not slug:
            continue
        if conn.execute("SELECT 1 FROM aaib_reports WHERE slug=?", (slug,)).fetchone():
            if not full:
                consecutive_known += 1
                if consecutive_known >= _KNOWN_TOLERANCE:
                    break
            continue
        consecutive_known = 0
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aaib_reports (slug, title, public_timestamp, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (slug, r.get("title"), r.get("public_timestamp"), db.STATUS_NEW, ts, ts),
        )
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute("SELECT slug FROM aaib_reports WHERE status=?", (db.STATUS_NEW,)).fetchall()
    for row in rows:
        slug = row["slug"]
        # Content/metadata fetch — if THIS fails, skip the report (stays 'new', retried next run).
        try:
            content = govuk.get_content(client, slug)
        except Exception as e:
            print(f"[aaib fetch] {slug}: content {e}", file=sys.stderr)
            continue
        det = content.get("details", {}) or {}
        meta = det.get("metadata", {}) or {}
        body_text = text.strip_html(det.get("body", ""))
        cat = meta.get("aircraft_category")
        cat = cat[0] if isinstance(cat, list) and cat else (cat or None)
        att = govuk.pick_main_pdf(det.get("attachments", []))
        pdf_url = att.get("url") if att else None
        # PDF download is best-effort — a failure (404, redirect, timeout) must NOT lose the
        # report; keep the Tier-1 body and let parse() fall back to it.
        pdf_path = None
        if pdf_url:
            try:
                candidate = os.path.join(pdf_dir, slug + ".pdf")
                govuk.download(client, pdf_url, candidate)
                pdf_path = candidate
            except Exception as e:
                print(f"[aaib fetch] {slug}: pdf {e}", file=sys.stderr)
        try:
            conn.execute(
                "UPDATE aaib_reports SET report_type=?, aircraft_category=?, aircraft_type=?, registration=?, "
                "location=?, date_of_occurrence=?, pdf_url=?, pdf_path=?, body_text=?, status=?, updated_at=? "
                "WHERE slug=?",
                (meta.get("report_type"), cat, meta.get("aircraft_type"), meta.get("registration"),
                 meta.get("location"), meta.get("date_of_occurrence"), pdf_url, pdf_path, body_text,
                 db.STATUS_FETCHED, db.now_ms(), slug),
            )
            conn.commit()
        except Exception as e:
            print(f"[aaib fetch] {slug}: db {e}", file=sys.stderr)
            continue
    return len(rows)


def parse(conn):
    rows = conn.execute(
        "SELECT slug, pdf_path, body_text FROM aaib_reports WHERE status=?", (db.STATUS_FETCHED,)
    ).fetchall()
    for row in rows:
        full_text = extract_text(row["pdf_path"]) if row["pdf_path"] else ""
        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        else:
            narrative, tier = (row["body_text"] or ""), "body"
        conn.execute(
            "UPDATE aaib_reports SET narrative_text=?, source_tier=?, status=?, updated_at=? WHERE slug=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["slug"]),
        )
        conn.commit()
    return len(rows)


def build(conn):
    rows = conn.execute(
        "SELECT slug, aircraft_type, registration, location, date_of_occurrence, narrative_text, report_type "
        "FROM aaib_reports WHERE status=?", (db.STATUS_PARSED,)
    ).fetchall()
    built = 0
    for row in rows:
        if not row["registration"] and not row["aircraft_type"]:
            conn.execute("UPDATE aaib_reports SET status=?, updated_at=? WHERE slug=?",
                         (db.STATUS_SKIPPED, db.now_ms(), row["slug"]))
            conn.commit()
            continue
        site_slug = text.make_site_slug(row["aircraft_type"], row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO aaib_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, narrative_text, "
            "probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["slug"], row["date_of_occurrence"], row["aircraft_type"], row["registration"], None,
             row["location"], "GB", row["narrative_text"], None,
             f"https://www.gov.uk/aaib-reports/{row['slug']}", row["report_type"], site_slug, db.now_ms()),
        )
        conn.execute("UPDATE aaib_reports SET status=?, updated_at=? WHERE slug=?",
                     (db.STATUS_BUILT, db.now_ms(), row["slug"]))
        conn.commit()
        built += 1
    return built
