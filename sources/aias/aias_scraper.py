#!/usr/bin/env python3
"""AIAS (Romania) accident/incident report ingest — patchright CF transport.
Source: aias.gov.ro WP custom post-type `investigatii` (233 posts), report PDFs
in /wp-content/uploads/YYYY/MM/<YYYYMMDD>_<TYPE>_<REG>_<LANG>.pdf (TYPE BI/RF,
LANG RO/EN). Same-origin → in-page fetch works directly. Mostly Romanian.

Stages: discover (sitemap -> aias_reports) | fetch (detail -> report PDF) |
parse (pdftotext) | build (aias_accidents). Resumable via status column.
"""
import sys, os, re, time, base64, sqlite3, subprocess

BASE = "https://aias.gov.ro"
SITEMAP = BASE + "/wp-sitemap-posts-investigatii-1.xml"
DELAY = 2.0
MIN_NARRATIVE = 600
FLOOR = 80
HOME = os.path.expanduser("~/aias-ingest")
DB = os.path.join(HOME, "aias.db")
PDFDIR = os.path.join(HOME, "pdfs")
PROFILE = os.path.join(HOME, ".cf-profile")

SCHEMA = """
CREATE TABLE IF NOT EXISTS aias_reports (
  case_id TEXT PRIMARY KEY, report_url TEXT, pdf_url TEXT, pdf_path TEXT,
  title TEXT, report_type TEXT, aircraft TEXT, registration TEXT,
  event_date TEXT, location TEXT, narrative_text TEXT, source_tier TEXT,
  lang TEXT, status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS aias_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'RO', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_aias_status ON aias_reports(status);
"""

def now(): return int(time.time()*1000)
def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c

def extract_text(path):
    if not path or not os.path.exists(path): return ""
    try:
        out = subprocess.run(["pdftotext","-q",str(path),"-"], capture_output=True, timeout=180)
    except Exception: return ""
    return out.stdout.decode("utf-8","replace").strip() if out.returncode==0 else ""

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+","-"," ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

_TYPE_MAP = [("accident","Accident"),("incident grav","Serious incident"),
             ("serious incident","Serious incident"),("incident","Incident")]
def map_type(s):
    sl=(s or "").lower()
    for k,v in _TYPE_MAP:
        if k in sl: return v
    return None

def parse_title(title):
    """'2024.05.23 Incident grav, Belmont DW 200, YR-5699 , Valea Ursului, ... , România - AIAS'"""
    t = re.sub(r"\s*[-–]\s*AIAS\s*$","",title or "").strip()
    md = re.match(r"^\s*(\d{4})[.\-/](\d{2})[.\-/](\d{2})\s*(.*)$", t)
    event_date=None; rest=t
    if md:
        event_date=f"{md.group(1)}-{md.group(2)}-{md.group(3)}"; rest=md.group(4).strip()
    parts=[p.strip() for p in rest.split(",") if p.strip()]
    report_type = map_type(parts[0]) if parts else None
    aircraft = parts[1] if len(parts)>1 else None
    registration = parts[2] if len(parts)>2 else None
    # location = remaining joined minus trailing Romania
    loc_parts=[p for p in parts[3:] if p.lower() not in ("romania","românia")]
    location = ", ".join(loc_parts) or None
    # registration sanity: looks like a reg (has a dash + alnum) else try regex over title
    if not registration or not re.match(r"^[A-Z0-9]{1,2}-?[A-Z0-9]{2,}$", registration.replace(" ","")):
        m=re.search(r"\b([A-Z]{1,2}-[A-Z0-9]{2,5})\b", t)
        registration = m.group(1) if m else registration
    return event_date, report_type, aircraft, (registration.replace(" ","") if registration else None), location

# report PDF basename pattern: <date>_<letters>_<reg>_<lang>.pdf ; exclude guides
# Handles both YYYYMMDD_ and YYYY.MM.DD_ date prefixes
_REPORT_RE = re.compile(r"/((?:\d{4}[.\-]?\d{2}[.\-]?\d{2})_[^/\"]+?\.pdf)", re.I)
# Also match guidebooks/nav PDFs to EXCLUDE them
_NAV_PDF_RE = re.compile(r"/(Ghid-|Raportarea-|formulare)", re.I)
def pick_report_pdf(hrefs):
    cands=[h for h in hrefs if _REPORT_RE.search(h) and not _NAV_PDF_RE.search(h)]
    if not cands: return None,None
    # absolute
    def absu(h): return h if h.startswith("http") else BASE+h
    en=[h for h in cands if re.search(r"_EN\.pdf$",h,re.I)]
    ro=[h for h in cands if re.search(r"_RO\.pdf$",h,re.I)]
    if en: return absu(en[0]),"en"
    if ro: return absu(ro[0]),"ro"
    return absu(cands[0]),"ro"

def case_id_from_pdf(url):
    m=_REPORT_RE.search(url)
    if not m: return None
    stem=re.sub(r"\.pdf$","",m.group(1),flags=re.I)
    stem=re.sub(r"_(EN|RO)$","",stem,flags=re.I)
    # normalize date part: replace dots with hyphens for consistency
    stem=re.sub(r"^(\d{4})\.(\d{2})\.(\d{2})", r"\1-\2-\3", stem)
    return stem  # e.g. 20240523_BI_YR-5699 or 2018-08-22_RF_YR-5202

# ---------- patchright transport ----------
_FETCH_JS = """async (u) => { try { const r = await fetch(u,{credentials:'include'});
  if(!r.ok) return {ok:false,status:r.status};
  const ct=r.headers.get('content-type')||'';
  if(ct.indexOf('pdf')>=0){ const b=await r.arrayBuffer(); const a=new Uint8Array(b);
    let s='';const C=0x8000; for(let i=0;i<a.length;i+=C){s+=String.fromCharCode.apply(null,a.subarray(i,i+C));}
    return {ok:true,b64:btoa(s)}; }
  return {ok:true,text:await r.text()}; } catch(e){ return {ok:false,status:'err:'+e}; } }"""

class Browser:
    def __init__(self):
        from patchright.sync_api import sync_playwright
        self._pw=sync_playwright().__enter__()
        self.ctx=self._pw.chromium.launch_persistent_context(PROFILE, headless=False, args=["--disable-dev-shm-usage"])
        self.page=self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
    def close(self):
        try:self.ctx.close()
        except Exception:pass
        try:self._pw.__exit__(None,None,None)
        except Exception:pass
    def wait_cf(self):
        for _ in range(30):
            t=self.page.title().lower()
            if "just a moment" not in t and "verifying" not in t and "loading" not in t and "moment" not in t:
                return
            self.page.wait_for_timeout(1000)
    def goto(self,url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000); self.wait_cf()
    def fetch(self,url):
        return self.page.evaluate(_FETCH_JS, url)

# ---------- stages ----------
def discover(c, br):
    br.goto(BASE+"/en/homepage/")
    r=br.fetch(SITEMAP)
    locs=re.findall(r"<loc>(.*?)</loc>", r.get("text","") or "")
    ins=0
    for url in locs:
        slug=url.rstrip("/").split("/")[-1]
        md=re.match(r"^(\d{4})-(\d{2})-(\d{2})-", slug)
        ev=f"{md.group(1)}-{md.group(2)}-{md.group(3)}" if md else None
        # provisional case_id = slug (replaced by pdf-stem at fetch when available)
        cid="slug:"+slug
        if c.execute("SELECT 1 FROM aias_reports WHERE report_url=?", (url,)).fetchone(): continue
        c.execute("INSERT OR IGNORE INTO aias_reports (case_id,report_url,event_date,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?)",
                  (cid,url,ev,'new',now(),now())); c.commit(); ins+=1
    return ins, len(locs)

def fetch(c, br):
    rows=c.execute("SELECT case_id,report_url,event_date FROM aias_reports WHERE status='new'").fetchall()
    if rows:
        try: br.goto(BASE+"/en/homepage/")
        except Exception: pass
    done=0; fails=0
    for row in rows:
        url=row["report_url"]
        try:
            br.goto(url); time.sleep(DELAY)
            html=br.page.content()
            title=br.page.title()
            hrefs=re.findall(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
            pdf_url,lang=pick_report_pdf(hrefs)
            ev,rtype,acft,reg,loc=parse_title(title)
            ev = ev or row["event_date"]
            new_cid = case_id_from_pdf(pdf_url) if pdf_url else None
            cid = new_cid or row["case_id"]
            pdf_path=None
            if pdf_url:
                dest=os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]","_",cid)+".pdf")
                res=br.fetch(pdf_url)
                if not res.get("ok"):
                    br.goto(url); time.sleep(1); res=br.fetch(pdf_url)
                if res.get("ok") and res.get("b64"):
                    with open(dest,"wb") as fh: fh.write(base64.b64decode(res["b64"]))
                    pdf_path=dest
            # update row (may change PK case_id)
            c.execute("UPDATE aias_reports SET case_id=?,pdf_url=?,pdf_path=?,title=?,report_type=?,aircraft=?,registration=?,event_date=?,location=?,lang=?,status=?,updated_at=? WHERE report_url=?",
                      (cid,pdf_url,pdf_path,title,rtype,acft,reg,ev,loc,lang,'fetched',now(),url)); c.commit()
            done+=1; fails=0
        except Exception as e:
            print(f"[aias fetch] {url}: {e}", file=sys.stderr); fails+=1
            if fails>=5:
                print("[aias fetch] 5 consecutive fails, aborting", file=sys.stderr); break
    return done

def parse(c):
    rows=c.execute("SELECT case_id,pdf_path FROM aias_reports WHERE status='fetched'").fetchall()
    for row in rows:
        txt=extract_text(row["pdf_path"])
        tier="pdf" if len(txt)>=MIN_NARRATIVE else ("scanned" if row["pdf_path"] else "none")
        c.execute("UPDATE aias_reports SET narrative_text=?,source_tier=?,status='parsed',updated_at=? WHERE case_id=?",
                  (txt,tier,now(),row["case_id"])); c.commit()
    return len(rows)

def build(c):
    rows=c.execute("SELECT * FROM aias_reports WHERE status='parsed'").fetchall()
    built=0
    for r in rows:
        narr=r["narrative_text"] or ""
        if (r["source_tier"] or "") not in ("pdf", "ocr") or len(narr)<FLOOR:
            c.execute("UPDATE aias_reports SET status='skipped',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();continue
        c.execute("""INSERT OR REPLACE INTO aias_accidents
          (case_id,event_date,aircraft,registration,operator,location,country,narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (r["case_id"],r["event_date"],r["aircraft"],r["registration"],None,r["location"],"RO",narr,None,r["report_url"],r["report_type"],
           site_slug(r["aircraft"],r["registration"],r["location"]),r["lang"] or "ro",now()))
        c.execute("UPDATE aias_reports SET status='built',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();built+=1
    return built

def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c=conn()
    if mode in ("discover","fetch","all"):
        br=Browser()
        try:
            if mode in ("discover","all"): print("discovered:", discover(c,br))
            if mode in ("fetch","all"): print("fetched:", fetch(c,br))
        finally: br.close()
    if mode in ("parse","all"): print("parsed:", parse(c))
    if mode in ("build","all"): print("built:", build(c))
    print("reports:", list(c.execute("SELECT status,count(*) FROM aias_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM aias_accidents").fetchone()[0])

if __name__=="__main__": main()
