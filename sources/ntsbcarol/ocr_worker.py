#!/usr/bin/env python3
"""OCR worker for ntsbcarol-ingest scanned PDFs.

Reads ~/ntsbcarol-ingest/ocr-queue.json and processes each case:
1. Ships PDF to OCR_REMOTE (<ocr-host>), runs ocrmypdf + pdftotext
2. Updates ntsbcarol_cases narrative_text, source_tier to 'ocr'
3. Resets status to 'parsed' for build() to pick up
4. Calls build() on each row inline

Progress saved to ~/ntsbcarol-ingest/ocr-progress.json (resumable).
Run as: OCR_REMOTE=<ocr-host> python3 ~/ntsbcarol-ingest/ocr_worker.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

DB_PATH = '~/ntsbcarol-ingest/ntsbcarol.db'
QUEUE_PATH = '~/ntsbcarol-ingest/ocr-queue.json'
PROGRESS_PATH = '~/ntsbcarol-ingest/ocr-progress.json'
LOG_PATH = '~/ntsbcarol-ingest/ocr.log'
MIN_NARRATIVE = 500
DELAY = 2.0

OCR_REMOTE = os.environ.get('OCR_REMOTE')
if not OCR_REMOTE:
    print('ERROR: OCR_REMOTE env var not set (e.g. <ocr-host>)', file=sys.stderr)
    sys.exit(1)


def log(msg):
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')


def ocr_pdf(pdf_path):
    """Ship PDF to remote, OCR, fetch text. Returns (text, tier)."""
    bn = os.path.basename(pdf_path)
    remote_pdf = f'/tmp/ntsbcarol_ocr_{bn}'
    remote_out = remote_pdf.replace('.pdf', '_ocr.pdf')
    remote_txt = remote_pdf.replace('.pdf', '.txt')
    try:
        # scp to remote
        subprocess.run(['scp', '-q', pdf_path, f'{OCR_REMOTE}:{remote_pdf}'],
                       check=True, timeout=120)
        # OCR on remote (runs as root, hetzner has no a1 user)
        cmd = (
            f'nice -n 19 ionice -c 3 '
            f'ocrmypdf --rotate-pages -l eng --force-ocr '
            f'{remote_pdf} {remote_out} 2>/dev/null && '
            f'pdftotext -layout {remote_out} {remote_txt}'
        )
        subprocess.run(['ssh', OCR_REMOTE, cmd], check=True, timeout=300)
        # Get text
        result = subprocess.run(
            ['ssh', OCR_REMOTE, f'cat {remote_txt}'],
            capture_output=True, timeout=60, check=True
        )
        text = result.stdout.decode('utf-8', 'replace')
        # Cleanup
        subprocess.run(
            ['ssh', OCR_REMOTE, f'rm -f {remote_pdf} {remote_out} {remote_txt}'],
            timeout=30
        )
        return text, 'ocr'
    except Exception as e:
        log(f'OCR ERROR {pdf_path}: {e}')
        return '', 'none'


def load_progress():
    if os.path.exists(PROGRESS_PATH):
        with open(PROGRESS_PATH) as f:
            return set(json.load(f).get('done', []))
    return set()


def save_progress(done):
    with open(PROGRESS_PATH, 'w') as f:
        json.dump({'done': list(done)}, f)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_one(conn, case_id):
    """Build a single case into ntsbcarol_accidents."""
    row = conn.execute('''
        SELECT case_id, ntsb_no, event_date, city, state, country,
               registration, make, model, fatalities_total,
               narrative_text, probable_cause, source_tier
        FROM ntsbcarol_cases WHERE case_id=?
    ''', (case_id,)).fetchone()
    if not row:
        return False

    narrative = row['narrative_text'] or ''
    if len(narrative) < MIN_NARRATIVE:
        conn.execute("UPDATE ntsbcarol_cases SET status='skipped' WHERE case_id=?", (case_id,))
        conn.commit()
        return False

    parts = [p for p in [row['city'], row['state']] if p]
    location = ', '.join(parts) if parts else None
    aircraft_parts = [p for p in [row['make'], row['model']] if p]
    aircraft = ' '.join(aircraft_parts) if aircraft_parts else None
    site_slug = row['ntsb_no'].lower()
    source_url = f"https://carol.ntsb.gov/event/{row['ntsb_no']}"

    ts = int(time.time() * 1000)
    conn.execute('''
        INSERT OR REPLACE INTO ntsbcarol_accidents
        (case_id, event_date, aircraft, registration, operator,
         location, country, narrative_text, probable_cause,
         source_url, report_type, site_slug,
         fatalities_total, phase, category, lang, built_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ''', (
        row['case_id'], row['event_date'], aircraft,
        row['registration'], None, location,
        row['country'] or 'US', narrative[:12000],
        row['probable_cause'],
        source_url, 'final', site_slug,
        row['fatalities_total'], row['phase'] if hasattr(row, 'phase') else None,
        None, 'en', ts
    ))
    conn.execute("UPDATE ntsbcarol_cases SET status='built' WHERE case_id=?", (case_id,))
    conn.commit()
    return True


def main():
    queue = json.load(open(QUEUE_PATH))
    done = load_progress()
    log(f'Queue: {len(queue)} cases, {len(done)} already done')

    conn = connect()
    built = 0
    skipped = 0

    for i, item in enumerate(queue):
        case_id = item['case_id']
        if case_id in done:
            continue

        pdf_path = item['pdf_path']
        log(f'[{i+1}/{len(queue)}] OCR {case_id} ({pdf_path})')

        text, tier = ocr_pdf(pdf_path)

        # Extract probable cause
        pc = ''
        for header in ['PROBABLE CAUSE', 'Probable Cause']:
            idx = text.find(header)
            if idx >= 0:
                after = text[idx + len(header):].lstrip(' :\n\r')
                cutoffs = ['\n\n\n', 'FINDINGS', 'SAFETY RECOMMENDATIONS']
                end = min(3000, len(after))
                for c in cutoffs:
                    ci = after.find(c)
                    if 0 < ci < end:
                        end = ci
                pc = after[:end].strip()
                if len(pc) > 50:
                    break

        # Update DB
        conn.execute('''
            UPDATE ntsbcarol_cases
            SET narrative_text=?, probable_cause=?, source_tier=?, status='parsed'
            WHERE case_id=?
        ''', (text[:12000], pc or None, tier, case_id))
        conn.commit()

        # Build
        if build_one(conn, case_id):
            log(f'  built ({len(text)} chars, tier={tier}, pc={len(pc)}ch)')
            built += 1
        else:
            log(f'  skipped (text too short: {len(text)}ch)')
            skipped += 1

        done.add(case_id)
        save_progress(done)
        time.sleep(DELAY)

    conn.close()
    log(f'OCR complete: built={built} skipped={skipped}')
    print(f'\nFINAL: built={built} skipped={skipped} total_done={len(done)}')


if __name__ == '__main__':
    main()
