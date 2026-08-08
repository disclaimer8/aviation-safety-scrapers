"""Salvage builder: populate taits_accidents from detail-page summary_text.
PDF enrichment (142 full reports) deferred (cross-origin CF download fix needed).
Narrative = summary_text (EN), floor 80 chars, only kind='article'."""
import sqlite3, os, re, time

c = sqlite3.connect(os.path.expanduser("~/taits-ingest/taits.db"))
c.row_factory = sqlite3.Row

def slugify(*parts):
    s = " ".join([p for p in parts if p])
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:80] or None

rows = c.execute("""
  SELECT * FROM taits_reports
  WHERE kind='article' AND length(coalesce(summary_text,''))>=80
""").fetchall()

built = 0
for r in rows:
    narr = (r["summary_text"] or "").strip()
    if len(narr) < 80:
        continue
    site_slug = slugify(r["aircraft"], r["registration"], r["location"])
    c.execute("""
      INSERT OR REPLACE INTO taits_accidents
        (case_id, event_date, aircraft, registration, operator, location, country,
         narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["case_id"], r["event_date"], r["aircraft"], r["registration"], r["operator"],
        r["location"], "LT", narr, None, r["report_url"], r["report_type"],
        site_slug, r["lang"] or "en", int(time.time()*1000),
    ))
    built += 1
c.commit()
print("BUILT", built)
print("ACCIDENTS_TOTAL", c.execute("select count(*) from taits_accidents").fetchone()[0])
print("narr buckets:", list(c.execute("select case when length(narrative_text)<200 then '80-200' when length(narrative_text)<600 then '200-600' else '>=600' end b, count(*) from taits_accidents group by b")))
for s in c.execute("select case_id,event_date,registration,aircraft,location,length(narrative_text) from taits_accidents order by event_date desc limit 3"):
    print("  SAMPLE", s[0],"|",s[1],"|",s[2],"|",s[3],"|",s[4],"| narrlen",s[5])
