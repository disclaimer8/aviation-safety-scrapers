#!/usr/bin/env python3
"""Mexico AFAC (Agencia Federal de Aviacion Civil) 'Informes Finales' ingest --
patchright transport (gob.mx F5/Shape passes headed). Hub
gob.mx/afac/acciones-y-programas/informes-finales-251241 -> per-year sub-pages
-> report PDFs at gob.mx/cms/uploads/attachment/file/<id>/<reg>-<ddmmyyyy>.pdf.
Spanish narratives. Stages: discover|fetch|parse|build|redate (resumable)."""
import sys, os, re, time, base64, sqlite3, subprocess, shlex, tempfile, uuid

BASE="https://www.gob.mx"
HUB=BASE+"/afac/acciones-y-programas/informes-finales-251241"
DELAY=2.5
MIN_NARRATIVE=600
FLOOR=80
HOME=os.path.expanduser("~/mexico-ingest")
DB=os.path.join(HOME,"mexico.db")
PDFDIR=os.path.join(HOME,"pdfs")
PROFILE=os.path.join(HOME,".cf-profile")

SCHEMA="""
CREATE TABLE IF NOT EXISTS mexico_reports (
  case_id TEXT PRIMARY KEY, pdf_url TEXT, pdf_path TEXT, title TEXT,
  report_type TEXT, registration TEXT, event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT, lang TEXT DEFAULT 'es',
  status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS mexico_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'MX', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_mexico_status ON mexico_reports(status);
"""
def now(): return int(time.time()*1000)
def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c

# ---------------------------------------------------------------------------
# OCR support -- adapted from ~/aaid-ingest/aaid_ingest/pdf.py
# Remote OCR via OCR_REMOTE=<ocr-host> (runs as a1 via ssh key).
# Never run ocrmypdf locally on this loaded mini-PC; always use the remote path.
# ---------------------------------------------------------------------------

def _ocr_remote(pdf_path, lang, host):
    """Ship PDF to remote host, OCR it there (niced), return text or ''."""
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


def ocr_extract(pdf_path, lang="spa"):
    """OCR a scanned PDF. Uses OCR_REMOTE env (remote hetzner) when set.
    Returns '' on any failure. 600s per-PDF timeout on remote.
    MUST run as a1 (ssh key is authorized on hetzner for a1 only)."""
    if not pdf_path or not os.path.exists(str(pdf_path)):
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    # Local OCR fallback (only if OCR_REMOTE not set -- should not happen in prod)
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
    if not path or not os.path.exists(path): return ""
    try: out=subprocess.run(["pdftotext","-q",str(path),"-"],capture_output=True,timeout=180)
    except Exception: return ""
    return out.stdout.decode("utf-8","replace").strip() if out.returncode==0 else ""

def slugify(*parts):
    s=re.sub(r"[^A-Za-z0-9]+","-"," ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

_REG_RE=re.compile(r"\b(X[A-C]-?[A-Z]{3})\b", re.I)
_FNDATE_RE=re.compile(r"(\d{2})(\d{2})(20\d{2})")
def case_from_pdf(url):
    stem=re.sub(r"\.pdf$","",url.split("/")[-1],flags=re.I)
    return stem.lower() or None
def reg_from(text):
    m=_REG_RE.search(text or "")
    if not m: return None
    r=m.group(1).upper().replace("-","")
    return r[:2]+"-"+r[2:]
def date_from_fname(stem):
    m=_FNDATE_RE.search(stem or "")
    if m: return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None

# ---------------------------------------------------------------------------
# Spanish (+ English fallback) date extraction from PDF text
# ---------------------------------------------------------------------------

_MONTH_MAP = {
    'enero': '01', 'ene': '01',
    'febrero': '02', 'feb': '02',
    'marzo': '03', 'mar': '03',
    'abril': '04', 'abr': '04',
    'mayo': '05', 'may': '05',
    'junio': '06', 'jun': '06',
    'julio': '07', 'jul': '07',
    'agosto': '08', 'ago': '08',
    'septiembre': '09', 'sep': '09', 'sept': '09',
    'octubre': '10', 'oct': '10',
    'noviembre': '11', 'nov': '11',
    'diciembre': '12', 'dic': '12',
    # English months
    'january': '01', 'jan': '01',
    'february': '02',
    'march': '03',
    'april': '04',
    'june': '06',
    'july': '07',
    'august': '08',
    'september': '09',
    'october': '10',
    'november': '11',
    'december': '12',
}

_MONTHS_ES_LONG = (
    'enero|febrero|marzo|abril|mayo|junio|julio|agosto'
    '|septiembre|octubre|noviembre|diciembre'
)
_MONTHS_ES_SHORT = 'ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic'
_MONTHS_ES_ALL = _MONTHS_ES_LONG + '|' + _MONTHS_ES_SHORT

# Normal Spanish long-form: "10 de mayo de 1990", "24 DE AGOSTO DE 1992"
_DATE_LONG_RE = re.compile(
    r'\b(\d{1,2})\s+de\s+(' + _MONTHS_ES_ALL + r')\w*\s+de\s+((?:19|20)\d{2})\b',
    re.IGNORECASE,
)

# Fully-collapsed spaced OCR: "09deDiciembrede2012"
_DATE_COLLAPSED_RE = re.compile(
    r'\b(\d{1,2})de(' + _MONTHS_ES_LONG + r')de((19|20)\d{2})\b',
    re.IGNORECASE,
)

# English: "December 9, 2012" or "9 December 2012"
_DATE_EN_MDY_RE = re.compile(
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b',
    re.IGNORECASE,
)
_DATE_EN_DMY_RE = re.compile(
    r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s+((?:19|20)\d{2})\b',
    re.IGNORECASE,
)

# Numeric: DD/MM/YYYY or DD-MM-YYYY
_DATE_NUM_RE = re.compile(r'\b(\d{2})[/\-](\d{2})[/\-]((?:19|20)\d{2})\b')

# Context keywords that indicate an event date (higher priority) -- Spanish + English
_EVENT_CONTEXT = re.compile(
    r'(fecha\s+del?\s+(?:accidente|suceso|evento|ocurrencia)'
    r'|(?:el\s+)?d[i\xed]a\s+del?\s+accidente'
    r'|ocurri[o\xf3]\s+el\s+d[i\xed]a'
    r'|(?:el\s+)?accidente\s+ocurri[o\xf3]'
    r'|hora\s+local[,;:\s]'
    r'|se\s+accident[o\xf3]\s+el\s+d[i\xed]a'
    r'|^\s*(?:date|date:)\s*$'
    r'|\baccident\s+date\b'
    r'|\bon\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d)',
    re.IGNORECASE | re.MULTILINE,
)

# Standalone FECHA line (spaced OCR reports split "FECHA / DEL ACCIDENTE" across lines)
_FECHA_STANDALONE = re.compile(r'^\s*FECHA\s*$')

# Lines that indicate publication/expiry dates (to deprioritise)
_PUBL_CONTEXT = re.compile(
    r'(expedid[ao]|expedici[o\xf3]n|vigente\s+al|vigencia|publicaci[o\xf3]n'
    r'|report\s+date|date\s+issued|date\s+of\s+report)',
    re.IGNORECASE,
)


def _collapse_lines(text):
    """Per-line aggressive collapse for spaced OCR text.
    Detects lines where >40% of tokens are single chars (OCR artefact) and
    removes intra-line spaces, turning '0 9 d e D ic ie m b re d e 2 0 1 2'
    into '09deDiciembrede2012'."""
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        tokens = stripped.split()
        if len(tokens) >= 4:
            single_char = sum(1 for t in tokens if len(t) == 1)
            if single_char / len(tokens) > 0.4:
                collapsed = re.sub(
                    r'(?<=[A-Za-z\xe1\xe9\xed\xf3\xfa\xf1\xfc0-9])\s+'
                    r'(?=[A-Za-z\xe1\xe9\xed\xf3\xfa\xf1\xfc0-9])', '', stripped
                )
                result.append(collapsed)
                continue
        result.append(line)
    return '\n'.join(result)


def _parse_es_match(m):
    """Convert a Spanish date match (day, month_name, year) to YYYY-MM-DD or None."""
    try:
        day = m.group(1).strip()
        month_raw = m.group(2).strip().lower()
        year = m.group(3).strip()
        month_num = None
        for key in sorted(_MONTH_MAP.keys(), key=len, reverse=True):
            if month_raw.startswith(key):
                month_num = _MONTH_MAP[key]
                break
        if not month_num:
            return None
        d, y = int(day), int(year)
        if not (1 <= d <= 31) or not (1970 <= y <= 2030):
            return None
        return f"{y:04d}-{month_num}-{d:02d}"
    except Exception:
        return None


def _parse_en_mdy(m):
    """Convert English Month DD, YYYY match to YYYY-MM-DD or None."""
    try:
        month_raw = m.group(1).strip().lower()
        day = m.group(2).strip()
        year = m.group(3).strip()
        month_num = _MONTH_MAP.get(month_raw)
        if not month_num:
            return None
        d, y = int(day), int(year)
        if not (1 <= d <= 31) or not (1970 <= y <= 2030):
            return None
        return f"{y:04d}-{month_num}-{d:02d}"
    except Exception:
        return None


def _parse_en_dmy(m):
    """Convert English DD Month YYYY match to YYYY-MM-DD or None."""
    try:
        day = m.group(1).strip()
        month_raw = m.group(2).strip().lower()
        year = m.group(3).strip()
        month_num = _MONTH_MAP.get(month_raw)
        if not month_num:
            return None
        d, y = int(day), int(year)
        if not (1 <= d <= 31) or not (1970 <= y <= 2030):
            return None
        return f"{y:04d}-{month_num}-{d:02d}"
    except Exception:
        return None


def _try_date_in_text(txt):
    """Try all date regexes on a text snippet. Returns first match or None.
    Tries Spanish normal, Spanish per-line-collapsed, Spanish fully-collapsed,
    English MDY, English DMY."""
    # Spanish normal spaced form
    m = _DATE_LONG_RE.search(txt)
    if m:
        dt = _parse_es_match(m)
        if dt:
            return dt
    # Per-line collapsed (for spaced OCR: '0 9 d e D ic ie m b re')
    collapsed = _collapse_lines(txt)
    m = _DATE_LONG_RE.search(collapsed)
    if m:
        dt = _parse_es_match(m)
        if dt:
            return dt
    # Fully-collapsed (no spaces at all)
    m = _DATE_COLLAPSED_RE.search(collapsed)
    if m:
        day, month_raw, year = m.group(1), m.group(2), m.group(3)
        month_num = None
        for key in sorted(_MONTH_MAP.keys(), key=len, reverse=True):
            if month_raw.lower().startswith(key):
                month_num = _MONTH_MAP[key]
                break
        if month_num:
            try:
                d, y = int(day), int(year)
                if 1 <= d <= 31 and 1970 <= y <= 2030:
                    return f"{y:04d}-{month_num}-{d:02d}"
            except Exception:
                pass
    # English MDY: "December 9, 2012"
    m = _DATE_EN_MDY_RE.search(txt)
    if m:
        dt = _parse_en_mdy(m)
        if dt:
            return dt
    # English DMY: "9 December 2012"
    m = _DATE_EN_DMY_RE.search(txt)
    if m:
        dt = _parse_en_dmy(m)
        if dt:
            return dt
    return None


def event_date_from_text(text):
    """Extract accident/event date from Spanish (or English) PDF text.
    Returns YYYY-MM-DD or None.

    Strategy:
    1. Dates near explicit event-context keywords (highest priority).
    2. FECHA standalone line with DEL ACCIDENTE in window (spaced OCR headers).
    3. First long-form date in header (first 200 lines), skipping publication lines.
    4. First long-form date anywhere, skipping publication lines.
    5. Fallback: first date anywhere (including publication dates).
    6. Numeric date near context keyword.
    """
    if not text:
        return None

    lines = text.split('\n')

    # Pass 1: dates near an event-context keyword (first 600 lines)
    for i, line in enumerate(lines[:600]):
        if _PUBL_CONTEXT.search(line):
            continue
        if _EVENT_CONTEXT.search(line):
            window = '\n'.join(lines[i:i+6])
            dt = _try_date_in_text(window)
            if dt:
                return dt

    # Pass 2: FECHA standalone line (spaced OCR header with "FECHA / DEL ACCIDENTE")
    for i, line in enumerate(lines[:400]):
        if _FECHA_STANDALONE.match(line) or line.strip() == 'FECHA':
            window_lines = lines[i:i+10]
            window = '\n'.join(window_lines)
            collapsed_window = _collapse_lines(window)
            if re.search(r'(del?\s*accidente|DELACCIDENTE)', collapsed_window, re.IGNORECASE):
                dt = _try_date_in_text(window)
                if dt:
                    return dt

    # Pass 3: first date in header (first 200 lines), skip publication lines
    head_lines = [l for l in lines[:200] if not _PUBL_CONTEXT.search(l)]
    head = '\n'.join(head_lines)
    dt = _try_date_in_text(head)
    if dt:
        return dt

    # Pass 4: first date anywhere, skip publication lines
    clean_lines = [l for l in lines if not _PUBL_CONTEXT.search(l)]
    full = '\n'.join(clean_lines)
    dt = _try_date_in_text(full)
    if dt:
        return dt

    # Pass 5: fallback -- first date anywhere (including publication dates)
    dt = _try_date_in_text(text)
    if dt:
        return dt

    # Pass 6: numeric date near context keyword
    for i, line in enumerate(lines[:600]):
        if _EVENT_CONTEXT.search(line):
            window = '\n'.join(lines[i:i+4])
            m = _DATE_NUM_RE.search(window)
            if m:
                try:
                    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if 1 <= d <= 31 and 1 <= mo <= 12 and 1970 <= y <= 2030:
                        return f"{y:04d}-{mo:02d}-{d:02d}"
                except Exception:
                    pass

    return None


_FETCH_JS="""async (u)=>{try{const r=await fetch(u,{credentials:'include'});if(!r.ok)return{ok:false,status:r.status};
 const b=await r.arrayBuffer();const a=new Uint8Array(b);let s='';const C=0x8000;
 for(let i=0;i<a.length;i+=C){s+=String.fromCharCode.apply(null,a.subarray(i,i+C));}return{ok:true,b64:btoa(s)};}
 catch(e){return{ok:false,status:'err:'+e};}}"""
class Browser:
    def __init__(self):
        from patchright.sync_api import sync_playwright
        self._pw=sync_playwright().__enter__()
        self.ctx=self._pw.chromium.launch_persistent_context(PROFILE,headless=False,args=["--disable-dev-shm-usage"])
        self.page=self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
    def close(self):
        try:self.ctx.close()
        except Exception:pass
        try:self._pw.__exit__(None,None,None)
        except Exception:pass
    def goto(self,url):
        self.page.goto(url,wait_until="domcontentloaded",timeout=45000)
        for _ in range(20):
            if "loading" not in self.page.title().lower(): break
            self.page.wait_for_timeout(1000)
        try:self.page.wait_for_load_state("networkidle",timeout=12000)
        except Exception:pass
    def anchors(self):
        return self.page.eval_on_selector_all("a[href]","els=>els.map(e=>({h:e.href,t:(e.innerText||'').trim().slice(0,90)}))")
    def fetch(self,url): return self.page.evaluate(_FETCH_JS,url)

def discover(c, br):
    br.goto(HUB); time.sleep(DELAY)
    hub_items=br.anchors()
    years={HUB}
    for i in hub_items:
        if re.search(r"/afac/acciones-y-programas/informes-finales-",i["h"]): years.add(i["h"].split("?")[0])
    print("year-pages:",len(years),file=sys.stderr)
    ins=0
    for yurl in sorted(years):
        try: br.goto(yurl); time.sleep(DELAY)
        except Exception as e: print("[mx year]",yurl,e,file=sys.stderr); continue
        for i in br.anchors():
            h=i["h"]
            if "/cms/uploads/" not in h or ".pdf" not in h.lower(): continue
            cid=case_from_pdf(h)
            if not cid: continue
            if c.execute("SELECT 1 FROM mexico_reports WHERE case_id=?",(cid,)).fetchone(): continue
            reg=reg_from(i["t"]) or reg_from(cid)
            ev=date_from_fname(cid)
            c.execute("INSERT OR IGNORE INTO mexico_reports (case_id,pdf_url,title,report_type,registration,event_date,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (cid,h,i["t"],"Final report",reg,ev,'new',now(),now())); c.commit(); ins+=1
    return ins

def fetch(c, br):
    rows=c.execute("SELECT case_id,pdf_url FROM mexico_reports WHERE status='new'").fetchall()
    if rows:
        try: br.goto(HUB)
        except Exception: pass
    done=0; fails=0
    for row in rows:
        try:
            dest=os.path.join(PDFDIR,re.sub(r"[^A-Za-z0-9_.-]","_",row["case_id"])+".pdf")
            res=br.fetch(row["pdf_url"])
            if not res.get("ok"):
                br.goto(HUB); time.sleep(1); res=br.fetch(row["pdf_url"])
            pdf_path=None
            if res.get("ok") and res.get("b64"):
                with open(dest,"wb") as fh: fh.write(base64.b64decode(res["b64"])); pdf_path=dest
            c.execute("UPDATE mexico_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",(pdf_path,now(),row["case_id"])); c.commit()
            done+=1; fails=0; time.sleep(DELAY)
        except Exception as e:
            print("[mx fetch]",row["case_id"],e,file=sys.stderr); fails+=1
            if fails>=5: break
    return done


def parse(c):
    """Parse fetched PDFs. For scanned (image-only) PDFs, falls back to OCR via
    ocr_extract() with lang='spa'. OCR_REMOTE env must be set to use remote hetzner.
    Also re-parses previously skipped rows that had scanned tier (OCR retry)."""
    rows = c.execute(
        "SELECT case_id,pdf_path,registration,event_date FROM mexico_reports "
        "WHERE status='fetched' OR (status='skipped' AND source_tier='scanned')"
    ).fetchall()
    parsed = 0
    ocr_count = 0
    for r in rows:
        txt = extract_text(r["pdf_path"])
        tier = "pdf"
        if len(txt) < FLOOR:
            # pdftotext yielded too little -- try OCR (remote hetzner via OCR_REMOTE)
            ocr_txt = ocr_extract(r["pdf_path"], lang="spa")
            if len(ocr_txt) >= FLOOR:
                txt = ocr_txt
                tier = "ocr"
                ocr_count += 1
                print(f"[mx ocr] {r['case_id']} => {len(txt)} chars", file=sys.stderr)
            else:
                tier = "scanned"  # OCR also failed (truly hopeless scan)
        ev = r["event_date"] or event_date_from_text(txt)
        reg = r["registration"] or reg_from(txt)
        c.execute(
            "UPDATE mexico_reports SET narrative_text=?,source_tier=?,"
            "event_date=COALESCE(?,event_date),"
            "registration=COALESCE(?,registration),status='parsed',updated_at=? WHERE case_id=?",
            (txt, tier, ev, reg, now(), r["case_id"])
        )
        c.commit()
        parsed += 1
    print(f"[mx parse] {parsed} parsed, {ocr_count} via OCR", file=sys.stderr)
    return parsed


def build(c):
    """Build mexico_accidents from parsed rows. Re-builds previously skipped
    rows that are now parseable after OCR. Does NOT change case_id or site_slug
    for existing built rows (SEO slugs are stable)."""
    rows = c.execute(
        "SELECT * FROM mexico_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    skipped = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute("UPDATE mexico_reports SET status='skipped',updated_at=? WHERE case_id=?",
                      (now(), r["case_id"]))
            c.commit()
            skipped += 1
            continue
        ev = r["event_date"] or event_date_from_text(narr)
        c.execute("""INSERT OR REPLACE INTO mexico_accidents
          (case_id,event_date,aircraft,registration,operator,location,country,narrative_text,
           probable_cause,source_url,report_type,site_slug,lang,built_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (r["case_id"], ev, None, r["registration"], None, r["location"], "MX",
           narr, None, r["pdf_url"], r["report_type"],
           slugify(r["registration"], r["case_id"]), "es", now()))
        c.execute("UPDATE mexico_reports SET status='built',updated_at=? WHERE case_id=?",
                  (now(), r["case_id"]))
        c.commit()
        built += 1
    print(f"[mx build] {built} built, {skipped} skipped", file=sys.stderr)
    return built


def redate(c):
    """Re-extract event_date for all built rows whose event_date is NULL.
    Reads stored narrative_text -- no PDF re-read needed.
    Updates both mexico_reports and mexico_accidents."""
    rows = c.execute(
        "SELECT case_id, narrative_text, event_date FROM mexico_reports WHERE status='built'"
    ).fetchall()
    updated = 0
    for r in rows:
        if r["event_date"]:
            continue  # Already has a date -- don't overwrite
        dt = event_date_from_text(r["narrative_text"])
        if dt:
            c.execute("UPDATE mexico_reports SET event_date=?,updated_at=? WHERE case_id=?",
                      (dt, now(), r["case_id"]))
            c.execute("UPDATE mexico_accidents SET event_date=? WHERE case_id=?",
                      (dt, r["case_id"]))
            c.commit()
            updated += 1
            print(f"[mx redate] {r['case_id']} => {dt}", file=sys.stderr)
    print(f"[mx redate] {updated}/{len(rows)} rows got event_date", file=sys.stderr)
    return updated


def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    os.makedirs(PDFDIR,exist_ok=True); c=conn()
    if mode in ("discover","fetch","all"):
        br=Browser()
        try:
            if mode in ("discover","all"): print("discovered:",discover(c,br))
            if mode in ("fetch","all"): print("fetched:",fetch(c,br))
        finally: br.close()
    if mode in ("parse","all"): print("parsed:",parse(c))
    if mode in ("build","all"): print("built:",build(c))
    if mode in ("redate","all"): print("redated:",redate(c))
    print("reports:",list(c.execute("SELECT status,count(*) FROM mexico_reports GROUP BY status")))
    print("accidents:",c.execute("SELECT count(*) FROM mexico_accidents").fetchone()[0])

if __name__=="__main__": main()
