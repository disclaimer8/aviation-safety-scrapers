#!/usr/bin/env python3
"""OJK / ESIB (Estonia) aviation accident-report ingest — plain httpx.

Source: Ohutusjuurdluse Keskus (Estonian Safety Investigation Bureau).
Aviation reports are Drupal "Article" nodes surfaced via the vPortal Solr
search API (search.service.eu-live.vportal.ee). The aviation-reports listing
is the keyword facet:
  ET: "Lennuõnnetuste aruanded" (aviation accident reports, ~36)
      "Lennuõnnetuste ohutusjuurdlused" (ongoing aviation investigations, ~3)
  EN: "Aviation Safety Reports" (only a couple are EN-tagged)
~41 unique report pages total (aviation is a one-investigator unit). Each report
page has a final/preliminary PDF under /sites/default/files/documents/YYYY-MM/.

Plain httpx GET works (ojk.ee returns 200, no bot block). The vportal search
host ships an incomplete TLS chain, so that one host uses verify=False.

Stages: discover (search API -> ojk_reports) | fetch (detail page -> PDF) |
parse (pdftotext) | build (ojk_accidents). Resumable via status column.
"""
import sys, os, re, time, sqlite3, subprocess, warnings
import httpx

warnings.filterwarnings("ignore")

BASE = "https://ojk.ee"
SEARCH_API = "https://search.service.eu-live.vportal.ee/v1/search/ojk"
# (langcode, keyword) facets that list aviation reports
FACETS = [
    ("et", "Lennuõnnetuste aruanded"),
    ("et", "Lennuõnnetuste ohutusjuurdlused"),
    ("en", "Aviation Safety Reports"),
    ("en", "Aviation Safety Investigations"),
]
DELAY = 1.5
MIN_NARRATIVE = 600   # tier 'pdf'
FLOOR = 80            # build floor
HOME = os.path.expanduser("~/ojk-ingest")
DB = os.path.join(HOME, "ojk.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (compatible; FlightFinderBot/1.0; +https://flightfinder)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ojk_reports (
  case_id TEXT PRIMARY KEY, node_id TEXT, report_url TEXT, pdf_url TEXT,
  pdf_path TEXT, title TEXT, report_number TEXT, report_type TEXT,
  aircraft TEXT, registration TEXT, event_date TEXT, location TEXT,
  narrative_text TEXT, source_tier TEXT, lang TEXT,
  status TEXT DEFAULT 'new', discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS ojk_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'EE', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_ojk_status ON ojk_reports(status);
CREATE INDEX IF NOT EXISTS idx_ojk_node ON ojk_reports(node_id);
"""

def now(): return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit()
    return c

_site = None
_api = None
def site_client():
    global _site
    if _site is None:
        _site = httpx.Client(timeout=60, follow_redirects=True,
                             headers={"User-Agent": UA})
    return _site
def api_client():
    global _api
    if _api is None:
        _api = httpx.Client(timeout=60, follow_redirects=True, verify=False,
            headers={"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/"})
    return _api

def extract_text(path):
    if not path or not os.path.exists(path): return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(p for p in parts if p)).strip("-").lower()
    return s[:80] or None

# ---- registration: ES-xxx (Estonian) or foreign (SP-FDO, OH-HCI, OE-XOS, T7-VIT...) ----
_REG_RE = re.compile(r"\b([A-Z]{1,2}\d?-[A-Z0-9]{2,5})\b")
def find_reg(*texts):
    for t in texts:
        if not t: continue
        m = _REG_RE.search(t.upper())
        if m: return m.group(1)
    return None

_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")
def find_date(*texts):
    """Return ISO date from a dd.mm.yyyy occurrence in the text."""
    for t in texts:
        if not t: continue
        m = _DATE_RE.search(t)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            try:
                return "%04d-%02d-%02d" % (int(y), int(mo), int(d))
            except Exception:
                pass
    return None

_TYPE_MAP = [
    ("final accident", "Final report"), ("lõpparuanne", "Final report"),
    ("final report", "Final report"), ("preliminary", "Preliminary report"),
    ("esialgne", "Preliminary report"), ("vahearuanne", "Interim report"),
]
def map_type(title, fname):
    s = ((title or "") + " " + (fname or "")).lower()
    for k, v in _TYPE_MAP:
        if k in s: return v
    return "Final report"  # report contract default

def node_num(node_id):
    m = re.search(r"node/(\d+)", node_id or "")
    return m.group(1) if m else (node_id or None)

# taxonomy tags that bleed into the concatenated content_str
_TAG_NOISE = re.compile(
    r"\s*(Lennuõnnetuste aruanded|Lennuõnnetuste ohutusjuurdlused|Lennundus|"
    r"Aviation Safety Reports|Aviation Safety Investigations|Aviation|"
    r"Seotud dokumendid).*$", re.I)
def strip_tags(s):
    if not s:
        return None
    s = _TAG_NOISE.sub("", s).strip(" ,;:-")
    return s or None

def doc_to_row(doc):
    """Extract a normalized record from a search-API doc."""
    nid = node_num(doc.get("id"))
    uri = doc.get("uri") or ""
    report_url = uri if uri.startswith("http") else BASE + uri
    title = doc.get("title")
    lead = doc.get("lead_text") or ""
    content = doc.get("content") or []
    content_str = "\n".join(content) if isinstance(content, list) else str(content)
    created = (doc.get("created") or "")[:10] or None
    lang = doc.get("langcode") or ("en" if uri.startswith("/en/") else "et")
    # PDF filename appears in content ("... .pdf | 626 KB | pdf ...")
    fm = re.search(r"([A-Za-z0-9_.\-]+\.pdf)", content_str)
    pdf_fname = fm.group(1) if fm else None
    # report number (e.g. ECCAIRS EE051/180310/...)
    rm = re.search(r"(ECCAIRS[^\n|]+|EE\d{2,}[\w/]*)", content_str)
    report_number = rm.group(1).strip() if rm else None
    ev = find_date(content_str, lead, pdf_fname) or created
    reg = find_reg(title, lead, pdf_fname, content_str)
    rtype = map_type(title, pdf_fname)
    # aircraft: from content "Aircraft:" line, else lead heuristics
    aircraft = None
    cm = re.search(r"(?:Aircraft|Liiklusvahend|Õhusõiduk)\s*:?\s*([^\n|]{2,80})",
                   content_str, re.I)
    if cm:
        aircraft = strip_tags(re.sub(r"\s+", " ", cm.group(1)).strip(" :"))
    location = None
    lm = re.search(r"(?:Location|Asukoht)\s*:?\s*([^\n|]{1,80})", content_str, re.I)
    if lm:
        location = strip_tags(re.sub(r"\s+", " ", lm.group(1)).strip(" :"))
    return dict(node_id=nid, report_url=report_url, title=title,
                report_number=report_number, report_type=rtype, aircraft=aircraft,
                registration=reg, event_date=ev, location=location,
                lang=lang, pdf_fname=pdf_fname)

def make_case_id(row):
    """Intrinsic id: report number > PDF filename stem > node id."""
    rn = row.get("report_number")
    if rn:
        cid = re.sub(r"^ECCAIRS\s*", "", rn)
        cid = re.sub(r"[^A-Za-z0-9]+", "-", cid).strip("-")
        if cid: return "ojk-" + cid.lower()
    f = row.get("pdf_fname")
    if f:
        stem = re.sub(r"\.pdf$", "", f, flags=re.I)
        stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-")
        if stem: return "ojk-" + stem.lower()[:90]
    return "ojk-node-" + str(row.get("node_id"))

# ---------- stages ----------
def discover(c):
    api = api_client()
    by_node = {}  # node_num -> row (prefer en lang)
    for lang, kw in FACETS:
        try:
            r = api.get(SEARCH_API, params={
                "filters[keyword]": kw, "langcode": lang,
                "sort_by": "created", "page": "1", "limit": "100"})
            d = r.json()
        except Exception as exc:
            print(f"[ojk discover] facet {lang}/{kw}: {exc}", file=sys.stderr)
            continue
        time.sleep(DELAY)
        if not isinstance(d, dict) or "response" not in d:
            continue
        for doc in d["response"]["docs"]:
            row = doc_to_row(doc)
            nid = row["node_id"]
            if not nid:
                continue
            prev = by_node.get(nid)
            # prefer english-language version of the same node
            if prev is None or (row["lang"] == "en" and prev["lang"] != "en"):
                by_node[nid] = row
    inserted = 0
    for nid, row in by_node.items():
        cid = make_case_id(row)
        exists = c.execute(
            "SELECT 1 FROM ojk_reports WHERE node_id=? OR case_id=?",
            (nid, cid)).fetchone()
        if exists:
            continue
        c.execute(
            "INSERT OR IGNORE INTO ojk_reports "
            "(case_id,node_id,report_url,title,report_number,report_type,"
            " aircraft,registration,event_date,location,lang,status,"
            " discovered_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, nid, row["report_url"], row["title"], row["report_number"],
             row["report_type"], row["aircraft"], row["registration"],
             row["event_date"], row["location"], row["lang"], "new",
             now(), now()))
        c.commit(); inserted += 1
    return inserted, len(by_node)

_PDF_HREF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.I)
def fetch(c):
    os.makedirs(PDFDIR, exist_ok=True)
    site = site_client()
    rows = c.execute("SELECT case_id,report_url FROM ojk_reports WHERE status='new'").fetchall()
    done = 0; fails = 0
    for row in rows:
        url = row["report_url"]
        try:
            time.sleep(DELAY)
            h = site.get(url).text
            hrefs = _PDF_HREF_RE.findall(h)
            # report PDFs live under /sites/.../documents/
            cands = [x for x in hrefs if "/sites/" in x and "/documents/" in x] or hrefs
            pdf_url = None; pdf_path = None
            if cands:
                pu = cands[0]
                pdf_url = pu if pu.startswith("http") else BASE + pu
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", row["case_id"]) + ".pdf"
                dest = os.path.join(PDFDIR, safe)
                time.sleep(DELAY)
                pr = site.get(pdf_url)
                if pr.status_code == 200 and (
                        "pdf" in pr.headers.get("content-type", "").lower()
                        or pr.content[:4] == b"%PDF"):
                    with open(dest, "wb") as fh:
                        fh.write(pr.content)
                    pdf_path = dest
            c.execute("UPDATE ojk_reports SET pdf_url=?,pdf_path=?,status='fetched',"
                      "updated_at=? WHERE case_id=?",
                      (pdf_url, pdf_path, now(), row["case_id"]))
            c.commit(); done += 1; fails = 0
        except Exception as exc:
            print(f"[ojk fetch] {url}: {exc}", file=sys.stderr); fails += 1
            if fails >= 5:
                print("[ojk fetch] 5 consecutive fails, aborting", file=sys.stderr)
                break
    return done

def parse(c):
    rows = c.execute("SELECT case_id,pdf_path FROM ojk_reports WHERE status='fetched'").fetchall()
    for row in rows:
        txt = extract_text(row["pdf_path"])
        tier = "pdf" if len(txt) >= MIN_NARRATIVE else ("scanned" if row["pdf_path"] else "none")
        c.execute("UPDATE ojk_reports SET narrative_text=?,source_tier=?,"
                  "status='parsed',updated_at=? WHERE case_id=?",
                  (txt, tier, now(), row["case_id"]))
        c.commit()
    return len(rows)

def build(c):
    # NOTE: ojk_reports has no probable_cause column on purpose — do NOT select it.
    rows = c.execute("SELECT case_id,event_date,aircraft,registration,location,"
                     "narrative_text,source_tier,report_url,report_type,lang "
                     "FROM ojk_reports WHERE status='parsed'").fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            c.execute("UPDATE ojk_reports SET status='skipped',updated_at=? WHERE case_id=?",
                      (now(), r["case_id"])); c.commit(); continue
        c.execute("""INSERT OR REPLACE INTO ojk_accidents
          (case_id,event_date,aircraft,registration,operator,location,country,
           narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (r["case_id"], r["event_date"], r["aircraft"], r["registration"], None,
           r["location"], "EE", narr, None, r["report_url"],
           r["report_type"] or "Final report",
           site_slug(r["aircraft"], r["registration"], r["location"], r["case_id"]),
           r["lang"] or "et", now()))
        c.execute("UPDATE ojk_reports SET status='built',updated_at=? WHERE case_id=?",
                  (now(), r["case_id"])); c.commit(); built += 1
    return built

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()
    if mode in ("discover", "all"):
        ins, total = discover(c); print("discovered:", ins, "of", total)
    if mode in ("fetch", "all"):
        print("fetched:", fetch(c))
    if mode in ("parse", "all"):
        print("parsed:", parse(c))
    if mode in ("build", "all"):
        print("built:", build(c))
    print("reports:", list(c.execute(
        "SELECT status,count(*) FROM ojk_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM ojk_accidents").fetchone()[0])

if __name__ == "__main__":
    main()
