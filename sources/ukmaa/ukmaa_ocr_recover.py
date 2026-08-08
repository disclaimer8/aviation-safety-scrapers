#!/usr/bin/env python3
"""OCR recovery script for ukmaa stubs (narrative < 300 chars).

Run from minipc as:
  env OCR_REMOTE=<ocr-host> ~/aaid-ingest/.venv/bin/python3 ~/ukmaa-ingest/ukmaa_ocr_recover.py

Handles encrypted PDFs via qpdf --decrypt on the remote host before ocrmypdf.
"""

import sys, os, re, time, sqlite3, subprocess, uuid, shlex, json

HOME   = os.path.expanduser("~/ukmaa-ingest")
DB     = os.path.join(HOME, "ukmaa.db")
PDFDIR = os.path.join(HOME, "pdfs")
MIN_NARRATIVE = 300
FLOOR  = 80

def now():
    return int(time.time() * 1000)

def db_connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.commit()
    return c

# ─── OCR with decrypt ─────────────────────────────────────────────────────────

def _ocr_remote(pdf_path, host):
    remote = f"/tmp/ocr-{uuid.uuid4().hex}.pdf"
    remote_dec = remote.replace(".pdf", "-dec.pdf")
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), f"{host}:{remote}"],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print(f"  scp failed rc={cp.returncode}: {cp.stderr.decode()}", flush=True)
            return ""
        # Decrypt first (qpdf --decrypt is a no-op if not encrypted), then OCR
        cmd = (
            # Decrypt with qpdf; if it fails, use original
            f'qpdf --decrypt {shlex.quote(remote)} {shlex.quote(remote_dec)} 2>/dev/null '
            f'&& SRC={shlex.quote(remote_dec)} '
            f'|| SRC={shlex.quote(remote)}; '
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language eng '
            f'--sidecar "$f" --output-type none "$SRC" - >/dev/null 2>&1; '
            f'cat "$f"; rm -f "$f" {shlex.quote(remote)} {shlex.quote(remote_dec)}'
        )
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"  OCR error: {e}", flush=True)
        try:
            subprocess.run(["ssh", host, f"rm -f {shlex.quote(remote)} {shlex.quote(remote_dec)}"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""

def ocr_extract(pdf_path):
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, host)
    import tempfile
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        subprocess.run(
            ["ocrmypdf", "--force-ocr", "--language", "eng",
             "--sidecar", sidecar, "--output-type", "none",
             str(pdf_path), "-"],
            capture_output=True, timeout=600,
        )
        with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except Exception:
        return ""
    finally:
        try: os.unlink(sidecar)
        except OSError: pass

# ─── Metadata helpers ─────────────────────────────────────────────────────────

_SERIAL_TITLE_RE = re.compile(r'\b([A-Z]{2}\d{3,4}(?:/[A-Z]{2}\d{3,4})?)\b')
_DATE_4Y_RE = re.compile(
    r'\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})\b', re.I)
_DATE_2Y_RE = re.compile(
    r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})\b', re.I)
_SLUG_DATE_RE = re.compile(
    r'on-(\d{1,2})-(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)-(\d{2,4})', re.I)
_MONTH_MAP = {
    "jan":1,"january":1,"feb":2,"february":2,"mar":3,"march":3,
    "apr":4,"april":4,"may":5,"jun":6,"june":6,"jul":7,"july":7,
    "aug":8,"august":8,"sep":9,"september":9,"oct":10,"october":10,
    "nov":11,"november":11,"dec":12,"december":12,
}

def _2digit_year(yy):
    yy = int(yy)
    return 2000 + yy if yy <= 30 else 1990 + yy

def parse_date_from_text(txt):
    if not txt: return None
    m = _DATE_4Y_RE.search(txt)
    if m:
        d, ms, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        mo = _MONTH_MAP.get(ms)
        if mo and 1 <= d <= 31 and 2000 <= y <= 2030:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _DATE_2Y_RE.search(txt)
    if m:
        d, ms, yy = int(m.group(1)), m.group(2).lower()[:3], m.group(3)
        mo = _MONTH_MAP.get(ms); y = _2digit_year(yy)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _SLUG_DATE_RE.search(txt)
    if m:
        d, ms, yr = int(m.group(1)), m.group(2).lower()[:3], m.group(3)
        mo = _MONTH_MAP.get(ms)
        y = _2digit_year(yr) if len(yr) == 2 else int(yr)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def parse_registration(txt):
    if not txt: return None
    m = _SERIAL_TITLE_RE.search(txt)
    if m: return m.group(1)
    m2 = re.search(r'\b(G-[A-Z]{4})\b', txt)
    if m2: return m2.group(1)
    return None

def parse_aircraft_from_title(title):
    if not title: return None
    TYPES = [
        r'F-35B Lightning', r'F-35B', r'Hawk T Mk1A', r'Hawk TMk1A', r'Hawk T Mk1', r'Hawk TMk1',
        r'Hawk T\s+Mk\s*1[A-Z]?', r'Hawk',
        r'Tornado GR4', r'Tornado GR Mk4', r'Tornado GR4A', r'Tornado GR', r'Tornado F3', r'Tornado',
        r'Chinook ZA\d+', r'Chinook', r'Puma HC MK 2', r'Puma HC Mk 2', r'Puma HC2', r'Puma',
        r'Lynx Mk 9', r'Lynx', r'Sea King', r'Gazelle',
        r'Tucano ZF\d+', r'Tucano', r'Merlin', r'Apache',
        r'Watchkeeper WK\d+', r'Watchkeeper',
        r'Hercules C-130J Mk4', r'Hercules C-130', r'Hercules XV\d+', r'Hercules',
        r'Griffin MK1', r'Griffin', r'Squirrel HT1', r'Squirrel', r'Voyager',
        r'YAK52', r'Yak.52', r'Tutor G-BY\w+', r'Tutor', r'Typhoon', r'Nimrod', r'Tristar',
        r'Unmanned Air System \(UAS\) Hermes 450', r'Hermes 450',
    ]
    for pat in TYPES:
        m = re.search(r'\b' + pat + r'\b', title, re.I)
        if m: return m.group(0).strip()
    m = re.search(
        r'involving\s+(?:a\s+|an\s+|the\s+)?([A-Z][A-Za-z0-9 \-/]{3,35}?)\s+(?:[A-Z]{2}\d{3,4}|on\s)',
        title
    )
    if m:
        candidate = m.group(1).strip().rstrip(".,")
        if not re.match(r'(?i)accident|incident|occurrence|loss|crash|aircraft$|aircraft\s+accident', candidate):
            return candidate if 3 <= len(candidate) <= 50 else None
    return None

def parse_location_from_title(title):
    if not title: return None
    clean = re.sub(r'\b[A-Z]{2}\d{3,4}\b', '', title)
    clean = re.sub(r'\bG-[A-Z0-9]{3,5}\b', '', clean)
    clean = re.sub(r'\bWK\d{3,4}\b', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    m = re.search(
        r'\bin\s+([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?),\s+(?:Afghanistan|Iraq|Germany|Libya|Cyprus|Pakistan|Belize|Kenya|Oman|Wales|Scotland|England)',
        clean)
    if m: return m.group(1).strip()
    m = re.search(r'\bat\s+([A-Z][A-Za-z0-9 \-,]{3,50}?)(?:\s+on\s+\d|\s*$)', clean)
    if m:
        loc = m.group(1).strip().rstrip(",.")
        loc = re.sub(r',?\s+(?:Wales|Scotland|England|Afghanistan|Iraq|Germany)\s*$', '', loc).strip()
        if not re.match(r'^[A-Z]{2}\d', loc) and loc.lower() not in ("gb","uk") and 3 <= len(loc) <= 60:
            return loc
    m = re.search(r'\bnear\s+([A-Z][A-Za-z0-9 \-]{3,40}?)(?:\s+on\s|\s*,|\s*$)', clean)
    if m:
        loc = m.group(1).strip().rstrip(",.")
        if 3 <= len(loc) <= 60: return loc
    m = re.search(r',\s+([A-Z][A-Za-z][A-Za-z0-9 \-]{2,35}?)\s*$', clean)
    if m:
        loc = m.group(1).strip().rstrip(",.")
        if not re.match(r'^[A-Z]{2}\d', loc) and loc.lower() not in ("gb","uk") and 3 <= len(loc) <= 60:
            return loc
    return None

def parse_operator_from_title(title):
    t = title.lower()
    if any(x in t for x in ["raf","royal air force","air force"]): return "RAF"
    if any(x in t for x in ["royal navy","rnas","fleet air arm","naval air"]): return "Royal Navy"
    if any(x in t for x in ["army","army air corps","aac"]): return "Army Air Corps"
    if "736 naval" in t or "825 naval" in t or "naval air squadron" in t: return "Royal Navy"
    return None

def parse_operator_from_text(txt):
    if not txt: return None
    t = txt[:2000]
    if re.search(r'\bRoyal Air Force\b|\bRAF\b', t): return "RAF"
    if re.search(r'\bRoyal Navy\b|\bFleet Air Arm\b|\bRNAS\b|\bNaval\b', t): return "Royal Navy"
    if re.search(r'\bArmy Air Corps\b|\bAAC\b', t): return "Army Air Corps"
    return None

def parse_country_from_text(txt, title=""):
    combined = (title + " " + (txt or ""))[:3000]
    hints = {
        "Afghanistan":"AF","Kabul":"AF","Helmand":"AF","Kandahar":"AF",
        "Iraq":"IQ","Basra":"IQ","Libya":"LY","Germany":"DE","Sennelager":"DE",
        "Falkland":"FK","Mount Pleasant":"FK","Cyprus":"CY","Belize":"BZ",
        "Kenya":"KE","Oman":"OM","Bahrain":"BH","Norway":"NO",
    }
    for k, code in hints.items():
        if re.search(r'\b' + k + r'\b', combined, re.I): return code
    return "GB"

_CAUSE_RE = re.compile(
    r'(?:cause[s]?|causal factors?|summary of findings)[:\s\-]*\n([^\n]{20,}(?:\n[^\n]{10,}){0,10})',
    re.I)

def extract_probable_cause(text):
    if not text: return None
    m = _CAUSE_RE.search(text)
    if m:
        s = re.sub(r'\s+', ' ', m.group(1).strip())
        return s[:1000] or None
    return None

def _map_report_type(group, title):
    t = title.lower()
    if "board of inquiry" in t or "Board of Inquiries" in group: return "Board of Inquiry"
    if "military aircraft accident summary" in t or "MAAS" in group or "Military Aircraft" in group:
        return "MAAS summary"
    return "Service Inquiry"

def site_slug_fn(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(p for p in parts if p))
    return s.strip("-").lower()[:80] or None

# ─── Main OCR recovery loop ───────────────────────────────────────────────────

def main():
    host = os.environ.get("OCR_REMOTE")
    if not host:
        print("ERROR: OCR_REMOTE env var not set", file=sys.stderr)
        sys.exit(1)
    print(f"[ukmaa-ocr-recover] OCR host: {host}", flush=True)

    c = db_connect()

    # Find all stubs with narrative < 300 chars
    stubs = c.execute(
        "SELECT slug, title, group_name, first_published, main_pdf_path, "
        "aux_pdf_paths, body_html, narrative_text "
        "FROM ukmaa_reports WHERE length(narrative_text) < 300"
    ).fetchall()

    print(f"[ukmaa-ocr-recover] Found {len(stubs)} stub rows (narrative < 300 chars)", flush=True)

    ocr_ok = 0
    ocr_fail = 0
    skipped_no_pdf = 0
    results = []

    for row in stubs:
        slug = row["slug"]
        main_path = row["main_pdf_path"]
        aux_paths = json.loads(row["aux_pdf_paths"] or "[]")

        print(f"\n[ukmaa-ocr-recover] Processing: {slug[:65]}", flush=True)

        if not main_path or not os.path.exists(main_path):
            print(f"  SKIP: PDF missing at {main_path}", flush=True)
            skipped_no_pdf += 1
            results.append((slug, "no_pdf", 0))
            continue

        pdf_size = os.path.getsize(main_path)
        print(f"  PDF: {os.path.basename(main_path)} ({round(pdf_size/1024/1024, 1)}MB)", flush=True)

        # OCR the main (narrative) PDF
        print(f"  Running OCR (decrypt+OCR) via {host} ...", flush=True)
        t0 = time.time()
        ocr_txt = ocr_extract(main_path)
        elapsed = round(time.time() - t0, 1)
        print(f"  OCR done in {elapsed}s: {len(ocr_txt)} chars", flush=True)

        # If main yielded <MIN_NARRATIVE, try aux PDFs too (findings etc.)
        if len(ocr_txt) < MIN_NARRATIVE and aux_paths:
            print(f"  Main OCR short ({len(ocr_txt)}), trying aux PDFs...", flush=True)
            for aux_path in aux_paths:
                if not os.path.exists(aux_path):
                    continue
                ax_txt = ocr_extract(aux_path)
                if ax_txt:
                    ocr_txt = (ocr_txt + "\n\n" + ax_txt).strip()
                    print(f"  + aux {os.path.basename(aux_path)}: {len(ax_txt)} chars (total now {len(ocr_txt)})", flush=True)
                if len(ocr_txt) >= MIN_NARRATIVE:
                    break

        if len(ocr_txt) < FLOOR:
            print(f"  FAIL: OCR returned {len(ocr_txt)} chars (below floor {FLOOR})", flush=True)
            ocr_fail += 1
            results.append((slug, "ocr_blank", len(ocr_txt)))
            continue

        # Update ukmaa_reports
        c.execute(
            "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
            "status='parsed', updated_at=? WHERE slug=?",
            (ocr_txt, "ocr", now(), slug)
        )
        c.commit()

        # Build logic
        title      = row["title"] or ""
        group      = row["group_name"] or ""
        body_html  = row["body_html"] or ""

        event_date   = (parse_date_from_text(title)
                        or parse_date_from_text(slug)
                        or parse_date_from_text(body_html)
                        or parse_date_from_text(ocr_txt[:2000]))
        registration = parse_registration(title) or parse_registration(ocr_txt[:1000])
        aircraft     = parse_aircraft_from_title(title)
        location     = parse_location_from_title(title)
        operator     = parse_operator_from_title(title) or parse_operator_from_text(ocr_txt[:2000])
        country      = parse_country_from_text(ocr_txt[:3000], title)
        prob_cause   = extract_probable_cause(ocr_txt)
        report_type  = _map_report_type(group, title)
        source_url   = f"https://www.gov.uk/government/publications/{slug}"
        case_id_val  = f"ukmaa-{slug}"

        c.execute(
            "INSERT OR REPLACE INTO ukmaa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            " narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id_val, event_date, aircraft, registration, operator, location, country,
             ocr_txt, prob_cause, source_url, report_type,
             site_slug_fn(event_date, aircraft, registration, location),
             "en", now())
        )
        c.execute("UPDATE ukmaa_reports SET status='built', updated_at=? WHERE slug=?",
                  (now(), slug))
        c.commit()

        ocr_ok += 1
        results.append((slug, "ok", len(ocr_txt)))
        print(f"  OK: {len(ocr_txt)} chars written (tier=ocr)", flush=True)

    # ─── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "="*65, flush=True)
    print(f"[ukmaa-ocr-recover] DONE", flush=True)
    print(f"  Stubs found:    {len(stubs)}", flush=True)
    print(f"  OCR OK:         {ocr_ok}", flush=True)
    print(f"  OCR blank/fail: {ocr_fail}", flush=True)
    print(f"  No PDF:         {skipped_no_pdf}", flush=True)

    before_ge300 = 39 - len(stubs)  # total was 39
    after_lt300  = c.execute(
        "SELECT COUNT(*) FROM ukmaa_reports WHERE length(narrative_text) < 300"
    ).fetchone()[0]
    after_ge300 = c.execute(
        "SELECT COUNT(*) FROM ukmaa_reports WHERE length(narrative_text) >= 300"
    ).fetchone()[0]

    print(f"\n  Before: {before_ge300} rows >= 300 chars  ({len(stubs)} stubs)", flush=True)
    print(f"  After:  {after_ge300} rows >= 300 chars  ({after_lt300} stubs remaining)", flush=True)

    recovered_lens = [r[2] for r in results if r[1] == "ok"]
    if recovered_lens:
        recovered_lens.sort()
        n = len(recovered_lens)
        med = (recovered_lens[n//2 - 1] + recovered_lens[n//2]) // 2 if n % 2 == 0 else recovered_lens[n//2]
        print(f"  Recovered row lengths: min={min(recovered_lens)} median={med} max={max(recovered_lens)}", flush=True)

    print(flush=True)
    print("Per-row results:", flush=True)
    for slug, status, chars in results:
        print(f"  {status:10s} {chars:7d} chars  {slug[:60]}", flush=True)

    print(flush=True)
    print("ukmaa_reports status breakdown:", flush=True)
    for row in c.execute("SELECT status, source_tier, count(*) as n FROM ukmaa_reports GROUP BY status, source_tier ORDER BY status, source_tier"):
        print(f"  {row[0]:10s} / {row[1]:8s}: {row[2]}", flush=True)
    print(f"ukmaa_accidents total: {c.execute('SELECT COUNT(*) FROM ukmaa_accidents').fetchone()[0]}", flush=True)
    print(f"ukmaa_accidents with narrative >= 300: {c.execute('SELECT COUNT(*) FROM ukmaa_accidents WHERE length(narrative_text) >= 300').fetchone()[0]}", flush=True)

if __name__ == "__main__":
    main()
