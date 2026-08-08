import sqlite3, os, re
c=sqlite3.connect(os.path.expanduser("~/israel-ingest/israel.db"))
def rtype(title,cid):
    t=(title or "").lower()
    if re.match(r"^\d+-\d{2}-\d+$",cid): return "Interim statement"   # 19-23-01
    if "interim" in t: return "Interim statement"
    if "prelim" in t: return "Preliminary report"
    return "Final report"
for tbl in ("israel_reports","israel_accidents"):
    rows=c.execute(f"select case_id,title from {tbl}").fetchall() if tbl=="israel_reports" else c.execute("select a.case_id, r.title from israel_accidents a left join israel_reports r on r.case_id=a.case_id").fetchall()
    for cid,title in rows:
        c.execute(f"update {tbl} set report_type=? where case_id=?",(rtype(title,cid),cid))
c.commit()
print("types now:",dict(c.execute("select report_type,count(*) from israel_accidents group by report_type").fetchall()))
