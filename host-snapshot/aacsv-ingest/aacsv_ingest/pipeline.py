# aacsv_ingest/pipeline.py
"""discover -> fetch -> parse -> build pipeline for El Salvador AAC.

discover(): GET the single WPDM listing, parse every report-package row, INSERT
  new slugs into aacsv_reports.  Idempotent (per-slug skip).

fetch(): for each status='new' row, download the PDF via its wpdmdl URL and
  advance to 'fetched'.  Per-row try/except keeps a failed row at 'new' for
  retry.

parse(): pdftotext extract.  tier='pdf' when text >= MIN_NARRATIVE (500),
  'short' when shorter but present, 'none' when empty (scanned PDFs such as
  informe-final-8 land in 'none' and are skipped downstream — the scanned gate).

build(): project aacsv_reports → aacsv_accidents (country 'SV'), with
  final-over-preliminary dedup per occurrence (registration+year key): a FINAL
  report supersedes a PRELIMINARY one for the same case base.  Rows whose
  narrative is below _NARRATIVE_FLOOR are skipped.
"""
import os
import re
import sys
import time

from . import aacsv, db, text
from .pdf import extract_text, MIN_NARRATIVE

_NARRATIVE_FLOOR = 80  # chars; below this a row is not a real report


def discover(conn, client, full=False):
    resp = client.get(aacsv.INDEX_URL)
    resp.raise_for_status()
    html_text = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.text

    rows = aacsv.parse_listing(html_text)
    inserted = 0
    for row in rows:
        slug = row["slug"]
        if conn.execute("SELECT 1 FROM aacsv_reports WHERE slug=?", (slug,)).fetchone():
            continue
        ts = db.now_ms()
        conn.execute(
            "INSERT INTO aacsv_reports "
            "(slug, case_id, report_url, pdf_url, title, event_class, "
            "registration, date_of_occurrence, report_type, lang, status, "
            "discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug,
                row["case_id"],
                row["report_url"],
                row["pdf_url"],
                row["title"],
                row["event_class"],
                row["registration"],
                row["date_of_occurrence"],
                row["report_type"],
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
        "SELECT slug, pdf_url FROM aacsv_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    for row in rows:
        slug = row["slug"]
        pdf_url = row["pdf_url"]
        pdf_path = None
        if pdf_url:
            dest = os.path.join(pdf_dir, slug.replace("/", "_") + ".pdf")
            try:
                time.sleep(aacsv.DELAY)
                aacsv.download(client, pdf_url, dest)
                pdf_path = dest
            except Exception as exc:
                print(f"[aacsv fetch] {slug}: download {exc}", file=sys.stderr)
                continue  # stay 'new' for retry
        try:
            conn.execute(
                "UPDATE aacsv_reports SET pdf_path=?, status=?, updated_at=? WHERE slug=?",
                (pdf_path, db.STATUS_FETCHED, db.now_ms(), slug),
            )
            conn.commit()
        except Exception as exc:
            print(f"[aacsv fetch] {slug}: db {exc}", file=sys.stderr)
    return len(rows)


def parse(conn):
    rows = conn.execute(
        "SELECT slug, pdf_path FROM aacsv_reports WHERE status=?",
        (db.STATUS_FETCHED,),
    ).fetchall()
    for row in rows:
        full_text = extract_text(row["pdf_path"]) if row["pdf_path"] else ""
        if len(full_text) >= MIN_NARRATIVE:
            narrative, tier = full_text, "pdf"
        elif full_text:
            narrative, tier = full_text, "short"
        else:
            narrative, tier = "", "none"  # scanned / empty → skipped at build
        conn.execute(
            "UPDATE aacsv_reports SET narrative_text=?, source_tier=?, status=?, updated_at=? WHERE slug=?",
            (narrative, tier, db.STATUS_PARSED, db.now_ms(), row["slug"]),
        )
        conn.commit()
    return len(rows)


def _occurrence_key(reg, event_date):
    """Comparison key uniting a FINAL and its PRELIMINARY for one airframe/year.

    Returns None (never deduped) unless both a registration key and a 4-digit
    year are present.
    """
    rk = aacsv._reg_key(reg)
    yr = (event_date or "")[:4]
    # Only dedup when BOTH a registration key and a year are known; otherwise a
    # year-less row (e.g. two different occurrences of the same airframe in
    # different years, neither carrying a parseable date) must NOT collapse.
    return f"{rk}|{yr}" if (rk and yr) else None


def build(conn):
    """Project parsed reports into aacsv_accidents with final-over-prelim dedup."""
    rows = conn.execute(
        "SELECT slug, case_id, event_class, registration, location, operator, "
        "date_of_occurrence, narrative_text, pdf_url, report_url, report_type "
        "FROM aacsv_reports WHERE status=?",
        (db.STATUS_PARSED,),
    ).fetchall()

    # Decide a winner per occurrence among candidate (qualifying) rows: a FINAL
    # beats a PRELIMINARY; otherwise the longer narrative wins.
    candidates = []
    for row in rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < _NARRATIVE_FLOOR:
            conn.execute(
                "UPDATE aacsv_reports SET status=?, updated_at=? WHERE slug=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["slug"]),
            )
            conn.commit()
            continue
        candidates.append(row)

    winners = {}  # occurrence_key -> chosen row
    for row in candidates:
        key = _occurrence_key(row["registration"], row["date_of_occurrence"])
        if key is None:
            # no reliable occurrence key → key on slug (never deduped)
            winners[f"slug:{row['slug']}"] = row
            continue
        cur = winners.get(key)
        if cur is None:
            winners[key] = row
            continue
        cur_final = (cur["report_type"] == "final")
        new_final = (row["report_type"] == "final")
        if new_final and not cur_final:
            winners[key] = row
        elif new_final == cur_final and len(row["narrative_text"] or "") > len(cur["narrative_text"] or ""):
            winners[key] = row

    chosen_slugs = {r["slug"] for r in winners.values()}
    built = 0
    for row in candidates:
        if row["slug"] not in chosen_slugs:
            # superseded by a final/longer sibling → skip projection
            conn.execute(
                "UPDATE aacsv_reports SET status=?, updated_at=? WHERE slug=?",
                (db.STATUS_SKIPPED, db.now_ms(), row["slug"]),
            )
            conn.commit()
            continue
        narrative = row["narrative_text"] or ""
        source_url = row["pdf_url"] or row["report_url"]
        site_slug = text.make_site_slug(None, row["registration"], row["location"])
        conn.execute(
            "INSERT OR REPLACE INTO aacsv_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            "narrative_text, probable_cause, source_url, report_type, site_slug, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["case_id"],
                row["date_of_occurrence"],
                None,
                row["registration"],
                row["operator"],
                row["location"],
                "SV",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                db.now_ms(),
            ),
        )
        conn.execute(
            "UPDATE aacsv_reports SET status=?, updated_at=? WHERE slug=?",
            (db.STATUS_BUILT, db.now_ms(), row["slug"]),
        )
        conn.commit()
        built += 1
    return built
