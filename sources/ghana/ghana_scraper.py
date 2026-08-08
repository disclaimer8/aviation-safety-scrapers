#!/usr/bin/env python3
"""Ghana AIB (Aircraft Accident & Incident Investigation & Prevention Bureau)
httpx scraper. Source: aibghana.gov.gh/accident-reports/ (WP, PDFs in
/wp-content/uploads/). Small bureau. EN, 9G- regs. Stages discover|fetch|parse|build."""
import sys, os, re, time, sqlite3, subprocess, httpx
BASE="https://aibghana.gov.gh"
LISTS=[BASE+"/accident-reports/", BASE+"/safety-report/", BASE+"/"]
H={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
DELAY=1.5; MIN_NARRATIVE=600; FLOOR=80
HOME=os.path.expanduser("~/ghana-ingest"); DB=os.path.join(HOME,"ghana.db"); PDFDIR=os.path.join(HOME,"pdfs")
SCHEMA="""
CREATE TABLE IF NOT EXISTS ghana_reports (case_id TEXT PRIMARY KEY, pdf_url TEXT, pdf_path TEXT,
  title TEXT, report_type TEXT, registration TEXT, event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT, lang TEXT DEFAULT 'en', status TEXT DEFAULT 'new',
  discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS ghana_accidents (case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT,
  registration TEXT, operator TEXT, location TEXT, country TEXT DEFAULT 'GH', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT, lang TEXT, built_at INT);
"""
def now(): return int(time.time()*1000)
def conn():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c
def extract_text(p):
    if not p or not os.path.exists(p): return ""
    try: out=subprocess.run(["pdftotext","-q",str(p),"-"],capture_output=True,timeout=180)
    except Exception: return ""
    return out.stdout.decode("utf-8","replace").strip() if out.returncode==0 else ""
def slugify(*ps):
    s=re.sub(r"[^A-Za-z0-9]+","-"," ".join([p for p in ps if p])).strip("-").lower(); return s[:80] or None
def reg_from(s):
    m=re.search(r"\b(9G-[A-Z]{3})\b", s or "", re.I); return m.group(1).upper() if m else None
def is_report(name):
    n=name.lower()
    return ("report" in n or "complete" in n) and "guide" not in n and "form" not in n and "terminolog" not in n
def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "all"; os.makedirs(PDFDIR,exist_ok=True); c=conn()
    cl=httpx.Client(headers=H,timeout=25,follow_redirects=True,verify=False)
    if mode in ("discover","all"):
        ins=0; seen=set()
        for u in LISTS:
            try: r=cl.get(u)
            except Exception: continue
            for pu in dict.fromkeys(re.findall(r'href="([^"]+\.pdf[^"]*)"', r.text, re.I)):
                name=pu.split("/")[-1]
                if not is_report(name): continue
                cid=re.sub(r"\.pdf$","",name,flags=re.I).lower()
                if cid in seen: continue
                seen.add(cid)
                if c.execute("SELECT 1 FROM ghana_reports WHERE case_id=?",(cid,)).fetchone(): continue
                rt="Final report" if "complete" in name.lower() or "final" in name.lower() else ("Preliminary report" if "prelim" in name.lower() else "Investigation report")
                c.execute("INSERT OR IGNORE INTO ghana_reports (case_id,pdf_url,title,report_type,registration,status,discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                          (cid,(pu if pu.startswith("http") else BASE+pu),name,rt,reg_from(name),'new',now(),now())); c.commit(); ins+=1
            time.sleep(DELAY)
        print("discovered:",ins)
    if mode in ("fetch","all"):
        for row in c.execute("SELECT case_id,pdf_url FROM ghana_reports WHERE status='new'").fetchall():
            try:
                r=cl.get(row["pdf_url"]); 
                if r.content[:4]!=b"%PDF" and "pdf" not in r.headers.get("content-type","").lower(): raise ValueError("not pdf")
                dest=os.path.join(PDFDIR,re.sub(r"[^A-Za-z0-9_.-]","_",row["case_id"])+".pdf")
                open(dest,"wb").write(r.content)
                c.execute("UPDATE ghana_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",(dest,now(),row["case_id"])); c.commit()
            except Exception as e: print("[ghana fetch]",row["case_id"],e,file=sys.stderr)
            time.sleep(DELAY)
    if mode in ("parse","all"):
        for row in c.execute("SELECT case_id,pdf_path,registration FROM ghana_reports WHERE status='fetched'").fetchall():
            txt=extract_text(row["pdf_path"]); tier="pdf" if len(txt)>=MIN_NARRATIVE else ("scanned" if row["pdf_path"] else "none")
            c.execute("UPDATE ghana_reports SET narrative_text=?,source_tier=?,registration=COALESCE(?,registration),status='parsed',updated_at=? WHERE case_id=?",
                      (txt,tier,row["registration"] or reg_from(txt),now(),row["case_id"])); c.commit()
    if mode in ("build","all"):
        built=0
        for r in c.execute("SELECT * FROM ghana_reports WHERE status='parsed'").fetchall():
            narr=r["narrative_text"] or ""
            if (r["source_tier"] or "")!="pdf" or len(narr)<FLOOR:
                c.execute("UPDATE ghana_reports SET status='skipped',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();continue
            c.execute("INSERT OR REPLACE INTO ghana_accidents (case_id,event_date,aircraft,registration,operator,location,country,narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["case_id"],r["event_date"],None,r["registration"],None,r["location"],"GH",narr,None,r["pdf_url"],r["report_type"],slugify(r["registration"],r["case_id"]),"en",now()))
            c.execute("UPDATE ghana_reports SET status='built',updated_at=? WHERE case_id=?",(now(),r["case_id"]));c.commit();built+=1
        print("built:",built)
    print("accidents:",c.execute("SELECT count(*) FROM ghana_accidents").fetchone()[0])
if __name__=="__main__": main()
