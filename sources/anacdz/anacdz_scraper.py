#!/usr/bin/env python3
"""anacdz — ANAC Algeria (Agence Nationale de l'Aviation Civile, DZ)
aviation-accident/serious-incident investigation report ingest.

Source: https://www.anac.dz/en/investigation-reports/ (WordPress, EN+FR)
Reports are direct-linked PDFs uploaded to wp-content/uploads/2025/12/.
All PDFs are French-language investigation reports.
Registrations: 7T-xxx (Algeria). Note: Air Algérie 5017 (Mali, 2014) = BEA-France, NOT here.

case_id = 'anacdz-' + sanitized-pdf-filename (intrinsic, order-independent)
event_date: extracted from PDF text (French dates)
lang: 'fr'
country: 'DZ'

Stages: discover | fetch | parse | build | all
"""
import sys, os, re, time, sqlite3, subprocess, shlex, uuid

BASE = "https://anac.dz"
LISTING = "https://www.anac.dz/en/investigation-reports/"
DELAY = 2.0
FLOOR = 300
HOME = os.path.expanduser("~/anacdz-ingest")
DB = os.path.join(HOME, "anacdz.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OCR_LANG = "fra"

SCHEMA = """
CREATE TABLE IF NOT EXISTS anacdz_reports (
  case_id        TEXT PRIMARY KEY,
  pdf_url        TEXT,
  pdf_path       TEXT,
  anchor_text    TEXT,
  report_type    TEXT,
  registration   TEXT,
  aircraft       TEXT,
  event_date     TEXT,
  location       TEXT,
  narrative_text TEXT,
  source_tier    TEXT,
  lang           TEXT DEFAULT 'fr',
  status         TEXT DEFAULT 'new',
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS anacdz_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'DZ',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_anacdz_status ON anacdz_reports(status);
"""


def now():
    return int(time.time() * 1000)


def conn():
    os.makedirs(HOME, exist_ok=True)
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


# ---- OCR helpers ----

def _ocr_remote(pdf_path, lang, host):
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print(f"[anacdz ocr] scp failed rc={cp.returncode}", file=sys.stderr, flush=True)
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
        print(f"[anacdz ocr] remote exception: {e}", file=sys.stderr, flush=True)
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    import tempfile
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


def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


# ---- date parsing (French month names) ----

_FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_FR_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(sorted(_FR_MONTHS.keys(), key=len, reverse=True)) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
# "survenu le 10/08/2017" or "10-08-2017" or "10.08.2017"
_SURVENU_RE = re.compile(
    r"survenu\s+le\s+(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b",
    re.IGNORECASE,
)
_DATE_NUMERIC_RE = re.compile(
    r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.]((19|20)\d{2})\b"
)


def parse_date_from_url(url):
    """Extract date from the PDF filename (ANAC Algeria filenames contain dates)."""
    fn = url.rstrip("/").split("/")[-1]
    # Pattern: LE-DD-MOIS-YYYY or LE-DD-MONTHNAME-YYYY
    m = re.search(
        r"LE[_\-](\d{1,2})[_\-](" + "|".join(sorted(_FR_MONTHS.keys(), key=len, reverse=True)) + r")[_\-](\d{4})",
        fn, re.IGNORECASE,
    )
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _FR_MONTHS.get(month_str)
        if mo:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Numeric: DD-MM-YYYY in URL slug
    m = re.search(r"(\d{2})[_\-](\d{2})[_\-](\d{4})", fn)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Year-only from aout-2024 style
    m = re.search(r"(\d{4})", fn)
    if m:
        y = int(m.group(1))
        if 1990 <= y <= 2030:
            # try to extract month too
            m2 = re.search(
                r"[_\-](aout|août|mars|juin|mai|juillet|août|janvier|fevrier|février|"
                r"avril|septembre|octobre|novembre|decembre|décembre)[_\-]",
                fn, re.IGNORECASE,
            )
            if m2:
                mo_str = m2.group(1).lower()
                mo = _FR_MONTHS.get(mo_str)
                if mo:
                    return f"{y:04d}-{mo:02d}-01"
    return None


def parse_date(txt):
    if not txt:
        return None
    # Priority 1: "survenu le DD/MM/YYYY" — most reliable
    m = _SURVENU_RE.search(txt[:2000])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 2: French written month preceded by LE/le (event date context)
    # Avoid picking up "Établi le DD MONTH YYYY" (publication dates)
    m_event = re.search(
        r"(?:survenu|accident|incident)\s+[aàAÀ]\s+[^\n]{0,80}(?:le\s+)?(\d{1,2})\s+(" +
        "|".join(sorted(_FR_MONTHS.keys(), key=len, reverse=True)) +
        r")\s+(\d{4})\b",
        txt[:3000], re.IGNORECASE,
    )
    if m_event:
        d, month_str, y = int(m_event.group(1)), m_event.group(2).lower(), int(m_event.group(3))
        mo = _FR_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 3: Any French written month in cover area
    m = _FR_DATE_RE.search(txt[:3000])
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _FR_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 4: numeric DD/MM/YYYY
    m = _DATE_NUMERIC_RE.search(txt[:2000])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ---- registration extraction ----

# Algerian registrations: 7T-xxx; also foreign regs (N-xxxxx, F-xxxx, HB-xxx etc.)
_REG_RE = re.compile(
    r"\b(7T-[A-Z]{2,4}|N-?\d{2,5}[A-Z]{0,2}|F-[A-Z]{4}|HB-[A-Z]{3}|CS-[A-Z]{3}|TS-[A-Z]{3}|[A-Z]{2}-[A-Z]{3,4})\b",
    re.I,
)


def parse_reg(txt):
    if not txt:
        return None
    m = _REG_RE.search(txt[:4000])
    return m.group(1).upper() if m else None


def parse_aircraft(txt):
    if not txt:
        return None
    for pat in [
        r"(?:aéronef|aeronef|aircraft|avion|hélicoptère|helicoptere)\s+(?:de\s+type\s+)?([A-Za-z0-9][A-Za-z0-9 \-/]{2,35})[,\n\.]",
        r"(?:type\s+)([A-Za-z][A-Za-z0-9 \-/]{3,30})\s+immatricul[eé]",
        r"immatricul[eé]\s+(?:[^\s,]+)\s+(?:de\s+type\s+)?([A-Za-z][A-Za-z0-9 \-/]{3,30})[,\.\n]",
        r"\b(Bell\s+\d{3}[A-Z0-9 \-]*|Boeing\s+[A-Z]?\d{3}[A-Z0-9\- ]*|Airbus\s+A\d{3}[A-Za-z0-9 \-]*|Cessna\s+[A-Z]?\d{3}[A-Za-z0-9 ]*|Beechcraft\s+[A-Za-z0-9 \-]{3,30}|Robinson\s+R\d{2,3}|Piper\s+[A-Za-z0-9 \-]{3,25}|Daher\s+[A-Za-z0-9 \-]{3,20}|SF-50|Cirrus\s+[A-Za-z0-9 ]+)\b",
    ]:
        m = re.search(pat, txt[:4000], re.IGNORECASE)
        if m:
            ac = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(ac) > 2:
                return ac
    return None


def parse_location(txt):
    if not txt:
        return None
    for pat in [
        r"(?:survenu|accident)\s+[aà]\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})[,\.\n]",
        r"(?:survenu|accident)\s+[aà]\s+l[''](?:aéroport|aerodrome|aérodrome)\s+(?:d[e']?\s+)?([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})[,\.\n]",
        r"aéroport\s+(?:d[e']?\s+)?([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})[,\.\n]",
        r"aérodrome\s+(?:d[e']?\s+)?([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})[,\.\n]",
        r"à\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,30}),?\s+(?:Algérie|Algeria)",
    ]:
        m = re.search(pat, txt[:5000], re.IGNORECASE)
        if m:
            loc = re.split(r"[\n,\.]", m.group(1).strip())[0].strip()
            if len(loc) > 2:
                return loc
    return None


def parse_probable_cause(txt):
    if not txt:
        return None
    for pat in [
        r"causes?\s+probables?\s*:?\s*(.{60,800}?)(?:\n\n|\Z)",
        r"cause\s+de\s+l[''']accident\s*:?\s*(.{60,800}?)(?:\n\n|\Z)",
        r"Conclusions?\s*:?\s*(.{60,800}?)(?:\n\n|\Z)",
    ]:
        m = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
        if m:
            cause = re.sub(r"\s+", " ", m.group(1).strip())
            if len(cause) > 60:
                return cause[:1000]
    return None


def parse_operator(txt):
    if not txt:
        return None
    for pat in [
        r"(?:exploitant|opérateur|operator)\s*:?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \.,]{3,50})[,\n]",
        r"(?:appartenant\s+[aà]|propriété\s+de)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \.,]{3,50})[,\n\.]",
    ]:
        m = re.search(pat, txt[:4000], re.IGNORECASE)
        if m:
            op = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(op) > 2:
                return op
    return None


# ---- case_id derivation ----

def case_id_from_url(url):
    """Stable intrinsic case_id from PDF filename (never order-dependent)."""
    fn = url.rstrip("/").split("/")[-1]
    fn = re.sub(r"\.pdf$", "", fn, flags=re.I)
    # URL-decode
    try:
        from urllib.parse import unquote
        fn = unquote(fn)
    except Exception:
        pass
    # Normalize
    s = re.sub(r"[^A-Za-z0-9]+", "-", fn).strip("-").lower()
    return "anacdz-" + s[:80]


def classify_report_type(url, anchor):
    u = (url or "").lower()
    a = (anchor or "").lower()
    if "final" in u or "final" in a:
        return "Final report"
    if "incident-grave" in u or "incident grave" in a or "incident" in u:
        return "Serious incident report"
    return "Investigation report"


def location_from_url(url):
    """Extract location hint from ANAC DZ filename patterns."""
    fn = url.rstrip("/").split("/")[-1]
    fn_upper = fn.upper()
    # Patterns: "A-LAEROPORT-D-EL-OUED-GUEMAR", "A-DOUERA", "A-LAEROPORT-DE-OUARGLA-AIN-BEIDA"
    # Extract location keyword after common prefixes
    for pat in [
        r"A-LAEROPORT-D[EI]?-([A-Z][A-Z0-9\-]{2,40}?)(?:-LE-|\.\s*$|\.PDF|$)",
        r"A-LAEROPORT-D-([A-Z][A-Z0-9\-]{2,40}?)(?:-LE-|\.\s*$|\.PDF|$)",
        r"A-([A-Z][A-Z0-9\-]{2,35}?)(?:-LE-|-SURVENU|\.PDF|$)",
        r"NIVEAU-DE-LAEROPORT-D-([A-Z][A-Z0-9\-]{2,40}?)(?:-LE-|\.\s*$|\.PDF|$)",
    ]:
        m = re.search(pat, fn_upper)
        if m:
            raw = m.group(1).replace("-", " ").strip()
            if len(raw) > 2:
                return raw.title()
    return None


# ---- discover ----

def discover(c, cl):
    print(f"[anacdz discover] GET {LISTING}", flush=True)
    try:
        r = cl.get(LISTING)
    except Exception as e:
        print(f"[anacdz discover] request failed: {e}", file=sys.stderr, flush=True)
        return 0
    time.sleep(DELAY)
    if r.status_code != 200:
        print(f"[anacdz discover] HTTP {r.status_code}", file=sys.stderr, flush=True)
        return 0

    # Find all PDF hrefs
    pdf_re = re.compile(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', re.I)
    inserted = 0
    seen = set()
    for m in pdf_re.finditer(r.text):
        raw_url = m.group(1).strip()
        # Force HTTPS; normalise domain
        url = re.sub(r"^http://", "https://", raw_url)
        url = re.sub(r"https://anac\.dz", "https://anac.dz", url)
        if not url.startswith("http"):
            url = "https://anac.dz" + url

        if url in seen:
            continue
        seen.add(url)

        # Skip non-investigation PDFs (policy docs etc.)
        fn_lower = url.lower()
        if "politique" in fn_lower or "utilisation" in fn_lower or "site-web" in fn_lower:
            print(f"[anacdz discover] skip (policy): {url.split('/')[-1][:60]}", flush=True)
            continue

        cid = case_id_from_url(url)
        if c.execute("SELECT 1 FROM anacdz_reports WHERE case_id=?", (cid,)).fetchone():
            print(f"[anacdz discover] already known: {cid}", flush=True)
            continue

        rtype = classify_report_type(url, "")
        loc = location_from_url(url)
        date_hint = parse_date_from_url(url)

        c.execute(
            "INSERT OR IGNORE INTO anacdz_reports "
            "(case_id,pdf_url,report_type,location,event_date,lang,status,discovered_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, url, rtype, loc, date_hint, "fr", "new", now(), now()),
        )
        c.commit()
        inserted += 1
        print(f"[anacdz discover] {cid} | {rtype} | {url.split('/')[-1][:70]}", flush=True)

    return inserted


# ---- fetch ----

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, pdf_url FROM anacdz_reports WHERE status='new'"
    ).fetchall()
    done = 0
    for row in rows:
        cid, url = row["case_id"], row["pdf_url"]
        dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", cid) + ".pdf")
        try:
            time.sleep(DELAY)
            resp = cl.get(url)
            ct = resp.headers.get("content-type", "")
            if resp.content[:4] != b"%PDF" and "pdf" not in ct.lower():
                raise ValueError(f"not a PDF (ct={ct} status={resp.status_code})")
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            sz = os.path.getsize(dest)
            c.execute(
                "UPDATE anacdz_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            done += 1
            print(f"[anacdz fetch] {cid}: {sz//1024}KB", flush=True)
        except Exception as e:
            print(f"[anacdz fetch] {cid}: {e}", file=sys.stderr, flush=True)
    return done


# ---- parse ----

def _parse_one(cid, pdf_path, use_ocr=False):
    txt = extract_text(pdf_path)
    if len(txt) >= FLOOR:
        return txt, "pdf"
    if use_ocr and pdf_path and os.path.exists(pdf_path):
        print(f"[anacdz parse] {cid}: pdftotext={len(txt)} chars → OCR ({OCR_LANG})", flush=True)
        ocr_txt = ocr_extract(pdf_path, lang=OCR_LANG)
        if len(ocr_txt) >= FLOOR:
            print(f"[anacdz parse] {cid}: OCR yielded {len(ocr_txt)} chars", flush=True)
            return ocr_txt, "ocr"
        print(f"[anacdz parse] {cid}: OCR too short ({len(ocr_txt)} chars)", file=sys.stderr, flush=True)
    tier = "scanned" if (pdf_path and os.path.exists(pdf_path)) else "none"
    return txt, tier


def parse(c, use_ocr=True):
    rows = c.execute(
        "SELECT case_id, pdf_path, registration, event_date, location "
        "FROM anacdz_reports WHERE status='fetched'"
    ).fetchall()
    for row in rows:
        cid = row["case_id"]
        txt, tier = _parse_one(cid, row["pdf_path"], use_ocr=use_ocr)
        reg = row["registration"] or parse_reg(txt)
        date = row["event_date"] or parse_date(txt)
        loc = row["location"] or parse_location(txt)
        c.execute(
            "UPDATE anacdz_reports "
            "SET narrative_text=?,source_tier=?,registration=?,event_date=?,location=?,"
            "status='parsed',updated_at=? WHERE case_id=?",
            (txt, tier, reg, date, loc, now(), cid),
        )
        c.commit()
        print(f"[anacdz parse] {cid}: tier={tier} len={len(txt)} date={date} reg={reg}", flush=True)
    return len(rows)


# ---- build ----

def build(c):
    rows = c.execute(
        "SELECT case_id,event_date,aircraft,registration,location,narrative_text,"
        "source_tier,report_type,pdf_url,lang FROM anacdz_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    skipped = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        tier = r["source_tier"] or ""

        if tier not in ("pdf", "ocr") or len(narr) < FLOOR:
            skipped += 1
            c.execute(
                "UPDATE anacdz_reports SET status='skipped',updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            print(f"[anacdz build] SKIP {r['case_id']}: tier={tier} len={len(narr)}", flush=True)
            continue

        event_date = r["event_date"]
        registration = r["registration"]
        aircraft = r["aircraft"]
        location = r["location"]

        # Enrich from narrative if still missing
        if tier in ("pdf", "ocr"):
            if not event_date:
                event_date = parse_date(narr)
            if not registration:
                registration = parse_reg(narr)
            if not aircraft:
                aircraft = parse_aircraft(narr)
            if not location:
                location = parse_location(narr)

        operator = parse_operator(narr)
        probable_cause = parse_probable_cause(narr)

        # site_slug: stable, uses registration+location or case_id fallback
        parts = []
        if registration:
            parts.append(registration.lower().replace("-", ""))
        if location:
            parts.append(re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-")[:30])
        if event_date:
            parts.append(event_date[:4])
        slug_base = "-".join(parts) if parts else r["case_id"]
        slug = re.sub(r"-+", "-", slug_base).strip("-")[:80]

        c.execute(
            "INSERT OR REPLACE INTO anacdz_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            " narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], event_date, aircraft, registration, operator,
             location, "DZ", narr, probable_cause, r["pdf_url"],
             r["report_type"] or "Investigation report",
             slug, r["lang"] or "fr", now()),
        )
        c.execute(
            "UPDATE anacdz_reports SET status='built',updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1
        print(f"[anacdz build] {r['case_id']}: date={event_date} reg={registration} "
              f"loc={location} acft={aircraft} narr={len(narr)} tier={tier}", flush=True)

    return built, skipped


# ---- main ----

def main():
    import warnings
    warnings.filterwarnings("ignore")
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "fetch", "all"):
        cl = http()
        try:
            if mode in ("discover", "all"):
                n = discover(c, cl)
                print(f"discovered: {n}", flush=True)
            if mode in ("fetch", "all"):
                n = fetch(c, cl)
                print(f"fetched: {n}", flush=True)
        finally:
            cl.close()

    if mode in ("parse", "all"):
        n = parse(c, use_ocr=True)
        print(f"parsed: {n}", flush=True)

    if mode in ("build", "all"):
        b, sk = build(c)
        print(f"built: {b}  skipped: {sk}", flush=True)

    print("\nreports status:", list(c.execute(
        "SELECT status, count(*) FROM anacdz_reports GROUP BY status"
    ).fetchall()), flush=True)
    acc_count = c.execute("SELECT count(*) FROM anacdz_accidents").fetchone()[0]
    print(f"accidents: {acc_count}", flush=True)

    if mode in ("all", "build"):
        print("\n--- anacdz_accidents summary ---")
        for row in c.execute(
            "SELECT case_id, event_date, registration, location, site_slug, "
            "LENGTH(narrative_text) AS nl FROM anacdz_accidents ORDER BY event_date"
        ):
            print(f"  {row[0]!s:65s} | {row[1]!s:12s} | {row[2]!s:10s} | {row[3]!s:30s} | {row[4]!s:35s} | {row[5]}")


if __name__ == "__main__":
    main()
