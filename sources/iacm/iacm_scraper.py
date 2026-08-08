#!/usr/bin/env python3
"""IACM (Mozambique civil aviation authority) accident/incident report ingest.

Source: www.iacm.gov.mz/acidentes-incidentes/ (WordPress, PT)
Reports live in /app/uploads/. PDFs are scanned image-only → OCR required.
Registrations: C9-xxx (Mozambique). Language: pt. Country: MZ.

Stages: discover | fetch | parse | build | all
parse-skipped: re-OCR rows stuck at status='skipped'.

NOTE: bare iacm.gov.mz serves the Exchange webmail cert (mail.*); www.iacm.gov.mz has a valid Let's Encrypt cert and verifies normally — always use www.
"""
import sys, os, re, time, sqlite3, shlex, subprocess, tempfile, uuid

BASE = "https://www.iacm.gov.mz"
LISTING = BASE + "/acidentes-incidentes/"
DELAY = 2.0
FLOOR = 80              # absolute build floor (chars)
MIN_NARRATIVE = 600     # preferred tier
HOME = os.path.expanduser("~/iacm-ingest")
DB = os.path.join(HOME, "iacm.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OCR_LANG = "por"

SCHEMA = """
CREATE TABLE IF NOT EXISTS iacm_reports (
  case_id TEXT PRIMARY KEY,
  pdf_url TEXT, pdf_path TEXT,
  anchor_text TEXT, report_type TEXT,
  aircraft TEXT, registration TEXT,
  event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT,
  lang TEXT DEFAULT 'pt',
  status TEXT DEFAULT 'new',
  discovered_at INT, updated_at INT
);
CREATE TABLE IF NOT EXISTS iacm_accidents (
  case_id TEXT PRIMARY KEY,
  event_date TEXT, aircraft TEXT,
  registration TEXT, operator TEXT,
  location TEXT, country TEXT DEFAULT 'MZ',
  narrative_text TEXT, probable_cause TEXT,
  source_url TEXT, report_type TEXT,
  site_slug TEXT, lang TEXT DEFAULT 'pt',
  built_at INT
);
CREATE INDEX IF NOT EXISTS idx_iacm_status ON iacm_reports(status);
"""

def now(): return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def http():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=60.0,
        follow_redirects=True,
    )

# ---- OCR helpers (copied from aaid-ingest/aaid_ingest/pdf.py) ----

def _ocr_remote(pdf_path, lang, host):
    """Ship PDF to remote host, OCR with ocrmypdf, return sidecar text."""
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print(f"[iacm ocr] scp failed rc={cp.returncode}", file=sys.stderr, flush=True)
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[iacm ocr] remote exception: {e}", file=sys.stderr, flush=True)
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    """OCR a scanned PDF; uses OCR_REMOTE env (hetzner) when set."""
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
                ["ocrmypdf", "--force-ocr", "--language", lang,
                 "--sidecar", sidecar, "--output-type", "none",
                 str(pdf_path), "-"],
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
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

# ---- slug / ID helpers ----

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

def _case_id_from_url(url):
    """Derive a stable case_id from the PDF filename (never order-dependent)."""
    fn = url.rstrip("/").split("/")[-1]
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I)
    # URL-decode common chars
    fn = fn.replace("%C2%BA", "").replace("%C3%93", "O").replace("%C3%A3", "a")
    fn = re.sub(r"%[0-9A-Fa-f]{2}", "", fn)
    # Clean to slug-safe
    s = re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-").lower()
    return s[:60] or "iacm-unknown"

def _authority_id_from_text(text):
    """Try to extract official report number from OCR text.
    IACM uses patterns like 'CIA-XX/YYYY' or 'REF: CIA-...' in their reports.
    """
    if not text:
        return None
    m = re.search(r"\b(CIA-\d{2,3}[/-]\d{4})\b", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(IACM[/-]\d{2,4}[/-]\d{4})\b", text, re.I)
    if m:
        return m.group(1).upper()
    return None

# ---- date parsing (Portuguese month names) ----

_PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
    # variants / typos seen in scanned docs
    "marco": 3, "jan": 1, "fev": 2, "mar": 3, "abr": 4,
    "mai": 5, "jun": 6, "jul": 7, "ago": 8,
    "set": 9, "out": 10, "nov": 11, "dez": 12,
}
_PT_DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(_PT_MONTHS.keys()) + r")\s+(?:de\s+)?((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
_NUMERIC_DATE_RE = re.compile(r"\b([0-3]?\d)[./-]([01]?\d)[./-]((?:19|20)\d{2})\b")

def parse_date(txt):
    if not txt: return None
    # Priority 1: numeric date in the first 800 chars (cover page)
    m = _NUMERIC_DATE_RE.search(txt[:800])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 2: Portuguese written-out month anywhere in text
    m = _PT_DATE_TEXT_RE.search(txt)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _PT_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 3: numeric date anywhere
    m = _NUMERIC_DATE_RE.search(txt)
    if not m: return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if 1 <= mo <= 12 and 1 <= d <= 31:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

# ---- registration / aircraft extraction ----

_REG_RE = re.compile(r"\b(C9-[A-Z]{2,4}|ZU-[A-Z]{2,4})\b", re.I)

def parse_reg(txt):
    if not txt: return None
    m = _REG_RE.search(txt)
    return m.group(1).upper() if m else None

def parse_aircraft(txt):
    """Extract aircraft type from Portuguese OCR text."""
    if not txt: return None
    for pat in [
        r"aeronave\s+(?:do\s+tipo\s+)?([A-Za-z0-9][A-Za-z0-9 \-/]{2,30}),",
        r"tipo\s+([A-Za-z0-9][A-Za-z0-9 \-/]{2,30})[,\n]",
        r"\bhelicóptero\s+([A-Za-z0-9][A-Za-z0-9 \-/]{2,30})[,\n]",
        r"\bavião\s+([A-Za-z0-9][A-Za-z0-9 \-/]{2,30})[,\n]",
        r"aircraft\s+type\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9 \-/]{2,30})[,\n]",
    ]:
        m = re.search(pat, txt[:3000], re.IGNORECASE)
        if m:
            acft = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(acft) > 2:
                return acft
    return None

def parse_location(txt):
    """Extract location from Portuguese OCR text."""
    if not txt: return None
    for pat in [
        r"em\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÇÑÜ][a-záéíóúàãõâêôçñü\- ]{2,40}),?\s+Moçambique",
        r"(?:ocorreu|sucedeu|aconteceu)\s+(?:em|no|na|perto\s+de)\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÇÑÜ][a-záéíóúàãõâêôçñü\- ]{2,40})[,\.]",
        r"aeródromo\s+(?:de\s+)?([A-ZÁÉÍÓÚÀÃÕÂÊÔÇÑÜ][A-Za-záéíóúàãõâêôçñü\- ]{2,40})[,\.]",
        r"aeroporto\s+(?:de\s+|internacional\s+de\s+)?([A-ZÁÉÍÓÚÀÃÕÂÊÔÇÑÜ][A-Za-záéíóúàãõâêôçñü\- ]{2,40})[,\.]",
        r"em\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÇÑÜ][a-záéíóúàãõâêôçñü\- ]{2,40}),",
        # EN patterns (ZU-MDI report is bilingual)
        r"(?:near|at|in)\s+([A-Z][a-zA-Z\- ]{3,40}),?\s+Mozambique",
        r"airport\s+of\s+([A-Z][a-zA-Z\- ]{2,40})[,\.]",
    ]:
        m = re.search(pat, txt[:4000], re.IGNORECASE)
        if m:
            loc = re.split(r"[\n,]", m.group(1).strip())[0].strip()
            if len(loc) > 2:
                return loc
    return None

def parse_operator(txt):
    """Extract operator from OCR text."""
    if not txt: return None
    for pat in [
        r"operador[a]?\s*[:\-]\s*([A-Za-záéíóúàãõâêôçñü][A-Za-z áéíóúàãõâêôçñü\.,]{3,50})",
        r"companhia\s+aérea\s*[:\-]?\s*([A-Za-záéíóúàãõâêôçñü][A-Za-z áéíóúàãõâêôçñü\.,]{3,40})[,\n]",
        r"operator\s*[:\-]\s*([A-Za-z][A-Za-z \.,]{3,50})[,\n]",
    ]:
        m = re.search(pat, txt[:3000], re.IGNORECASE)
        if m:
            op = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(op) > 2:
                return op
    return None

def parse_probable_cause(txt):
    """Extract probable cause from OCR text."""
    if not txt: return None
    for pat in [
        r"causa\s+provável\s*[:\-]?\s*(.{50,600}?)(?:\n\n|\Z)",
        r"causas?\s+(?:do\s+)?acidente\s*[:\-]?\s*(.{50,600}?)(?:\n\n|\Z)",
        r"probable\s+cause\s*[:\-]?\s*(.{50,600}?)(?:\n\n|\Z)",
        r"findings\s+[:\-]\s*(.{50,600}?)(?:\n\n|\Z)",
    ]:
        m = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
        if m:
            cause = m.group(1).strip()
            # trim to reasonable length
            cause = re.sub(r"\s+", " ", cause)
            if len(cause) > 50:
                return cause[:1000]
    return None

# ---- report classification ----

def classify_report_type(url, anchor):
    u = url.lower()
    a = (anchor or "").lower()
    if "final" in u or "final" in a:
        return "Final report"
    if "preliminar" in u or "prelim" in u or "preliminar" in a:
        return "Preliminary report"
    return "Investigation report"

# ---- discover: parse the accidents page for report PDFs ----

def discover(c, cl):
    """Fetch the listing page and extract investigation report PDF links."""
    r = cl.get(LISTING)
    time.sleep(DELAY)
    if r.status_code != 200:
        print(f"[iacm discover] HTTP {r.status_code}", file=sys.stderr)
        return 0

    # Find all PDF links; filter to actual investigation reports (not schedules/forms)
    pdf_re = re.compile(r'href=["\']([^"\']*\.pdf[^"\']*)["\'][^>]*>\s*([^<]*)\s*<', re.I)
    inserted = 0
    seen_urls = set()

    for m in pdf_re.finditer(r.text):
        raw_url = m.group(1).strip()
        anchor = m.group(2).strip()

        # Normalise URL: http → https, ensure www
        url = raw_url.replace("http://www.iacm.gov.mz", BASE)
        if not url.startswith("http"):
            url = BASE + url

        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Skip quarterly schedules and notification forms
        fn_lower = url.lower()
        if "schedule" in fn_lower or "formulário" in fn_lower.replace("%c3%a1", "á") \
                or "notifica" in fn_lower or "comunicac" in fn_lower \
                or "reporte-de-ocorrencias" in fn_lower or "reporte-de" in fn_lower:
            print(f"[iacm discover] skip (non-report): {url.split('/')[-1][:60]}", flush=True)
            continue

        # Accept only actual investigation reports
        a_lower = anchor.lower()
        fn = fn_lower.split("/")[-1]
        is_report = (
            "relat" in fn or "report" in fn or
            "relat" in a_lower or "report" in a_lower or
            "acidente" in a_lower or "accident" in a_lower or
            "incidente" in a_lower or "incident" in a_lower
        )
        if not is_report:
            continue

        cid = _case_id_from_url(url)
        if c.execute("SELECT 1 FROM iacm_reports WHERE case_id=?", (cid,)).fetchone():
            print(f"[iacm discover] already known: {cid}", flush=True)
            continue

        rtype = classify_report_type(url, anchor)
        reg = parse_reg(anchor) or parse_reg(url)
        loc = None
        # extract location hint from anchor/URL (e.g. "em Quelimane", "PEMBA")
        if "quelimane" in a_lower or "quelimane" in fn_lower:
            loc = "Quelimane"
        elif "pemba" in a_lower or "pemba" in fn_lower:
            loc = "Pemba"

        c.execute(
            "INSERT OR IGNORE INTO iacm_reports "
            "(case_id,pdf_url,anchor_text,report_type,registration,location,"
            " lang,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, url, anchor, rtype, reg, loc, "pt", "new", now(), now())
        )
        c.commit()
        inserted += 1
        print(f"[iacm discover] {cid} | {rtype} | {anchor[:60]}", flush=True)

    return inserted

# ---- fetch ----

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, pdf_url FROM iacm_reports WHERE status='new'"
    ).fetchall()
    done = 0
    for row in rows:
        cid, url = row["case_id"], row["pdf_url"]
        dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", cid) + ".pdf")
        try:
            time.sleep(DELAY)
            r = cl.get(url)
            ct = r.headers.get("content-type", "")
            if "pdf" not in ct.lower() and r.content[:4] != b"%PDF":
                raise ValueError(f"not a pdf (ct={ct} status={r.status_code})")
            with open(dest, "wb") as fh:
                fh.write(r.content)
            sz = os.path.getsize(dest)
            c.execute(
                "UPDATE iacm_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",
                (dest, now(), cid)
            )
            c.commit()
            done += 1
            print(f"[iacm fetch] {cid}: {sz//1024}KB -> {dest}", flush=True)
        except Exception as e:
            print(f"[iacm fetch] {cid}: {e}", file=sys.stderr, flush=True)
    return done

# ---- parse ----

def _parse_one(cid, pdf_path, use_ocr=False):
    """pdftotext + OCR fallback. Returns (text, tier)."""
    txt = extract_text(pdf_path)
    if len(txt) >= MIN_NARRATIVE:
        return txt, "pdf"
    if len(txt) >= FLOOR:
        return txt, "pdf"
    # below floor → OCR
    if use_ocr and pdf_path and os.path.exists(pdf_path):
        print(f"[iacm parse] {cid}: pdftotext={len(txt)} chars → OCR ({OCR_LANG})", flush=True)
        ocr_txt = ocr_extract(pdf_path, lang=OCR_LANG)
        if len(ocr_txt) >= FLOOR:
            print(f"[iacm parse] {cid}: OCR yielded {len(ocr_txt)} chars", flush=True)
            return ocr_txt, "ocr"
        else:
            print(f"[iacm parse] {cid}: OCR also blank ({len(ocr_txt)} chars)", file=sys.stderr, flush=True)
    tier = "scanned" if pdf_path and os.path.exists(pdf_path) else "none"
    return txt, tier

def parse(c):
    rows = c.execute(
        "SELECT case_id, pdf_path FROM iacm_reports WHERE status='fetched'"
    ).fetchall()
    for row in rows:
        txt, tier = _parse_one(row["case_id"], row["pdf_path"], use_ocr=True)
        c.execute(
            "UPDATE iacm_reports SET narrative_text=?,source_tier=?,status='parsed',updated_at=? "
            "WHERE case_id=?",
            (txt, tier, now(), row["case_id"])
        )
        c.commit()
    return len(rows)

def parse_skipped(c):
    rows = c.execute(
        "SELECT case_id, pdf_path FROM iacm_reports WHERE status='skipped'"
    ).fetchall()
    ocr_ok = 0; still_blank = 0
    for row in rows:
        cid = row["case_id"]
        if not row["pdf_path"] or not os.path.exists(row["pdf_path"]):
            print(f"[iacm parse-skipped] {cid}: PDF missing", file=sys.stderr, flush=True)
            continue
        txt, tier = _parse_one(cid, row["pdf_path"], use_ocr=True)
        if len(txt) >= FLOOR:
            ocr_ok += 1
        else:
            still_blank += 1
        c.execute(
            "UPDATE iacm_reports SET narrative_text=?,source_tier=?,status='parsed',updated_at=? "
            "WHERE case_id=?",
            (txt, tier, now(), cid)
        )
        c.commit()
    print(f"[iacm parse-skipped] ocr_ok={ocr_ok} still_blank={still_blank}", flush=True)
    return ocr_ok, still_blank

# ---- build ----

def build(c):
    rows = c.execute(
        "SELECT case_id,event_date,aircraft,registration,location,narrative_text,"
        "source_tier,report_type,pdf_url,lang FROM iacm_reports WHERE status='parsed'"
    ).fetchall()
    built = 0; skipped = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        tier = r["source_tier"] or ""

        if tier not in ("pdf", "ocr") or len(narr) < FLOOR:
            skipped += 1
            c.execute(
                "UPDATE iacm_reports SET status='skipped',updated_at=? WHERE case_id=?",
                (now(), r["case_id"])
            )
            c.commit()
            continue

        # Enrich metadata from narrative text (both OCR and native-PDF tiers)
        event_date = r["event_date"]
        registration = r["registration"]
        aircraft = r["aircraft"]
        location = r["location"]
        operator = None
        probable_cause = None

        if tier in ("ocr", "pdf"):
            if not event_date:
                event_date = parse_date(narr)
            if not registration:
                registration = parse_reg(narr[:4000])
            elif registration:
                # OCR may have truncated the registration (e.g. ZU-MD vs ZU-MDI);
                # search wider context for a longer/better match
                better = parse_reg(narr[:4000])
                if better and len(better) > len(registration):
                    registration = better
            if not aircraft:
                aircraft = parse_aircraft(narr)
            if not location:
                location = parse_location(narr)
            operator = parse_operator(narr)
            probable_cause = parse_probable_cause(narr)

        # Try to extract official report ID from OCR text
        auth_id = _authority_id_from_text(narr)

        slug = site_slug(registration, location) if registration else site_slug(r["case_id"])
        # case_id must be a STABLE intrinsic id; the URL-derived work-table key carries
        # mangled diacritics, so prefer authority report no., else the reg+location slug.
        cid_final = auth_id if auth_id else slug

        c.execute(
            "INSERT OR REPLACE INTO iacm_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            " narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid_final, event_date, aircraft, registration, operator,
             location, "MZ", narr, probable_cause, r["pdf_url"],
             r["report_type"] or "Investigation report",
             slug, r["lang"] or "pt", now())
        )
        c.execute(
            "UPDATE iacm_reports SET status='built',updated_at=? WHERE case_id=?",
            (now(), r["case_id"])
        )
        c.commit()
        built += 1
        print(f"[iacm build] {cid_final}: date={event_date} reg={registration} loc={location} "
              f"narr={len(narr)} tier={tier}", flush=True)

    return built, skipped

# ---- main ----

def main():
    import warnings; warnings.filterwarnings("ignore")  # suppress httpx TLS warnings
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "fetch", "all"):
        cl = http()
        try:
            if mode in ("discover", "all"):
                n = discover(c, cl)
                print(f"discovered: {n}")
            if mode in ("fetch", "all"):
                n = fetch(c, cl)
                print(f"fetched: {n}")
        finally:
            cl.close()

    if mode in ("parse", "all"):
        n = parse(c)
        print(f"parsed: {n}")

    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}")

    if mode in ("build", "all", "parse-skipped"):
        b, sk = build(c)
        print(f"built: {b}  skipped: {sk}")

    print("reports:", list(c.execute("SELECT status, count(*) FROM iacm_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM iacm_accidents").fetchone()[0])

    if mode in ("all", "build", "parse-skipped"):
        print("\n--- iacm_accidents summary ---")
        for row in c.execute(
            "SELECT case_id, event_date, registration, site_slug, LENGTH(narrative_text) AS nl "
            "FROM iacm_accidents ORDER BY event_date"
        ):
            print(f"  {row[0]!s:55s} | {row[1]!s:12s} | {row[2]!s:10s} | {row[3]!s:35s} | {row[4]}")

if __name__ == "__main__":
    main()
