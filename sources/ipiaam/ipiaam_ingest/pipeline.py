# ipiaam_ingest/pipeline.py
"""
discover -> fetch -> parse -> build pipeline for IPIAAM Cabo Verde.

discover(): GET the IPIAAM listing page, parse all PDF links and INSERT new
  case_ids into ipiaam_reports.  Each entry stores report_ref, pdf_url,
  pub_date, lang (always 'en' initially).  Idempotent.

fetch(): for each status='new' row, download the PDF and advance to 'fetched'.
  Per-row try/except keeps failed rows at 'new' for retry.

parse(): extract text via pdftotext for 'fetched' rows.
    text length <  _SCANNED_FLOOR (500) -> tier 'scanned'
    text length >= MIN_NARRATIVE (600)  -> tier 'pdf'
    otherwise                           -> tier 'short'
  Extracts event_date, aircraft, registration, operator, fatalities_total
  from the PDF cover block.  pub_date from listing is only a fallback for
  event_date.

build(): emit ipiaam_accidents rows (country='CV').  Rows with narrative
  shorter than _NARRATIVE_FLOOR (300) are skipped.
"""
import os
import re
import sys
import time
import urllib.parse

from . import ipiaam as src, db
from .pdf import extract_text, MIN_NARRATIVE

_SCANNED_FLOOR = 500
_NARRATIVE_FLOOR = 300


def discover(conn, client, full=False):
    """GET the IPIAAM listing and insert new reports into ipiaam_reports."""
    print('[ipiaam discover] fetching listing …', flush=True)
    resp = client.get(src.LISTING_URL)
    resp.raise_for_status()
    html = resp.content.decode('utf-8', 'replace') if isinstance(resp.content, bytes) else resp.text

    rows = src.parse_listing(html)

    if not rows:

        # 8 rows from this source are already in production, so an

        # empty listing is the markup changing — not the authority

        # publishing nothing. Returning 0 here is indistinguishable

        # from a clean run, which is how a dead scraper stays quiet.

        raise RuntimeError(

            "[ipiaam discover] listing parsed to zero rows. The markup has"

            " probably changed; refusing to report an empty run as success."

        )

    print(f'[ipiaam discover] found {len(rows)} PDFs on listing', flush=True)

    inserted = 0
    for row in rows:
        case_id = row['case_id']
        if conn.execute(
            'SELECT 1 FROM ipiaam_reports WHERE case_id=?', (case_id,)
        ).fetchone():
            print(f'[ipiaam discover] skip existing {case_id}', flush=True)
            continue
        ts = db.now_ms()
        conn.execute(
            'INSERT INTO ipiaam_reports '
            '(case_id, report_ref, pdf_url, pub_date, lang, status, discovered_at, updated_at) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (
                case_id,
                row.get('report_ref'),
                row.get('pdf_url'),
                row.get('pub_date'),
                'en',
                db.STATUS_NEW,
                ts,
                ts,
            ),
        )
        print(f'[ipiaam discover] inserted {case_id}', flush=True)
        inserted += 1
    conn.commit()
    return inserted


def fetch(conn, client, pdf_dir):
    """Download PDFs for status='new' rows."""
    os.makedirs(pdf_dir, exist_ok=True)
    rows = conn.execute(
        "SELECT case_id, pdf_url FROM ipiaam_reports WHERE status=?",
        (db.STATUS_NEW,),
    ).fetchall()

    fetched = 0
    for row in rows:
        case_id = row['case_id']
        pdf_url = row['pdf_url']
        dest = os.path.join(pdf_dir, case_id + '.pdf')

        try:
            print(f'[ipiaam fetch] downloading {case_id} …', flush=True)
            time.sleep(src.DELAY)
            # Percent-encode spaces/special chars in path.
            parts = urllib.parse.urlsplit(pdf_url)
            safe_path = urllib.parse.quote(parts.path, safe='/%')
            safe_url = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
            )
            resp = client.get(safe_url, headers={'Referer': src.REFERER})
            resp.raise_for_status()
            with open(dest, 'wb') as fh:
                fh.write(resp.content)
            size = len(resp.content)
            print(f'[ipiaam fetch] {case_id}: {size} bytes', flush=True)
        except Exception as exc:
            print(f'[ipiaam fetch] {case_id}: FAILED {exc}', file=sys.stderr, flush=True)
            continue

        conn.execute(
            'UPDATE ipiaam_reports SET pdf_path=?, status=?, updated_at=? WHERE case_id=?',
            (dest, db.STATUS_FETCHED, db.now_ms(), case_id),
        )
        conn.commit()
        fetched += 1

    return fetched


def parse(conn):
    """Extract text and metadata from fetched PDFs."""
    rows = conn.execute(
        'SELECT case_id, pdf_path, pub_date FROM ipiaam_reports WHERE status=?',
        (db.STATUS_FETCHED,),
    ).fetchall()

    processed = 0
    for row in rows:
        pdf_path = row['pdf_path']
        full_text = extract_text(pdf_path) if pdf_path else ''
        tlen = len(full_text)

        if not full_text:
            tier = 'none'
        elif tlen < _SCANNED_FLOOR:
            tier = 'scanned'
        elif tlen >= MIN_NARRATIVE:
            tier = 'pdf'
        else:
            tier = 'short'

        # Metadata from PDF cover block.
        event_date = src.extract_event_date(full_text) or row['pub_date']
        registration = src.extract_registration(full_text)
        aircraft = src.extract_aircraft(full_text)
        operator = src.extract_operator(full_text)
        fatalities = src.extract_fatalities(full_text)
        lang = src.detect_lang(full_text)

        # If the listing had no report ref (fallback timestamp), try to extract
        # it from the PDF text so the report_ref column is accurate.
        existing_ref = conn.execute(
            'SELECT report_ref FROM ipiaam_reports WHERE case_id=?', (row['case_id'],)
        ).fetchone()['report_ref']
        # Timestamp-based refs look like pure digits (no slash).
        if existing_ref and '/' not in existing_ref:
            pdf_ref = src.extract_report_ref_from_pdf(full_text)
            if pdf_ref:
                existing_ref = pdf_ref
                conn.execute(
                    'UPDATE ipiaam_reports SET report_ref=?, updated_at=? WHERE case_id=?',
                    (pdf_ref, db.now_ms(), row['case_id']),
                )

        print(
            f'[ipiaam parse] {row["case_id"]}: tier={tier} len={tlen} '
            f'date={event_date} reg={registration} lang={lang} ref={existing_ref}',
            flush=True,
        )

        conn.execute(
            'UPDATE ipiaam_reports '
            'SET narrative_text=?, source_tier=?, date_of_occurrence=?, '
            'aircraft=?, registration=?, operator=?, fatalities_total=?, '
            'lang=?, status=?, updated_at=? WHERE case_id=?',
            (
                full_text, tier, event_date,
                aircraft, registration, operator, fatalities,
                lang, db.STATUS_PARSED, db.now_ms(), row['case_id'],
            ),
        )
        conn.commit()
        processed += 1

    return processed


def build(conn):
    """Emit ipiaam_accidents rows for status='parsed' rows."""
    rows = conn.execute(
        'SELECT case_id, report_ref, aircraft, registration, operator, location, '
        'date_of_occurrence, narrative_text, pdf_url, fatalities_total, lang '
        'FROM ipiaam_reports WHERE status=?',
        (db.STATUS_PARSED,),
    ).fetchall()

    built = 0
    for row in rows:
        narrative = row['narrative_text'] or ''
        if len(narrative) < _NARRATIVE_FLOOR:
            print(
                f'[ipiaam build] skip {row["case_id"]}: narrative only {len(narrative)} chars',
                flush=True,
            )
            conn.execute(
                'UPDATE ipiaam_reports SET status=?, updated_at=? WHERE case_id=?',
                (db.STATUS_SKIPPED, db.now_ms(), row['case_id']),
            )
            conn.commit()
            continue

        # Determine report_type from report_ref: INCID-A = Serious Incident,
        # INCID = Incident, ACID = Accident.
        # Also check narrative text for "Serious Incident" / "Incidente Grave"
        # phrasing when the ref alone is ambiguous (timestamp fallback IDs).
        ref = (row['report_ref'] or '').upper()
        if 'ACID' in ref:
            report_type = 'Accident'
        elif 'INCID-A' in ref:
            report_type = 'Serious Incident'
        elif re.search(
            r'(?:Serious\s+Incident|Incidente\s+Grave)',
            narrative[:2000], re.IGNORECASE,
        ):
            report_type = 'Serious Incident'
        else:
            report_type = 'Incident'

        site_slug = src.make_site_slug(
            row['aircraft'], row['registration'], row['location']
        )

        conn.execute(
            'INSERT OR REPLACE INTO ipiaam_accidents '
            '(case_id, event_date, aircraft, registration, operator, location, country, '
            'narrative_text, probable_cause, source_url, report_type, site_slug, '
            'fatalities_total, built_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                row['case_id'],
                row['date_of_occurrence'],
                row['aircraft'],
                row['registration'],
                row['operator'],
                row['location'],
                'CV',
                narrative,
                None,
                row['pdf_url'],
                report_type,
                site_slug,
                row['fatalities_total'],
                db.now_ms(),
            ),
        )
        conn.execute(
            'UPDATE ipiaam_reports SET status=?, updated_at=? WHERE case_id=?',
            (db.STATUS_BUILT, db.now_ms(), row['case_id']),
        )
        conn.commit()
        print(f'[ipiaam build] built {row["case_id"]} ({report_type})', flush=True)
        built += 1

    return built
