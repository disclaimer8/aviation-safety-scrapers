#!/usr/bin/env python3
"""Panama AAC / UPIA accident-report ingest — plain httpx (no browser).

Source: https://www.aeronautica.gob.pa/index.php/seguridad-aerea/upia
The UPIA page lists ~35 PDFs, MOST of which are regulations / comunicados /
resoluciones / abandonment edicts. Genuine accident investigation reports are
PDFs named after an aircraft/registration or a numeric report id. We discover
all PDF candidates, download+validate (%PDF), extract text (pdftotext, OCR
fallback via ocrmypdf for scanned RICOH copies), then classify: a row is built
ONLY if narrative_text >= 80 chars AND it reads like an accident-investigation
report (INFORME / accidente / piloto / investigación) and NOT a
resolution/comunicado/edicto/abandonment notice.

Stages: discover (UPIA page -> panama_reports) | fetch (download PDF) |
parse (pdftotext, OCR fallback) | build (panama_accidents). Resumable via
status column. Idempotent: known case_ids are skipped.

Transport: plain httpx GET (page returns 200, no bot block). Pace ~1.5s.
"""
import os
import re
import sys
import time
import sqlite3
import subprocess
import urllib.parse

BASE = "https://www.aeronautica.gob.pa"
UPIA = BASE + "/index.php/seguridad-aerea/upia"
UA = "Mozilla/5.0 (compatible; FlightFinderBot/1.0; +https://flightfinder)"
DELAY = 1.5
MIN_NARRATIVE = 600   # preferred tier 'pdf' length
FLOOR = 80            # absolute minimum narrative_text length to build a row
OCR_TIMEOUT = 420

HOME = os.path.expanduser("~/panama-ingest")
DB = os.path.join(HOME, "panama.db")
PDFDIR = os.path.join(HOME, "pdfs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS panama_reports (
  case_id TEXT PRIMARY KEY, source_url TEXT, pdf_path TEXT,
  filename TEXT, candidate INT DEFAULT 0, looks_like_report INT,
  report_type TEXT, aircraft TEXT, registration TEXT, event_date TEXT,
  location TEXT, narrative_text TEXT, source_tier TEXT, reject_reason TEXT,
  lang TEXT DEFAULT 'es', status TEXT DEFAULT 'new',
  discovered_at INT, updated_at INT);
CREATE TABLE IF NOT EXISTS panama_accidents (
  case_id TEXT PRIMARY KEY, event_date TEXT, aircraft TEXT, registration TEXT,
  operator TEXT, location TEXT, country TEXT DEFAULT 'PA', narrative_text TEXT,
  probable_cause TEXT, source_url TEXT, report_type TEXT, site_slug TEXT,
  lang TEXT, built_at INT);
CREATE INDEX IF NOT EXISTS idx_panama_status ON panama_reports(status);
"""


def now():
    return int(time.time() * 1000)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


_client = None


def client():
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=90, follow_redirects=True,
            headers={"User-Agent": UA})
    return _client


def http_get(url):
    r = client().get(url)
    r.raise_for_status()
    return r


# ---------- candidate classification ----------
# filenames we EXCLUDE outright (regulations / notices / boilerplate)
_EXCLUDE_NAME = re.compile(
    r"(comunicado|resoluci|resolucion|edicto|norma|requisito|protocolo|"
    r"registro_|brochure|flyer|info\s*qr|examenes|declaracion\s+anual|"
    r"100\s*d[ií]as|^003$|gobierno|certificado|circular)", re.I)

# filename patterns that MARK a PDF as a plausible accident-report candidate:
# US reg N-xxxx, Panama reg HP-xxx, generic ICAO reg, "Aeronave ...", or a
# long numeric report id (RICOH-style timestamp ids the UPIA uses).
_CAND_NAME = re.compile(
    r"(\bN-?\d{2,5}[A-Z]{0,2}\b|\bHP-?\d{2,5}\b|"
    r"\b[A-Z]{1,2}-[A-Z0-9]{3,5}\b|aeronave|\b\d{10,}\b)", re.I)

# text that proves a real accident-investigation report
_REPORT_TOKENS = ("informe", "accidente", "investigaci", "piloto",
                  "tripulaci", "aterriz", "despegue", "ocurrencia")
# text that proves it is a regulation / resolution / abandonment edict
_NONREPORT_TOKENS = ("se declara en abandono", "declaracion de abandono",
                     "declaración de abandono", "edicto", "resolucion no",
                     "resolución no", "comunicado", "junta directiva",
                     "abandono la aeronave")


def filename_stem(url):
    name = urllib.parse.unquote(url.split("/")[-1])
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    return name, stem


def case_id_from_stem(stem):
    cid = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return cid or None


def parse_registration(stem, text):
    # filename first
    for src in (stem, text[:2000] if text else ""):
        m = re.search(r"\b(HP-?\d{2,5}[A-Z]?)\b", src, re.I)
        if m:
            return m.group(1).upper().replace(" ", "")
        m = re.search(r"\bN-?\d{2,5}[A-Z]{0,2}\b", src, re.I)
        if m:
            return m.group(0).upper().replace(" ", "")
        m = re.search(r"\b([A-Z]{1,2}-[A-Z0-9]{3,5})\b", src)
        if m:
            return m.group(1).upper().replace(" ", "")
    return None


_DATE_RE = re.compile(
    r"(\d{4})[-/.](\d{2})[-/.](\d{2})|"
    r"(\d{1,2})\s+de\s+([a-zA-Z]+)\s+de\s+(\d{4})", re.I)
_MESES = {"enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
          "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
          "septiembre": "09", "setiembre": "09", "octubre": "10",
          "noviembre": "11", "diciembre": "12"}


def parse_event_date(text):
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    if m.group(1):
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    d, mon, y = m.group(4), m.group(5).lower(), m.group(6)
    mm = _MESES.get(mon)
    if mm:
        return f"{y}-{mm}-{int(d):02d}"
    return None


def classify(text):
    """Return (is_report, reason). is_report True only when it reads like an
    accident-investigation report, not a regulation/resolution/edicto."""
    low = (text or "").lower()
    if len(low) < FLOOR:
        return False, "narrative<80"
    non = sum(tok in low for tok in _NONREPORT_TOKENS)
    rep = sum(tok in low for tok in _REPORT_TOKENS)
    if non >= 1 and non >= rep:
        return False, "regulation/abandonment-notice"
    if rep == 0:
        return False, "no-accident-narrative-tokens"
    return True, None


def site_slug(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(p for p in parts if p)).strip("-").lower()
    return s[:80] or None


def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def ocr_text(path):
    """OCR a scanned image-only PDF via ocrmypdf (spa+eng), return text."""
    if not path or not os.path.exists(path):
        return ""
    ocr_out = path[:-4] + "_ocr.pdf"
    try:
        if not os.path.exists(ocr_out):
            subprocess.run(
                ["ocrmypdf", "-l", "spa+eng", "--force-ocr", "-q", path, ocr_out],
                capture_output=True, timeout=OCR_TIMEOUT)
    except Exception:
        return ""
    return extract_text(ocr_out)


# ---------- stages ----------
def discover(c):
    html = http_get(UPIA).text
    time.sleep(DELAY)
    hrefs = re.findall(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
    seen = set()
    ins = 0
    cand_n = 0
    for h in hrefs:
        # normalise to absolute http(s) URL; skip windows-backslash junk
        h = h.strip()
        if "\\" in h:
            continue
        if h.startswith("//"):
            url = "https:" + h
        elif h.startswith("http"):
            url = h
        else:
            url = urllib.parse.urljoin(BASE + "/", h.lstrip("/"))
        url = url.replace("http://www.aeronautica.gob.pa", BASE)
        if url in seen:
            continue
        seen.add(url)
        name, stem = filename_stem(url)
        excluded = bool(_EXCLUDE_NAME.search(stem))
        is_cand = (not excluded) and bool(_CAND_NAME.search(stem))
        cid = case_id_from_stem(stem)
        if not cid:
            continue
        if c.execute("SELECT 1 FROM panama_reports WHERE case_id=?", (cid,)).fetchone():
            continue
        c.execute(
            "INSERT OR IGNORE INTO panama_reports "
            "(case_id,source_url,filename,candidate,status,discovered_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cid, url, name, 1 if is_cand else 0,
             'new' if is_cand else 'excluded', now(), now()))
        c.commit()
        ins += 1
        if is_cand:
            cand_n += 1
    return ins, cand_n, len(seen)


def fetch(c):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id,source_url FROM panama_reports "
        "WHERE candidate=1 AND status='new'").fetchall()
    done = 0
    fails = 0
    for row in rows:
        url = row["source_url"]
        cid = row["case_id"]
        try:
            time.sleep(DELAY)
            r = http_get(url)
            ct = r.headers.get("content-type", "").lower()
            if "pdf" not in ct and r.content[:4] != b"%PDF":
                c.execute("UPDATE panama_reports SET status='nopdf',reject_reason=?,updated_at=? "
                          "WHERE case_id=?", ("not-a-pdf", now(), cid))
                c.commit()
                continue
            dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", cid) + ".pdf")
            with open(dest, "wb") as fh:
                fh.write(r.content)
            c.execute("UPDATE panama_reports SET pdf_path=?,status='fetched',updated_at=? "
                      "WHERE case_id=?", (dest, now(), cid))
            c.commit()
            done += 1
            fails = 0
        except Exception as e:
            print(f"[panama fetch] {url}: {e}", file=sys.stderr)
            fails += 1
            if fails >= 5:
                print("[panama fetch] 5 consecutive fails, aborting", file=sys.stderr)
                break
    return done


def parse(c):
    rows = c.execute(
        "SELECT case_id,pdf_path,filename FROM panama_reports WHERE status='fetched'").fetchall()
    for row in rows:
        txt = extract_text(row["pdf_path"])
        tier = "pdf"
        if len(txt) < FLOOR:
            ocr = ocr_text(row["pdf_path"])
            if len(ocr) > len(txt):
                txt, tier = ocr, "ocr"
        if len(txt) < FLOOR:
            tier = "scanned"
        stem = re.sub(r"\.pdf$", "", row["filename"] or "", flags=re.I)
        is_report, reason = classify(txt)
        reg = parse_registration(stem, txt)
        ev = parse_event_date(txt)
        c.execute(
            "UPDATE panama_reports SET narrative_text=?,source_tier=?,"
            "looks_like_report=?,reject_reason=?,registration=?,event_date=?,"
            "status='parsed',updated_at=? WHERE case_id=?",
            (txt, tier, 1 if is_report else 0, reason, reg, ev, now(), row["case_id"]))
        c.commit()
    return len(rows)


def build(c):
    # NOTE: panama_reports has NO probable_cause column; we never select it.
    rows = c.execute(
        "SELECT case_id,event_date,aircraft,registration,location,narrative_text,"
        "source_tier,looks_like_report,source_url,report_type,lang,reject_reason "
        "FROM panama_reports WHERE status='parsed'").fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if not r["looks_like_report"] or len(narr) < FLOOR:
            c.execute("UPDATE panama_reports SET status='skipped',updated_at=? WHERE case_id=?",
                      (now(), r["case_id"]))
            c.commit()
            continue
        c.execute(
            "INSERT OR REPLACE INTO panama_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            "narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], r["event_date"], r["aircraft"], r["registration"], None,
             r["location"], "PA", narr, None, r["source_url"],
             r["report_type"] or "Final report",
             site_slug(r["aircraft"], r["registration"], r["case_id"]),
             r["lang"] or "es", now()))
        c.execute("UPDATE panama_reports SET status='built',updated_at=? WHERE case_id=?",
                  (now(), r["case_id"]))
        c.commit()
        built += 1
    return built


def main():
    import httpx as _h  # noqa: ensure available
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()
    if mode in ("discover", "all"):
        print("discovered:", discover(c))
    if mode in ("fetch", "all"):
        print("fetched:", fetch(c))
    if mode in ("parse", "all"):
        print("parsed:", parse(c))
    if mode in ("build", "all"):
        print("built:", build(c))
    print("reports by status:",
          list(c.execute("SELECT status,count(*) FROM panama_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM panama_accidents").fetchone()[0])


# httpx imported at top-level for client()
import httpx  # noqa: E402

if __name__ == "__main__":
    main()
