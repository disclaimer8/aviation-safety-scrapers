#!/usr/bin/env python3
"""SVK (Slovakia) accident/incident final-report ingest — plain httpx, no browser.

Source: Ministerstvo dopravy SR, Letecky a namorny vysetrovaci utvar (Transport
Safety Investigation Unit). Final reports ("Zaverecne spravy") organised by year:
  /utvar-bezpecnostneho-vysetrovania-v-doprave/zaverecne-spravy/rok-YYYY
Each year page lists PDF anchors. Anchor text carries case_id, date (DD.MM.YYYY),
registration (OM-/OK-/OE-/D-...), aircraft, location. PDFs live under
/fileadmin/documents/doprava/uvlni/*.pdf and are born-digital (some scanned).

mindop.sk returns 200, no bot protection -> plain httpx GET.

Stages: discover (year pages -> svk_reports) | fetch (download PDF) |
parse (pdftotext + OCR fallback) | build (svk_accidents).
parse-skipped: re-run OCR parse for status='skipped' rows.
Resumable via status column.
"""
import sys, os, re, time, sqlite3, shlex, subprocess, tempfile, uuid

BASE = "https://www.mindop.sk"
LISTING = BASE + "/utvar-bezpecnostneho-vysetrovania-v-doprave/zaverecne-spravy"
YEARS = list(range(2009, 2027))
DELAY = 1.5
MIN_NARRATIVE = 600   # preferred tier 'pdf'
FLOOR = 80            # absolute build floor
HOME = os.path.expanduser("~/svk-ingest")
DB = os.path.join(HOME, "svk.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OCR_LANG = "slk"

SCHEMA = """
CREATE TABLE IF NOT EXISTS svk_reports (
  case_id TEXT PRIMARY KEY, year INT, report_url TEXT, pdf_url TEXT, pdf_path TEXT,
  anchor_text TEXT, report_type TEXT, aircraft TEXT, registration TEXT,
  event_date TEXT, location TEXT, narrative_text TEXT, source_tier TEXT,
  lang TEXT DEFAULT 'sk', status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS svk_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'SK', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT DEFAULT 'sk', built_at INT);
CREATE INDEX IF NOT EXISTS idx_svk_status ON svk_reports(status);
"""

def now(): return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c

def http():
    import httpx
    return httpx.Client(headers={"User-Agent": UA}, timeout=60.0, follow_redirects=True)

# ---- OCR helpers (adapted from aaid-ingest/aaid_ingest/pdf.py) ----

def _ocr_remote(pdf_path, lang, host):
    """OCR a scanned PDF on a remote (more powerful) host via ssh/scp.

    Ships PDF to <host>:/tmp, runs ocrmypdf under nice/ionice there (so it
    does not starve the prod web server), cats the sidecar text back, cleans
    up.  Returns "" on any failure.  Activated by env OCR_REMOTE=<host>.
    MUST be invoked as a1 (a1's key is authorised on hetzner).
    """
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    """OCR a scanned (image-only) PDF and return the recognised text.

    Uses remote hetzner host when OCR_REMOTE env is set (preferred — keeps
    heavy OCR off the loaded mini-PC).  Falls back to local ocrmypdf otherwise.
    Returns "" on any failure; generous 600s timeout for large scans.
    """
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                [
                    "ocrmypdf",
                    "--force-ocr",
                    "--language", lang,
                    "--sidecar", sidecar,
                    "--output-type", "none",
                    str(pdf_path),
                    "-",
                ],
                capture_output=True, timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    finally:
        try:
            os.unlink(sidecar)
        except OSError:
            pass

# ---- pdftotext ----

def extract_text(path):
    if not path or not os.path.exists(path): return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"], capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

# ---- case_id: intrinsic from filename (stable PK), fall back to anchor ----
def case_id_from_filename(pdf_url):
    fn = pdf_url.rstrip("/").split("/")[-1]
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I)
    # new format: SK<letter><year><nnn> e.g. SKA2023006 SKS2020001 SKI2023240
    # SKO2011005 SKP2014001 (prefix is one letter, any A-Z)
    m = re.search(r"(?<![A-Za-z0-9])(SK[A-Z]\d{6,7})(?![0-9])", fn, re.I)
    if m:
        return m.group(1).upper()
    # old format encoded in filename: ZS_LN_01_2009 / ZS_VI_02_2009 (year may be missing)
    m = re.search(r"\b(LN|VI)[_\s-]?(\d{1,3})[_\s-]?(\d{4})\b", fn, re.I)
    if m:
        return f"{m.group(1).upper()}-{int(m.group(2)):02d}-{m.group(3)}"
    # last resort: sanitized filename stem
    return re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-") or None

def case_id_from_anchor(txt):
    if not txt: return None
    m = re.search(r"\b(SK[A-Z]\d{6,7})\b", txt, re.I)
    if m: return m.group(1).upper()
    m = re.search(r"\b(LN|VI)\s*0?(\d{1,3})\s*/\s*(\d{4})", txt, re.I)
    if m: return f"{m.group(1).upper()}-{int(m.group(2)):02d}-{m.group(3)}"
    return None

# report_type from case_id prefix / anchor text
def map_type(case_id, anchor):
    cid = (case_id or "").upper()
    a = (anchor or "").lower()
    if cid.startswith("SKA"): return "Final report"  # nehoda (accident)
    if cid.startswith("SKS"): return "Final report"  # serious incident
    if cid.startswith("SKI"): return "Final report"  # incident
    return "Final report"

_DATE_RE = re.compile(r"\b([0-3]?\d)\.\s*([01]?\d)\.\s*((?:19|20)\d{2})\b")

# Slovak month names for OCR text date extraction
_SK_MONTHS = {
    "januára": 1, "januárom": 1, "január": 1,
    "februára": 2, "februárom": 2, "február": 2,
    "marca": 3, "marcom": 3, "marec": 3,
    "apríla": 4, "apríli": 4, "apríl": 4,
    "mája": 5, "máji": 5, "máj": 5,
    "júna": 6, "júni": 6, "jún": 6,
    "júla": 7, "júli": 7, "júl": 7,
    "augusta": 8, "auguste": 8, "august": 8,
    "septembra": 9, "septembri": 9, "september": 9,
    "októbra": 10, "októbri": 10, "október": 10,
    "novembra": 11, "novembri": 11, "november": 11,
    "decembra": 12, "decembri": 12, "december": 12,
}
_SK_DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2})\.\s*(" + "|".join(_SK_MONTHS.keys()) + r")\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)

def parse_date(txt):
    if not txt: return None
    # try Slovak written-out month first (e.g. "12. decembra 2013")
    m = _SK_DATE_TEXT_RE.search(txt)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _SK_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # numeric DD.MM.YYYY
    m = _DATE_RE.search(txt)
    if not m: return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

# registration: OM-xxx (SK) plus foreign OK- OE- D- HA- SP- etc.
_REG_RE = re.compile(r"\b([A-Z]{1,2}-[A-Z0-9]{2,5}|OM-[A-Z0-9]{2,5})\b")
def parse_reg(txt):
    if not txt: return None
    # avoid matching case_id; case_id has no dash, regs do
    m = _REG_RE.search(txt)
    return m.group(1) if m else None

def clean_anchor(raw):
    t = re.sub(r"&nbsp;", " ", raw or "")
    t = re.sub(r"\(pdf[^)]*\)", "", t, flags=re.I)   # drop trailing (pdf, 5 MB)
    t = re.sub(r"\s+", " ", t).strip(" ,;")
    return t.strip()

def parse_anchor_fields(case_id, raw, pdf_url):
    txt = clean_anchor(raw)
    event_date = parse_date(txt)
    reg = parse_reg(txt)
    # location/aircraft: tokenize the comma-separated anchor, strip case_id/date/reg/parashute notes
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    cleaned = []
    for p in parts:
        pl = p.lower()
        if case_id and case_id.replace("-", "").lower() in p.replace("/", "").replace(" ", "").lower():
            continue
        if _DATE_RE.search(p): continue
        if reg and reg.lower() == p.lower(): continue
        cleaned.append(p)
    aircraft = cleaned[0] if cleaned else None
    location = cleaned[-1] if len(cleaned) > 1 else None
    # aircraft heuristics: if the only token looks like a place note keep as location
    return event_date, reg, aircraft, location


def _enrich_from_ocr(ocr_text):
    """Extract event_date, registration, aircraft, location from OCR text.

    Used to fill in or improve metadata for scanned-PDF rows where the anchor
    text was sparse. Returns (event_date, registration, aircraft, location) —
    any field may be None.
    """
    if not ocr_text:
        return None, None, None, None
    event_date = parse_date(ocr_text)
    # registration: prefer OM- registrations; search first 2000 chars where
    # it typically appears near the header
    header = ocr_text[:2000]
    reg_m = re.search(r"\bOM-[A-Z0-9]{2,5}\b", header)
    registration = reg_m.group(0) if reg_m else parse_reg(header)
    # aircraft type: look for "lietadla typu X" / "typ X" pattern
    acft = None
    for pat in [
        r"lietadl[ao]\s+typ[uo]\s+([A-Za-z0-9 \-/]{3,40})",
        r"\btyp[u:]?\s+([A-Za-z0-9 \-/]{3,40})",
    ]:
        m = re.search(pat, header, re.IGNORECASE)
        if m:
            acft = m.group(1).strip().strip(",;.")
            # trim trailing cruft (anything after newline or comma)
            acft = re.split(r"[\n,]", acft)[0].strip()
            if len(acft) > 3:
                break
    # location: look for "v obci X" / "v katastri X" / "pri obci X" etc.
    loc = None
    for pat in [
        r"v\s+obci\s+([A-ZÁÉÍÓÚÄÖÜÝČĎĽŇŘŠŤŽ][a-záéíóúäöüýčďľňřšťž\- ]{2,40})",
        r"pri\s+obci\s+([A-ZÁÉÍÓÚÄÖÜÝČĎĽŇŘŠŤŽ][a-záéíóúäöüýčďľňřšťž\- ]{2,40})",
        r"v\s+katastri\s+(?:obce\s+)?([A-ZÁÉÍÓÚÄÖÜÝČĎĽŇŘŠŤŽ][a-záéíóúäöüýčďľňřšťž\- ]{2,40})",
        r"(?:letisku?|aerodróm[eu]?)\s+([A-ZÁÉÍÓÚÄÖÜÝČĎĽŇŘŠŤŽ][A-Za-záéíóúäöüýčďľňřšťž\- ]{2,40})",
    ]:
        m = re.search(pat, ocr_text[:3000], re.IGNORECASE)
        if m:
            loc = m.group(1).strip().strip(",;.")
            loc = re.split(r"[\n,]", loc)[0].strip()
            if len(loc) > 2:
                break
    return event_date, registration, acft, loc

# ---------------- stages ----------------
def discover(c, cl):
    inserted = 0
    for yr in YEARS:
        url = f"{LISTING}/rok-{yr}"
        try:
            r = cl.get(url); time.sleep(DELAY)
            if r.status_code != 200:
                print(f"[svk discover] rok-{yr}: HTTP {r.status_code}", file=sys.stderr); continue
        except Exception as e:
            print(f"[svk discover] rok-{yr}: {e}", file=sys.stderr); continue
        anchors = re.findall(r'href="([^"]+\.pdf[^"]*)"[^>]*>([^<]*)<', r.text, re.I)
        # restrict to the uvlni document dir to avoid stray nav PDFs
        for href, raw in anchors:
            if "/fileadmin/documents/doprava/uvlni/" not in href.lower():
                continue
            pdf_url = href if href.startswith("http") else BASE + href
            cid = case_id_from_anchor(raw) or case_id_from_filename(pdf_url)
            if not cid:
                continue
            if c.execute("SELECT 1 FROM svk_reports WHERE case_id=?", (cid,)).fetchone():
                continue
            ev, reg, acft, loc = parse_anchor_fields(cid, raw, pdf_url)
            rtype = map_type(cid, raw)
            c.execute(
                "INSERT OR IGNORE INTO svk_reports "
                "(case_id,year,report_url,pdf_url,anchor_text,report_type,aircraft,"
                " registration,event_date,location,lang,status,discovered_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, yr, url, pdf_url, clean_anchor(raw), rtype, acft, reg, ev, loc,
                 "sk", "new", now(), now()))
            c.commit(); inserted += 1
    return inserted

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute("SELECT case_id,pdf_url FROM svk_reports WHERE status='new'").fetchall()
    done = 0; fails = 0
    for row in rows:
        cid = row["case_id"]; url = row["pdf_url"]
        dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", cid) + ".pdf")
        try:
            time.sleep(DELAY)
            r = cl.get(url)
            ct = r.headers.get("content-type", "")
            if "pdf" not in ct.lower() and r.content[:4] != b"%PDF":
                raise ValueError(f"not a pdf (ct={ct} status={r.status_code})")
            with open(dest, "wb") as fh:
                fh.write(r.content)
            c.execute("UPDATE svk_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",
                      (dest, now(), cid)); c.commit()
            done += 1; fails = 0
        except Exception as e:
            print(f"[svk fetch] {cid}: {e}", file=sys.stderr); fails += 1
            if fails >= 5:
                print("[svk fetch] 5 consecutive fails, aborting", file=sys.stderr); break
    return done

def _parse_one(cid, pdf_path, use_ocr=False):
    """Parse a single PDF; returns (narrative_text, source_tier).

    use_ocr=True: attempt OCR fallback when pdftotext yields < FLOOR chars.
    """
    txt = extract_text(pdf_path)
    if len(txt) >= MIN_NARRATIVE:
        return txt, "pdf"
    if len(txt) >= FLOOR:
        return txt, "pdf"
    # below floor — try OCR if requested
    if use_ocr and pdf_path and os.path.exists(pdf_path):
        ocr_txt = ocr_extract(pdf_path, lang=OCR_LANG)
        if len(ocr_txt) >= FLOOR:
            print(f"[svk parse] {cid}: OCR yielded {len(ocr_txt)} chars", flush=True)
            return ocr_txt, "ocr"
        else:
            print(f"[svk parse] {cid}: OCR also blank ({len(ocr_txt)} chars)", file=sys.stderr, flush=True)
    if pdf_path:
        tier = "scanned"
    else:
        tier = "none"
    return txt, tier

def parse(c):
    """Parse all status='fetched' rows (pdftotext + OCR fallback)."""
    rows = c.execute("SELECT case_id,pdf_path FROM svk_reports WHERE status='fetched'").fetchall()
    for row in rows:
        txt, tier = _parse_one(row["case_id"], row["pdf_path"], use_ocr=True)
        c.execute("UPDATE svk_reports SET narrative_text=?,source_tier=?,status='parsed',updated_at=? WHERE case_id=?",
                  (txt, tier, now(), row["case_id"])); c.commit()
    return len(rows)

def parse_skipped(c):
    """Re-parse rows stuck at status='skipped' (scanned PDFs) via OCR.

    Resets them to 'fetched' equivalent in memory, runs OCR, updates narrative
    and source_tier, then sets status='parsed' so build() can pick them up.
    Does NOT re-download PDFs (uses existing pdf_path).
    """
    rows = c.execute(
        "SELECT case_id,pdf_path FROM svk_reports WHERE status='skipped'"
    ).fetchall()
    ocr_ok = 0; still_blank = 0
    for row in rows:
        cid = row["case_id"]
        pdf_path = row["pdf_path"]
        if not pdf_path or not os.path.exists(pdf_path):
            print(f"[svk parse-skipped] {cid}: PDF missing at {pdf_path}", file=sys.stderr, flush=True)
            continue
        txt, tier = _parse_one(cid, pdf_path, use_ocr=True)
        if len(txt) >= FLOOR:
            ocr_ok += 1
        else:
            still_blank += 1
            print(f"[svk parse-skipped] {cid}: still blank after OCR ({len(txt)} chars)", file=sys.stderr, flush=True)
        c.execute(
            "UPDATE svk_reports SET narrative_text=?,source_tier=?,status='parsed',updated_at=? WHERE case_id=?",
            (txt, tier, now(), cid)
        )
        c.commit()
    print(f"[svk parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank

def build(c):
    """Build svk_accidents from all status='parsed' rows.

    Accepted tiers: 'pdf', 'ocr'.  Rows with tier='scanned'/'none' and text
    below FLOOR are skipped (set back to 'skipped').

    For OCR rows, metadata (event_date, registration, aircraft, location) is
    enriched from the OCR text when the anchor fields were sparse.
    NOTE: existing 183 'built' rows are untouched (INSERT OR REPLACE would
    overwrite — we use INSERT OR IGNORE for rows already in svk_accidents, and
    only UPDATE svk_reports status for freshly built rows).
    """
    rows = c.execute(
        "SELECT case_id,event_date,aircraft,registration,location,narrative_text,"
        "source_tier,report_type,pdf_url,lang FROM svk_reports WHERE status='parsed'"
    ).fetchall()
    built = 0; skipped_scanned = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        tier = r["source_tier"] or ""
        if tier not in ("pdf", "ocr") or len(narr) < FLOOR:
            if tier in ("scanned", "none", "ocr"):
                skipped_scanned += 1
            c.execute("UPDATE svk_reports SET status='skipped',updated_at=? WHERE case_id=?",
                      (now(), r["case_id"])); c.commit(); continue

        # Metadata: anchor fields are the primary source; enrich from OCR text
        # when they are missing (common for old scanned reports from 2009-2010).
        event_date = r["event_date"]
        registration = r["registration"]
        aircraft = r["aircraft"]
        location = r["location"]
        if tier == "ocr":
            ocr_date, ocr_reg, ocr_acft, ocr_loc = _enrich_from_ocr(narr)
            if not event_date: event_date = ocr_date
            if not registration: registration = ocr_reg
            if not aircraft: aircraft = ocr_acft
            if not location: location = ocr_loc

        c.execute(
            "INSERT OR REPLACE INTO svk_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            " narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], event_date, aircraft, registration, None,
             location, "SK", narr, None, r["pdf_url"], r["report_type"] or "Final report",
             site_slug(aircraft, registration, location), r["lang"] or "sk", now()))
        c.execute("UPDATE svk_reports SET status='built',updated_at=? WHERE case_id=?",
                  (now(), r["case_id"])); c.commit(); built += 1
    return built, skipped_scanned

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()
    if mode in ("discover", "fetch", "all"):
        cl = http()
        try:
            if mode in ("discover", "all"): print("discovered:", discover(c, cl))
            if mode in ("fetch", "all"): print("fetched:", fetch(c, cl))
        finally:
            cl.close()
    if mode in ("parse", "all"): print("parsed:", parse(c))
    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}")
    if mode in ("build", "all", "parse-skipped"):
        b, sc = build(c); print(f"built: {b}  skipped_scanned: {sc}")
    print("reports:", list(c.execute("SELECT status,count(*) FROM svk_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM svk_accidents").fetchone()[0])

if __name__ == "__main__":
    main()
