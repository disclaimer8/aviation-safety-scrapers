#!/usr/bin/env python3
"""Israel AIAI (Ministry of Transport, Aviation Investigation of Accidents & Incidents)
report ingest — patchright CF transport. Source: gov.il dynamiccollector
aiai_investigations (paginated ?skip=0,10..). Per-item report PDFs at
gov.il/BlobFolder/dynamiccollectorresultitem/<case>/en/<file>.pdf (bilingual,
prefer EN). ~15 reports. Same-origin in-page CF fetch.
Stages: discover | fetch | parse | build (resumable via status)."""
import sys, os, re, time, base64, sqlite3, subprocess

BASE = "https://www.gov.il"
LIST = BASE + "/en/departments/dynamiccollectors/aiai_investigations?skip={}"
DELAY = 2.5
MIN_NARRATIVE = 600
FLOOR = 80
HOME = os.path.expanduser("~/israel-ingest")
DB = os.path.join(HOME, "israel.db")
PDFDIR = os.path.join(HOME, "pdfs")
PROFILE = os.path.join(HOME, ".cf-profile")

SCHEMA = """
CREATE TABLE IF NOT EXISTS israel_reports (
  case_id TEXT PRIMARY KEY, pdf_url TEXT, pdf_path TEXT, title TEXT,
  report_type TEXT, registration TEXT, event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT, lang TEXT,
  status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS israel_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'IL', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_israel_status ON israel_reports(status);
"""

def now(): return int(time.time()*1000)
def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c

def extract_text(path):
    if not path or not os.path.exists(path): return ""
    try: out=subprocess.run(["pdftotext","-q",str(path),"-"],capture_output=True,timeout=180)
    except Exception: return ""
    return out.stdout.decode("utf-8","replace").strip() if out.returncode==0 else ""

def slugify(*parts):
    s=re.sub(r"[^A-Za-z0-9]+","-"," ".join([p for p in parts if p])).strip("-").lower()
    return s[:80] or None

_SEG_RE=re.compile(r"dynamiccollectorresultitem/([^/]+)/",re.I)
def parse_blob(url):
    """Return (case_id, lang) from a BlobFolder PDF url."""
    m=_SEG_RE.search(url)
    if not m: return None,None
    seg=m.group(1)
    lang="en" if "/en/" in url.lower() or seg.lower().endswith("-en") else "he"
    cid=re.sub(r"-en$","",seg.lower()).replace("_","-")
    return cid, lang

def report_type_from(title, cid):
    t=(title or "").lower()
    if re.match(r"^\d+-\d{2}-\d+$",cid): return "Interim statement"
    if "interim" in t: return "Interim statement"
    if "prelim" in t: return "Preliminary report"
    return "Final report"

# ---- patchright ----
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
    def wait_cf(self):
        for _ in range(30):
            t=self.page.title().lower()
            if "attention required" not in t and "just a moment" not in t and "verifying" not in t: return
            self.page.wait_for_timeout(1000)
    def goto(self,url):
        self.page.goto(url,wait_until="domcontentloaded",timeout=45000); self.wait_cf()
        try:self.page.wait_for_load_state("networkidle",timeout=10000)
        except Exception:pass
    def fetch(self,url): return self.page.evaluate(_FETCH_JS,url)

def discover(c, br):
    skip=0; found={}
    while True:
        br.goto(LIST.format(skip)); time.sleep(DELAY)
        items=br.page.eval_on_selector_all("a[href]","els=>els.map(e=>({h:e.href,t:(e.innerText||'').trim().slice(0,80)}))")
        blob=[i for i in items if "blobfolder/dynamiccollectorresultitem" in i["h"].lower() and ".pdf" in i["h"].lower()]
        if not blob: break
        for i in blob:
            cid,lang=parse_blob(i["h"])
            if not cid: continue
            # prefer en pdf; keep first en, else first he
            if cid not in found or (lang=="en" and found[cid][1]!="en"):
                found[cid]=(i["h"],lang,i["t"])
        skip+=10
        if skip>200: break
    ins=0
    for cid,(url,lang,title) in found.items():
        if c.execute("SELECT 1 FROM israel_reports WHERE case_id=?",(cid,)).fetchone(): continue
        c.execute("INSERT OR IGNORE INTO israel_reports (case_id,pdf_url,title,report_type,lang,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (cid,url,title,report_type_from(title,cid),lang,'new',now(),now())); c.commit(); ins+=1
    return ins, len(found)

def fetch(c, br):
    rows=c.execute("SELECT case_id,pdf_url FROM israel_reports WHERE status='new'").fetchall()
    if rows:
        try: br.goto(LIST.format(0))
        except Exception: pass
    done=0; fails=0
    for row in rows:
        try:
            dest=os.path.join(PDFDIR,re.sub(r"[^A-Za-z0-9_.-]","_",row["case_id"])+".pdf")
            res=br.fetch(row["pdf_url"])
            if not res.get("ok"):
                br.goto(LIST.format(0)); time.sleep(1); res=br.fetch(row["pdf_url"])
            pdf_path=None
            if res.get("ok") and res.get("b64"):
                with open(dest,"wb") as fh: fh.write(base64.b64decode(res["b64"])); pdf_path=dest
            c.execute("UPDATE israel_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",(pdf_path,now(),row["case_id"])); c.commit()
            done+=1; fails=0; time.sleep(DELAY)
        except Exception as e:
            print("[israel fetch]",row["case_id"],e,file=sys.stderr); fails+=1
            if fails>=5: break
    return done

_REG_RE=re.compile(r"\b(4X-[A-Z]{3})\b")
_DATE_RE=re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](20\d{2})\b")
def parse(c):
    rows=c.execute("SELECT case_id,pdf_path FROM israel_reports WHERE status='fetched'").fetchall()
    for r in rows:
        txt=extract_text(r["pdf_path"])
        tier="pdf" if len(txt)>=MIN_NARRATIVE else ("scanned" if r["pdf_path"] else "none")
        reg=None; m=_REG_RE.search(txt or "")
        if m: reg=m.group(1)
        ev=None; d=_DATE_RE.search(txt or "")
        if d: ev=f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}"
        c.execute("UPDATE israel_reports SET narrative_text=?,source_tier=?,registration=?,event_date=COALESCE(event_date,?),status='parsed',updated_at=? WHERE case_id=?",
                  (txt,tier,reg,ev,now(),r["case_id"])); c.commit()
    return len(rows)

def build(c):
    rows=c.execute("SELECT * FROM israel_reports WHERE status='parsed'").fetchall()
    built=0
    for r in rows:
        narr=r["narrative_text"] or ""
        if (r["source_tier"] or "")!="pdf" or len(narr)<FLOOR:
            c.execute("UPDATE israel_reports SET status='skipped',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();continue
        # event_date fallback from case NN-YY
        ev=r["event_date"]
        if not ev:
            m=re.match(r"^(\d{1,3})-(\d{2})$",r["case_id"])
            if m: ev=f"20{m.group(2)}-01-01"
        c.execute("""INSERT OR REPLACE INTO israel_accidents
          (case_id,event_date,aircraft,registration,operator,location,country,narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (r["case_id"],ev,None,r["registration"],None,r["location"],"IL",narr,None,r["pdf_url"],r["report_type"],
           slugify(r["registration"],r["case_id"]),r["lang"] or "en",now()))
        c.execute("UPDATE israel_reports SET status='built',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();built+=1
    return built

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
    print("reports:",list(c.execute("SELECT status,count(*) FROM israel_reports GROUP BY status")))
    print("accidents:",c.execute("SELECT count(*) FROM israel_accidents").fetchone()[0])

if __name__=="__main__": main()
