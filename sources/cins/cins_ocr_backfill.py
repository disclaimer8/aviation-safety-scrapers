#!/usr/bin/env python3
"""Standalone backfill script for CINS Serbia scanned PDFs.
Runs OCR via hetzner remote, updates cins.db, then re-runs build.
Usage: python3 cins_ocr_backfill.py
"""
import os, sys, subprocess, shlex, uuid, sqlite3, time, re

DB = os.path.expanduser("~/cins-ingest/cins.db")
OCR_REMOTE = os.environ.get("OCR_REMOTE", "root@136.243.144.209")
OCR_LANG = "rus"  # hetzner has rus (Cyrillic) but not srp
FLOOR = 600
MIN_MARKERS = 2

MARKERS = (
    "извештај", "ваздухоплов", "удес", "нез", "саобраћај", "авион",
    "република", "комисија", "регист", "аеродром",
    "izveštaj", "izvestaj", "vazduhoplov", "udes", "nezgod",
    "saobraćaj", "saobracaj", "avion", "komisija", "registar", "aerodrom",
    "report", "aircraft", "accident", "incident", "investigation",
    "registration", "airport",
)

def now_ms():
    return int(time.time() * 1000)

def count_markers(text):
    if not text: return 0
    low = text.lower()
    return sum(1 for m in MARKERS if m in low)

def is_usable(text):
    if not text or not text.strip(): return False
    return count_markers(text) >= MIN_MARKERS

def ocr_remote(pdf_path, lang, host):
    remote = "/tmp/cins-ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print("[cins-ocr] scp failed for %s" % pdf_path)
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        print("[cins-ocr] error for %s: %s" % (pdf_path, e))
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except: pass
        return ""

def detect_lang(text):
    if any(0x400 <= ord(ch) <= 0x4ff for ch in (text or "")):
        return "sr"
    return "en"

def make_slug(aircraft, registration, location):
    parts = []
    for s in [aircraft, registration, location]:
        if s:
            parts.append(re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-'))
    return '-'.join(p for p in parts if p)[:120]

def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    
    rows = c.execute(
        "SELECT case_id, pdf_path, source_tier FROM cins_reports WHERE status='skipped'"
    ).fetchall()
    
    print("[cins-ocr] Found %d skipped rows" % len(rows))
    
    built = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        tier = row["source_tier"]
        
        if not pdf_path or not os.path.exists(pdf_path):
            print("[cins-ocr] %s: pdf missing at %s" % (cid, pdf_path))
            continue
        
        print("[cins-ocr] OCR-ing %s (tier=%s)..." % (cid, tier), flush=True)
        txt = ocr_remote(pdf_path, OCR_LANG, OCR_REMOTE)
        
        if len(txt) < FLOOR:
            print("[cins-ocr] %s: short after OCR (%d chars)" % (cid, len(txt)))
            continue
        
        if not is_usable(txt):
            print("[cins-ocr] %s: not usable (markers=%d)" % (cid, count_markers(txt)))
            continue
        
        lang = detect_lang(txt)
        print("[cins-ocr] %s: OK chars=%d lang=%s markers=%d" % (cid, len(txt), lang, count_markers(txt)))
        
        # Update cins_reports to 'parsed' with OCR text
        c.execute(
            "UPDATE cins_reports SET narrative_text=?, source_tier='ocr', lang=?, status='parsed', updated_at=? WHERE case_id=?",
            (txt, lang, now_ms(), cid)
        )
        c.commit()
    
    # Now run build for parsed rows
    parsed_rows = c.execute(
        "SELECT case_id, event_class, aircraft, registration, operator, location, "
        "date_of_occurrence, narrative_text, pdf_url, report_url "
        "FROM cins_reports WHERE status='parsed'"
    ).fetchall()
    
    print("[cins-ocr] Building %d parsed rows..." % len(parsed_rows))
    for row in parsed_rows:
        narrative = row["narrative_text"] or ""
        if len(narrative) < 80:
            c.execute("UPDATE cins_reports SET status='skipped', updated_at=? WHERE case_id=?",
                      (now_ms(), row["case_id"]))
            c.commit()
            continue
        
        source_url = row["pdf_url"] or row["report_url"]
        site_slug = make_slug(row["aircraft"], row["registration"], row["location"])
        
        c.execute(
            "INSERT OR REPLACE INTO cins_accidents "
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
                "RS",
                narrative,
                None,
                source_url,
                row["event_class"],
                site_slug,
                now_ms(),
            )
        )
        c.execute("UPDATE cins_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now_ms(), row["case_id"]))
        c.commit()
        print("[cins-ocr] built %s" % row["case_id"])
        built += 1
    
    print("[cins-ocr] Done! built=%d" % built)
    status = c.execute("SELECT status, COUNT(*) FROM cins_reports GROUP BY status").fetchall()
    print("status:", [(r[0], r[1]) for r in status])
    n = c.execute("SELECT COUNT(*) FROM cins_accidents").fetchone()[0]
    print("cins_accidents total:", n)

if __name__ == "__main__":
    main()
