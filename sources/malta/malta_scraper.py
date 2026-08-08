#!/usr/bin/env python3
"""Malta BAAI (Bureau of Air Accident Investigation) ingest — patchright CF.
Source: baai.gov.mt/accident-incident-report/ (single page, ~30 report PDFs at
baai.gov.mt/wp-content/uploads/<year>/<name>.pdf). EN, 9H- regs. Same-origin
in-page CF fetch. Stages: discover|fetch|parse|build."""
import sys, os, re, time, base64, sqlite3, subprocess
BASE="https://baai.gov.mt"
LIST=BASE+"/accident-incident-report/"
DELAY=2.5; MIN_NARRATIVE=600; FLOOR=80
HOME=os.path.expanduser("~/malta-ingest"); DB=os.path.join(HOME,"malta.db")
PDFDIR=os.path.join(HOME,"pdfs"); PROFILE=os.path.join(HOME,".cf-profile")
SCHEMA="""
CREATE TABLE IF NOT EXISTS malta_reports (
  case_id TEXT PRIMARY KEY, pdf_url TEXT, pdf_path TEXT, title TEXT,
  report_type TEXT, registration TEXT, event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT, lang TEXT DEFAULT 'en',
  status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS malta_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'MT', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_malta_status ON malta_reports(status);
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
_REG_RE=re.compile(r"\b(9H-[A-Z]{3})\b", re.I)
def reg_from(s):
    m=_REG_RE.search(s or ""); return m.group(1).upper() if m else None
_D1=re.compile(r"(\d{2})-(\d{2})-(20\d{2}|\d{2})")   # DD-MM-YYYY or DD-MM-YY
_D2=re.compile(r"(\d{2})(\d{2})(\d{2})(?:\D|$)")       # DDMMYY
def date_from(s):
    m=_D1.search(s or "")
    if m:
        y=m.group(3); y=("20"+y) if len(y)==2 else y
        return f"{y}-{m.group(2)}-{m.group(1)}"
    return None
def rtype(title, name):
    t=((title or "")+" "+(name or "")).lower()
    if "final" in t: return "Final report"
    if "prelim" in t: return "Preliminary report"
    if "progress" in t or "inquiry" in t: return "Progress report"
    if "basic" in t: return "Basic report"
    return "Investigation report"
def case_from(url):
    return re.sub(r"\.pdf$","",url.split("/")[-1],flags=re.I).lower() or None
def is_report(url, title):
    n=url.split("/")[-1].lower(); t=(title or "").lower()
    if "practical-guide" in n or "guide-on-safety" in n: return False
    if "no investigation required" in t: return False
    return True

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
        for _ in range(30):
            t=self.page.title().lower()
            if "just a moment" not in t and "attention" not in t: break
            self.page.wait_for_timeout(1000)
        try:self.page.wait_for_load_state("networkidle",timeout=10000)
        except Exception:pass
    def fetch(self,url): return self.page.evaluate(_FETCH_JS,url)

def discover(c, br):
    br.goto(LIST); time.sleep(DELAY)
    items=br.page.eval_on_selector_all("a[href]","els=>els.map(e=>({h:e.href,t:(e.innerText||'').trim().slice(0,90)}))")
    pdfs=[i for i in items if ".pdf" in i["h"].lower()]
    ins=0; seen=set()
    for i in pdfs:
        url=i["h"]
        if not is_report(url,i["t"]): continue
        cid=case_from(url)
        if not cid or cid in seen: continue
        seen.add(cid)
        if c.execute("SELECT 1 FROM malta_reports WHERE case_id=?",(cid,)).fetchone(): continue
        name=url.split("/")[-1]
        c.execute("INSERT OR IGNORE INTO malta_reports (case_id,pdf_url,title,report_type,registration,event_date,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (cid,url,i["t"],rtype(i["t"],name),reg_from(name),date_from(name),'new',now(),now())); c.commit(); ins+=1
    return ins
def fetch(c, br):
    rows=c.execute("SELECT case_id,pdf_url FROM malta_reports WHERE status='new'").fetchall()
    if rows:
        try: br.goto(LIST)
        except Exception: pass
    done=0; fails=0
    for row in rows:
        try:
            dest=os.path.join(PDFDIR,re.sub(r"[^A-Za-z0-9_.-]","_",row["case_id"])+".pdf")
            res=br.fetch(row["pdf_url"])
            if not res.get("ok"):
                br.goto(LIST); time.sleep(1); res=br.fetch(row["pdf_url"])
            pdf_path=None
            if res.get("ok") and res.get("b64"):
                with open(dest,"wb") as fh: fh.write(base64.b64decode(res["b64"])); pdf_path=dest
            c.execute("UPDATE malta_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",(pdf_path,now(),row["case_id"])); c.commit()
            done+=1; fails=0; time.sleep(DELAY)
        except Exception as e:
            print("[malta fetch]",row["case_id"],e,file=sys.stderr); fails+=1
            if fails>=5: break
    return done
def parse(c):
    rows=c.execute("SELECT case_id,pdf_path,registration,event_date FROM malta_reports WHERE status='fetched'").fetchall()
    for r in rows:
        txt=extract_text(r["pdf_path"])
        tier="pdf" if len(txt)>=MIN_NARRATIVE else ("scanned" if r["pdf_path"] else "none")
        reg=r["registration"] or reg_from(txt)
        c.execute("UPDATE malta_reports SET narrative_text=?,source_tier=?,registration=COALESCE(?,registration),status='parsed',updated_at=? WHERE case_id=?",
                  (txt,tier,reg,now(),r["case_id"])); c.commit()
    return len(rows)
def build(c):
    rows=c.execute("SELECT * FROM malta_reports WHERE status='parsed'").fetchall()
    built=0
    for r in rows:
        narr=r["narrative_text"] or ""
        if (r["source_tier"] or "")!="pdf" or len(narr)<FLOOR:
            c.execute("UPDATE malta_reports SET status='skipped',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();continue
        c.execute("""INSERT OR REPLACE INTO malta_accidents
          (case_id,event_date,aircraft,registration,operator,location,country,narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (r["case_id"],r["event_date"],None,r["registration"],None,r["location"],"MT",narr,None,r["pdf_url"],r["report_type"],
           slugify(r["registration"],r["case_id"]),"en",now()))
        c.execute("UPDATE malta_reports SET status='built',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();built+=1
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
    print("reports:",list(c.execute("SELECT status,count(*) FROM malta_reports GROUP BY status")))
    print("accidents:",c.execute("SELECT count(*) FROM malta_accidents").fetchone()[0])
if __name__=="__main__": main()
