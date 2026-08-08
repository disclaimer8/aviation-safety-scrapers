#!/usr/bin/env python3
"""SCAA (Seychelles Civil Aviation Authority, scaa.sc) accident-report ingest.

Transport: plain httpx GET. scaa.sc is a Joomla site behind a WAF that 403s
realistic-browser User-Agents but serves 200 to a curl-like UA, so we send
{User-Agent: curl/7.88.1, Accept: */*}. No browser needed.

Stages: discover -> fetch -> parse -> build, resumable via the status column on
the scaa_reports work table.

FINDING (2026-06-09): SCAA publishes NO aircraft accident/incident investigation
final reports on its website. The site carries only annual reports, AICs, the
"Investigation of Accidents" *regulation* (the law, not a report), SMS forms and
other regulatory PDFs. discover() therefore enumerates candidate PDFs across the
known sections and applies an ACCIDENT-REPORT gate + an explicit denylist so
that annual/regulatory docs are NEVER ingested as accidents. With the current
site this yields 0 reports, which is the correct outcome. The pipeline is left
in place so a future genuine final report would be picked up automatically.
"""
import os, re, sys, time, sqlite3, subprocess

import httpx

BASE = "https://www.scaa.sc"
DELAY = 1.5
MIN_NARRATIVE = 600   # preferred tier
FLOOR = 80            # absolute floor to build
HOME = os.path.expanduser("~/scaa-ingest")
DB = os.path.join(HOME, "scaa.db")
PDFDIR = os.path.join(HOME, "pdfs")

HEADERS = {"User-Agent": "curl/7.88.1", "Accept": "*/*"}

# Sections that can carry PDFs; we scan them for candidate report links.
SECTIONS = [
    "/index.php/regulatory/safety-reporting",
    "/index.php/regulatory/safety-security-regulation-department",
    "/index.php/regulatory/state-safety-programme",
    "/index.php/regulatory/other-regulations",
    "/index.php/media-centre/publications",
    "/index.php/downloads",
    "/index.php/service-providers/sms-documents-information",
]

# A candidate PDF is treated as an accident *report* only if its name signals an
# actual investigation report. The word "accident" alone is not enough (the site
# hosts the "Investigation of Accidents Regulations" law). Require report-ish
# wording AND that it is not on the denylist below.
ACCIDENT_HINT = re.compile(
    r"(accident|incident|occurrence|investigation)[^/]*\b(report|final|aig|ai)\b"
    r"|\bfinal[\s_%-]*report\b"
    r"|\bAIG[\s_%-]*\d",
    re.I,
)
DENY = re.compile(
    r"annual\s*report"        # SCAA Annual Report eCopy YYYY
    r"|regulation"            # ...Investigation of Accidents Regulations (the law)
    r"|policy|charter|form|manual|guideline|code\s*of\s*conduct"
    r"|just\s*culture|hazard\s*log|audit|procedure|survey|application"
    r"|annex|schedule|fees|mortgage|sop|covid",
    re.I,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scaa_reports (
  case_id TEXT PRIMARY KEY, report_url TEXT, pdf_url TEXT, pdf_path TEXT,
  title TEXT, report_type TEXT, aircraft TEXT, registration TEXT,
  event_date TEXT, location TEXT, narrative_text TEXT, source_tier TEXT,
  lang TEXT DEFAULT en, status TEXT DEFAULT new,
  discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS scaa_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'SC', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT DEFAULT 'en', built_at INT);
CREATE INDEX IF NOT EXISTS idx_scaa_status ON scaa_reports(status);
"""

def now(): return int(time.time() * 1000)

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA); c.commit(); return c

def client():
    return httpx.Client(timeout=40, follow_redirects=True, headers=HEADERS)

def absu(h):
    if h.startswith("http"): return h
    return BASE + h if h.startswith("/") else BASE + "/" + h

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

def case_id_from_url(url):
    stem = re.sub(r"\.pdf$", "", url.split("/")[-1], flags=re.I)
    stem = re.sub(r"%20", "-", stem)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return stem or None

# Seychelles registrations are S7-xxx; foreign regs also possible.
_REG_RE = re.compile(r"\b(S7-[A-Z0-9]{3}|[A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{1,5}[A-Z]{0,2})\b")
def parse_reg(text):
    m = _REG_RE.search(text or "")
    return m.group(1) if m else None

_DATE_RE = re.compile(r"\b(\d{1,2})[ /-]([A-Za-z]{3,9}|\d{1,2})[ /-](\d{4})\b")
_MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}
def parse_date(text):
    m = _DATE_RE.search(text or "")
    if not m: return None
    d, mon, y = m.group(1), m.group(2).lower(), m.group(3)
    if mon.isdigit():
        mn = int(mon)
    else:
        mn = _MONTHS.get(mon[:3])
    if not mn or not (1 <= mn <= 12): return None
    try:
        return "%04d-%02d-%02d" % (int(y), mn, int(d))
    except ValueError:
        return None

def is_accident_report(url, title=""):
    name = url.split("/")[-1]
    hay = name + " " + (title or "")
    if DENY.search(hay):
        return False
    return bool(ACCIDENT_HINT.search(hay))

# ---------- stages ----------
def discover(c):
    cli = client(); ins = 0; scanned = 0
    for sec in SECTIONS:
        try:
            r = cli.get(absu(sec)); time.sleep(DELAY)
        except Exception as e:
            print("[scaa discover] %s: %s" % (sec, e), file=sys.stderr); continue
        if r.status_code != 200:
            print("[scaa discover] %s -> %s" % (sec, r.status_code), file=sys.stderr); continue
        pdfs = re.findall(r"href=\"([^\"]+\.pdf[^\"]*)\"", r.text, re.I)
        for href in pdfs:
            scanned += 1
            href = href.replace("&amp;", "&")
            if not is_accident_report(href):
                continue
            url = absu(href)
            cid = case_id_from_url(url)
            if not cid:
                continue
            if c.execute("SELECT 1 FROM scaa_reports WHERE case_id=?", (cid,)).fetchone():
                continue
            c.execute(
                "INSERT OR IGNORE INTO scaa_reports "
                "(case_id, report_url, pdf_url, title, report_type, lang, status, discovered_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, absu(sec), url, url.split("/")[-1], "Final report", "en", "new", now(), now()))
            c.commit(); ins += 1
    cli.close()
    print("[scaa discover] scanned %d candidate PDFs across %d sections" % (scanned, len(SECTIONS)),
          file=sys.stderr)
    return ins

def fetch(c):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute("SELECT case_id, pdf_url FROM scaa_reports WHERE status='new'").fetchall()
    if not rows: return 0
    cli = client(); done = 0; fails = 0
    for row in rows:
        url = row["pdf_url"]
        try:
            time.sleep(DELAY)
            r = cli.get(url)
            ct = r.headers.get("content-type", "")
            if "pdf" not in ct.lower() and r.content[:4] != b"%PDF":
                raise ValueError("not a pdf (ct=%s)" % ct)
            dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", row["case_id"]) + ".pdf")
            with open(dest, "wb") as fh: fh.write(r.content)
            c.execute("UPDATE scaa_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
                      (dest, now(), row["case_id"])); c.commit()
            done += 1; fails = 0
        except Exception as e:
            print("[scaa fetch] %s: %s" % (url, e), file=sys.stderr); fails += 1
            if fails >= 5:
                print("[scaa fetch] 5 consecutive fails, aborting", file=sys.stderr); break
    cli.close()
    return done

def parse(c):
    rows = c.execute("SELECT case_id, pdf_path FROM scaa_reports WHERE status='fetched'").fetchall()
    for row in rows:
        txt = extract_text(row["pdf_path"])
        tier = "pdf" if len(txt) >= FLOOR else ("scanned" if row["pdf_path"] else "none")
        ev = parse_date(txt[:4000])
        reg = parse_reg(txt[:4000])
        c.execute("UPDATE scaa_reports SET narrative_text=?, source_tier=?, event_date=COALESCE(event_date,?), "
                  "registration=COALESCE(registration,?), status='parsed', updated_at=? WHERE case_id=?",
                  (txt, tier, ev, reg, now(), row["case_id"])); c.commit()
    return len(rows)

def build(c):
    rows = c.execute("SELECT * FROM scaa_reports WHERE status='parsed'").fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if (r["source_tier"] or "") != "pdf" or len(narr) < FLOOR:
            c.execute("UPDATE scaa_reports SET status='skipped', updated_at=? WHERE case_id=?",
                      (now(), r["case_id"])); c.commit(); continue
        c.execute(
            "INSERT OR REPLACE INTO scaa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            " narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], r["event_date"], r["aircraft"], r["registration"], None,
             r["location"], "SC", narr, None, r["pdf_url"], r["report_type"] or "Final report",
             site_slug(r["aircraft"], r["registration"], r["location"]) or r["case_id"],
             r["lang"] or "en", now()))
        c.execute("UPDATE scaa_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), r["case_id"])); c.commit(); built += 1
    return built

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()
    if mode in ("discover", "all"): print("discovered:", discover(c))
    if mode in ("fetch", "all"):    print("fetched:", fetch(c))
    if mode in ("parse", "all"):    print("parsed:", parse(c))
    if mode in ("build", "all"):    print("built:", build(c))
    print("reports:", list(c.execute("SELECT status, count(*) FROM scaa_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM scaa_accidents").fetchone()[0])

if __name__ == "__main__":
    main()
