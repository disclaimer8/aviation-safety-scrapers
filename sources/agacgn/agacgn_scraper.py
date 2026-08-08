#!/usr/bin/env python3
"""agacgn — AGAC Guinea (Autorité Guinéenne de l'Aviation Civile, GN)
aviation-accident/serious-incident investigation report ingest.

Sources:
  1. agac.gov.gn — direct Laravel/storage PDF links discovered from homepage
  2. BAG AIA dashboard: GET https://dashboard.bagaia.org/publications-report
     Accept: application/json  → JSON index of 97 reports; 9 are Guinea records.

BAG AIA cross-reference analysis (9 Guinea records):
  WITH report_link (4 fetchable PDFs, 4 unique accidents):
    9ebecc33: 2004-11-08, 3X-GCM, B737-205, Air Guinée Express — B737 Conakry
    9ebeccba: 2018-06-24, 3X-AAK, L410UVP,  Eagle Air — L410 crash
    9ebecd28: 2010-07-28, TS-IEA, B737-700,  Mauritania Airways — Conakry runway
    9ebecdda: 2014-05-27, ZS-OMC, B1900D,    Solenta Aviation — serious incident
  WITHOUT report_link (5 entries, no PDF available):
    9db7eede-d5dc: 2014-05-04, ZS-OMC, B1900D, Solenta — older duplicate of 9ebecdda
    9db7eede-d8f5: 2014-08-06, 3X-AAE, AS350B, Armée de l'Air — no PDF
    9db7eedf-30c1: 2018-06-24, 3X-AAK, L410,   Eagle Air — older duplicate of 9ebeccba
    9db7eedf-8de1: 2022-09-02, CS-TVI, A320neo, TAP Air Portugal — no PDF
    9ebecd78: 2022-09-02, CS-TVI, A320neo, TAP Air Portugal — BAG AIA data error
             (report_link points to same B737 PDF as 3X-GCM, 2004 accident — wrong)

NOTE on 9ebecd78 (A320neo / TAP CS-TVI 2022): BAG AIA links this to the B737 Conakry
2004 PDF — that is a database error in BAG AIA (wrong report_link). The A320neo landed
at Conakry with a nose-wheel issue; the ACTUAL report PDF is not publicly available from
AGAC. This record is noted but NOT ingested.

case_id = 'agacgn-' + sanitized key (intrinsic from report_number or registration+date)
event_date: extracted from PDF text (French/English dates)
lang: 'fr' (or 'en' when EN report detected)
country: 'GN'

Stages: discover | fetch | parse | build | all
"""
import sys, os, re, time, sqlite3, subprocess, shlex, uuid, json

AGAC_BASE = "https://agac.gov.gn"
BAGAIA_API = "https://dashboard.bagaia.org/publications-report"
DELAY = 2.0
FLOOR = 300
HOME = os.path.expanduser("~/agacgn-ingest")
DB = os.path.join(HOME, "agacgn.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
OCR_LANG = "fra+eng"

# Known BAG AIA records WITH fetchable PDFs (from AGAC storage links)
# format: (bagaia_id, report_number, event_date_dd_mm_yyyy, registration, aircraft, operator, pdf_url)
BAGAIA_RECORDS_WITH_LINKS = [
    (
        "9ebecc33-a499-4d5c-bdb7-3960f4a836c8",
        "MT/CET/001/2005",
        "2004-11-08",
        "3X-GCM",
        "Boeing B737-205",
        "Air Guinée Express",
        "https://agac.gov.gn/storage/gzi9hHF6KVVuCDR642QtQWCLjFnEfN-metaUmFwcG9ydCBGaW5hbCBBY2NpZGVudCBBaXIgR3VpbmXMgWUgRXhwcmVzcyBCNzM3LTIwMCAzWC1HQ00ucGRm-.pdf",
    ),
    (
        "9ebeccba-1ba1-42bb-ad20-990c5cf8c4fa",
        "MT/CET/001/2018",
        "2018-06-24",
        "3X-AAK",
        "Let L-410UVP",
        "Eagle Air",
        "https://agac.gov.gn/storage/TrZJ35IDgodlIzvbrweT5PAZlf87vd-metaUmFwcG9ydCBGaW5hbCBBY2NpZGVudCBFYWdsZSBBSVIgTDQxMCAzWC1BQUsucGRm-.pdf",
    ),
    (
        "9ebecd28-115c-462b-a0ac-1e47cdec0ce3",
        "2010/303/SGG",
        "2010-07-28",
        "TS-IEA",
        "Boeing B737-700",
        "Mauritania Airways",
        "https://agac.gov.gn/storage/cOkgYzDDZDyWLUGi8j6KJFjXwvV1UC-metaUmFwcG9ydCBGaW5hbCBBY2NpZGVudCBNYXVyaXRhbmlhIEFpcndheXMgQjczNy03MDAucGRm-.pdf",
    ),
    (
        "9ebecdda-d34b-4f93-bb1a-d259b5b82d43",
        "2014/080/MT/SSG",
        "2014-05-27",
        "ZS-OMC",
        "Beechcraft 1900D",
        "Solenta Aviation",
        "https://agac.gov.gn/storage/8XUldTcglP6zwUJgNAsBxoeLp9PQSN-metaUmFwcG9ydCBGaW5hbCBJbmNpZGVudCBTb2xlbnRhIEF2aWF0aW9uIEIxOTAwRCBaUy1PTUMucGRm-.pdf",
    ),
]

# Records WITHOUT fetchable PDFs (noted for transparency)
BAGAIA_RECORDS_NO_LINK = [
    ("9db7eede-d5dc-4d32-989c-daf1c7ca5af5", "00816/MT/CAB/DNAC", "2014-05-04", "ZS-OMC", "Beechcraft 1900D", "Solenta Aviation", "DUPLICATE of 2014/080/MT/SSG (older BAG entry, no PDF)"),
    ("9db7eede-d8f5-4dbe-838b-f3afbb5205cc", "5126/MT/CAB/SGG/2019", "2014-08-06", "3X-AAE", "AS350B", "Armée de l'Air", "NO PDF available"),
    ("9db7eedf-30c1-4a37-acba-e66a96ce5488", "MT/CET/001/2018", "2018-06-24", "3X-AAK", "L410", "Eagle Air", "DUPLICATE of MT/CET/001/2018 (older BAG entry, no PDF)"),
    ("9db7eedf-8de1-4b2d-ba7a-7649e5118778", "", "2022-09-02", "CS-TVI", "A320neo", "TAP Air Portugal", "NO PDF available from AGAC"),
    ("9ebecd78-13d2-4690-9d0c-3a3ae882f8ae", "MT/CET/002/2022", "2022-09-02", "CS-TVI", "A320neo", "TAP Air Portugal", "BAG AIA data error: report_link points to B737/2004 PDF (wrong). No valid PDF."),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS agacgn_reports (
  case_id        TEXT PRIMARY KEY,
  report_number  TEXT,
  pdf_url        TEXT,
  pdf_path       TEXT,
  anchor_text    TEXT,
  report_type    TEXT,
  registration   TEXT,
  aircraft       TEXT,
  operator       TEXT,
  event_date     TEXT,
  location       TEXT,
  narrative_text TEXT,
  source_tier    TEXT,
  lang           TEXT DEFAULT 'fr',
  bagaia_id      TEXT,
  status         TEXT DEFAULT 'new',
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS agacgn_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'GN',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'fr',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_agacgn_status ON agacgn_reports(status);
"""


def now():
    return int(time.time() * 1000)


def conn():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def http():
    import httpx
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=60.0,
        follow_redirects=True,
    )


# ---- OCR helpers ----

def _ocr_remote(pdf_path, lang, host):
    remote = "/tmp/ocr-%s.pdf" % uuid.uuid4().hex
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), "%s:%s" % (host, remote)],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            print(f"[agacgn ocr] scp failed rc={cp.returncode}", file=sys.stderr, flush=True)
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language %s '
            '--sidecar "$f" --output-type none %s - >/dev/null 2>&1; '
            'cat "$f"; rm -f "$f" %s'
        ) % (shlex.quote(lang), shlex.quote(remote), shlex.quote(remote))
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[agacgn ocr] remote exception: {e}", file=sys.stderr, flush=True)
        try:
            subprocess.run(["ssh", host, "rm -f %s" % shlex.quote(remote)],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""


def ocr_extract(pdf_path, lang=OCR_LANG):
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, lang, host)
    import tempfile
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
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


# ---- date parsing (French + English) ----

_FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_ALL_MONTHS = {**_FR_MONTHS, **_EN_MONTHS}
_DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?(" + "|".join(sorted(_ALL_MONTHS.keys(), key=len, reverse=True)) + r")\s+(?:de\s+)?(\d{4})\b",
    re.IGNORECASE,
)
_DATE_NUMERIC_RE = re.compile(
    r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.]((19|20)\d{2})\b"
)
_DATE_BAG_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")


def parse_date_bagaia(s):
    """Parse BAG AIA date format DD/MM/YYYY."""
    if not s:
        return None
    m = _DATE_BAG_RE.match(s.strip())
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def parse_date(txt):
    if not txt:
        return None
    # Priority 1: written month name
    m = _DATE_TEXT_RE.search(txt[:4000])
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        mo = _ALL_MONTHS.get(month_str)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Priority 2: numeric DD/MM/YYYY in cover area
    m = _DATE_NUMERIC_RE.search(txt[:2000])
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ---- registration extraction ----

_REG_RE = re.compile(
    r"\b(3X-[A-Z]{3}|7T-[A-Z]{2,4}|ZS-[A-Z]{3}|TS-[A-Z]{3}|CS-[A-Z]{3}|F-[A-Z]{4}|[A-Z]{2}-[A-Z]{3,4}|N\d{2,5}[A-Z]{0,2})\b",
    re.I,
)


def parse_reg(txt):
    if not txt:
        return None
    m = _REG_RE.search(txt[:4000])
    return m.group(1).upper() if m else None


def parse_aircraft(txt):
    if not txt:
        return None
    for pat in [
        r"(?:aéronef|aeronef|aircraft|avion|hélicoptère|helicoptere)\s+(?:de\s+type\s+)?([A-Za-z0-9][A-Za-z0-9 \-/]{2,35})[,\n\.]",
        r"(?:type\s+)([A-Za-z][A-Za-z0-9 \-/]{3,30})\s+immatricul[eé]",
        r"\b(Boeing\s+[A-Z]?\d{3}[A-Z0-9\- ]*|Airbus\s+A\d{3}[A-Za-z0-9 \-]*|Let\s+L-4\d{2}[A-Za-z0-9 \-]*|L-?410[A-Za-z0-9 \-]*|Beechcraft\s+[A-Za-z0-9 \-]{3,30}|Beech\s+[A-Za-z0-9 \-]{3,25}|Écureuil\s+[A-Za-z0-9 ]*|AS\s*350[A-Z]?|Bell\s+\d{3}[A-Z0-9 \-]*|Cessna\s+[A-Z]?\d{3}[A-Za-z0-9 ]*)\b",
    ]:
        m = re.search(pat, txt[:4000], re.IGNORECASE)
        if m:
            ac = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(ac) > 2:
                return ac
    return None


def parse_location(txt):
    if not txt:
        return None
    for pat in [
        r"(?:at|near|in|à|sur)\s+([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})\s+(?:Airport|Aéroport|International)",
        r"aéroport\s+(?:international\s+)?(?:d[e']?\s+)?([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\- ]{2,40})[,\.\n]",
        r"airport\s+(?:of\s+)?([A-Z][a-zA-Z\- ]{2,40})[,\.\n]",
        r"Conakry|Labé|Kankan|Kindia|Kamsar|Faranah|Boké|Nzérékoré",
    ]:
        m = re.search(pat, txt[:5000], re.IGNORECASE)
        if m:
            if "Conakry|Labé" in pat:
                return m.group(0)
            loc = re.split(r"[\n,\.]", m.group(1).strip())[0].strip()
            if len(loc) > 2:
                return loc
    return None


def parse_probable_cause(txt):
    if not txt:
        return None
    for pat in [
        r"causes?\s+probables?\s*:?\s*(.{60,800}?)(?:\n\n|\Z)",
        r"probable\s+cause\s*[:\-]\s*(.{60,800}?)(?:\n\n|\Z)",
        r"Conclusions?\s*[:\-]\s*(.{60,800}?)(?:\n\n|\Z)",
    ]:
        m = re.search(pat, txt, re.IGNORECASE | re.DOTALL)
        if m:
            cause = re.sub(r"\s+", " ", m.group(1).strip())
            if len(cause) > 60:
                return cause[:1000]
    return None


def parse_operator(txt):
    if not txt:
        return None
    for pat in [
        r"(?:compagnie|exploitant|opérateur|operator|airline)\s*[:\-]?\s*([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \.,]{3,50})[,\n\.]",
        r"(?:de la compagnie)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \.,]{3,40})[,\n\.]",
    ]:
        m = re.search(pat, txt[:4000], re.IGNORECASE)
        if m:
            op = re.split(r"[\n,;]", m.group(1).strip())[0].strip()
            if len(op) > 2:
                return op
    return None


def detect_lang(txt):
    """Detect whether narrative is primarily French or English."""
    if not txt:
        return "fr"
    en_markers = len(re.findall(r"\b(the|accident|aircraft|investigation|report|flight|crew)\b", txt[:3000], re.I))
    fr_markers = len(re.findall(r"\b(le|la|les|du|des|accident|aéronef|enquête|rapport|vol)\b", txt[:3000], re.I))
    return "en" if en_markers > fr_markers else "fr"


# ---- case_id derivation ----

def case_id_from_report_number(report_number, registration, event_date):
    """Derive a stable intrinsic case_id."""
    if report_number:
        s = re.sub(r"[^A-Za-z0-9]+", "-", report_number.strip()).strip("-").lower()
        if s and len(s) >= 3:
            return "agacgn-" + s[:50]
    # Fallback: registration + year
    if registration and event_date:
        year = event_date[:4]
        reg = re.sub(r"[^A-Za-z0-9]+", "", registration).lower()
        return f"agacgn-{reg}-{year}"
    if registration:
        reg = re.sub(r"[^A-Za-z0-9]+", "", registration).lower()
        return f"agacgn-{reg}"
    return "agacgn-unknown"


# ---- discover ----

def discover(c, cl):
    """Load the 4 known BAG AIA records with fetchable PDFs."""
    inserted = 0
    for (bid, rnum, edate, reg, acft, op, pdf_url) in BAGAIA_RECORDS_WITH_LINKS:
        cid = case_id_from_report_number(rnum, reg, edate)
        if c.execute("SELECT 1 FROM agacgn_reports WHERE case_id=?", (cid,)).fetchone():
            print(f"[agacgn discover] already known: {cid}", flush=True)
            continue

        # Classify occurrence type from operator/registration context
        if "incident" in rnum.lower() or reg == "ZS-OMC":
            rtype = "Final report (serious incident)"
        else:
            rtype = "Final report"

        c.execute(
            "INSERT OR IGNORE INTO agacgn_reports "
            "(case_id,report_number,pdf_url,anchor_text,report_type,registration,"
            " aircraft,operator,event_date,lang,bagaia_id,status,discovered_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, rnum, pdf_url, f"{acft} {reg}", rtype, reg,
             acft, op, edate, "fr", bid, "new", now(), now()),
        )
        c.commit()
        inserted += 1
        print(f"[agacgn discover] {cid} | {rnum} | {reg} {acft} | {edate}", flush=True)

    # Also try to discover any additional PDFs from the AGAC homepage
    print(f"[agacgn discover] probing {AGAC_BASE}/", flush=True)
    try:
        r = cl.get(AGAC_BASE + "/")
        time.sleep(DELAY)
        if r.status_code == 200:
            # Find storage PDFs on homepage that might be accident reports
            storage_pdfs = re.findall(
                r'(https://agac\.gov\.gn/storage/[^"\'> ]+\.pdf)',
                r.text, re.I,
            )
            for url in storage_pdfs:
                # Skip images accidentally named .pdf
                if any(x in url.lower() for x in ["image", "photo", "logo"]):
                    continue
                cid = "agacgn-hp-" + re.sub(r"[^A-Za-z0-9]+", "-",
                                              url.split("/")[-1])[:50].strip("-")
                if c.execute("SELECT 1 FROM agacgn_reports WHERE case_id=?", (cid,)).fetchone():
                    continue
                # Only add if not already covered by BAG AIA records above
                if url in [r[6] for r in BAGAIA_RECORDS_WITH_LINKS]:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO agacgn_reports "
                    "(case_id,pdf_url,report_type,lang,status,discovered_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cid, url, "Investigation report", "fr", "new", now(), now()),
                )
                c.commit()
                inserted += 1
                print(f"[agacgn discover] homepage PDF: {cid}", flush=True)
    except Exception as e:
        print(f"[agacgn discover] homepage probe failed: {e}", file=sys.stderr, flush=True)

    # Print BAG AIA summary of records without PDFs
    print("\n[agacgn discover] BAG AIA records WITHOUT fetchable PDFs:", flush=True)
    for bid, rnum, edate, reg, acft, op, note in BAGAIA_RECORDS_NO_LINK:
        print(f"  {bid[:36]} | {edate} | {reg:10s} | {acft:20s} | {note}", flush=True)

    return inserted


# ---- fetch ----

def fetch(c, cl):
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT case_id, pdf_url FROM agacgn_reports WHERE status='new'"
    ).fetchall()
    done = 0
    for row in rows:
        cid, url = row["case_id"], row["pdf_url"]
        if not url:
            print(f"[agacgn fetch] {cid}: no URL, skip", file=sys.stderr, flush=True)
            continue
        dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", cid) + ".pdf")
        try:
            time.sleep(DELAY)
            resp = cl.get(url)
            ct = resp.headers.get("content-type", "")
            if resp.content[:4] != b"%PDF" and "pdf" not in ct.lower():
                raise ValueError(f"not a PDF (ct={ct} status={resp.status_code})")
            with open(dest, "wb") as fh:
                fh.write(resp.content)
            sz = os.path.getsize(dest)
            c.execute(
                "UPDATE agacgn_reports SET pdf_path=?,status='fetched',updated_at=? WHERE case_id=?",
                (dest, now(), cid),
            )
            c.commit()
            done += 1
            print(f"[agacgn fetch] {cid}: {sz//1024}KB", flush=True)
        except Exception as e:
            print(f"[agacgn fetch] {cid}: {e}", file=sys.stderr, flush=True)
    return done


# ---- parse ----

def _parse_one(cid, pdf_path, use_ocr=False):
    txt = extract_text(pdf_path)
    if len(txt) >= FLOOR:
        return txt, "pdf"
    if use_ocr and pdf_path and os.path.exists(pdf_path):
        print(f"[agacgn parse] {cid}: pdftotext={len(txt)} chars → OCR ({OCR_LANG})", flush=True)
        ocr_txt = ocr_extract(pdf_path, lang=OCR_LANG)
        if len(ocr_txt) >= FLOOR:
            print(f"[agacgn parse] {cid}: OCR yielded {len(ocr_txt)} chars", flush=True)
            return ocr_txt, "ocr"
        print(f"[agacgn parse] {cid}: OCR too short ({len(ocr_txt)} chars)", file=sys.stderr, flush=True)
    tier = "scanned" if (pdf_path and os.path.exists(pdf_path)) else "none"
    return txt, tier


def parse(c, use_ocr=True):
    rows = c.execute(
        "SELECT case_id, pdf_path, registration, event_date, location, aircraft, operator "
        "FROM agacgn_reports WHERE status='fetched'"
    ).fetchall()
    for row in rows:
        cid = row["case_id"]
        txt, tier = _parse_one(cid, row["pdf_path"], use_ocr=use_ocr)
        reg = row["registration"] or parse_reg(txt)
        date = row["event_date"] or parse_date(txt)
        loc = row["location"] or parse_location(txt)
        acft = row["aircraft"] or parse_aircraft(txt)
        op = row["operator"] or parse_operator(txt)
        lang = detect_lang(txt)
        c.execute(
            "UPDATE agacgn_reports "
            "SET narrative_text=?,source_tier=?,registration=?,event_date=?,location=?,"
            "aircraft=?,operator=?,lang=?,status='parsed',updated_at=? WHERE case_id=?",
            (txt, tier, reg, date, loc, acft, op, lang, now(), cid),
        )
        c.commit()
        print(f"[agacgn parse] {cid}: tier={tier} len={len(txt)} date={date} reg={reg} lang={lang}", flush=True)
    return len(rows)


# ---- build ----

def build(c):
    rows = c.execute(
        "SELECT case_id,event_date,aircraft,registration,location,operator,"
        "narrative_text,source_tier,report_type,pdf_url,lang FROM agacgn_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    skipped = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        tier = r["source_tier"] or ""

        if tier not in ("pdf", "ocr") or len(narr) < FLOOR:
            skipped += 1
            c.execute(
                "UPDATE agacgn_reports SET status='skipped',updated_at=? WHERE case_id=?",
                (now(), r["case_id"]),
            )
            c.commit()
            print(f"[agacgn build] SKIP {r['case_id']}: tier={tier} len={len(narr)}", flush=True)
            continue

        event_date = r["event_date"]
        registration = r["registration"]
        aircraft = r["aircraft"]
        location = r["location"]
        operator = r["operator"]

        # Enrich from narrative if still missing
        if tier in ("pdf", "ocr"):
            if not event_date:
                event_date = parse_date(narr)
            if not registration:
                registration = parse_reg(narr)
            if not aircraft:
                aircraft = parse_aircraft(narr)
            if not location:
                location = parse_location(narr)
            if not operator:
                operator = parse_operator(narr)

        probable_cause = parse_probable_cause(narr)

        # site_slug
        parts = []
        if registration:
            parts.append(registration.lower().replace("-", ""))
        if event_date:
            parts.append(event_date[:4])
        slug_base = "-".join(parts) if parts else r["case_id"]
        slug = re.sub(r"-+", "-", slug_base).strip("-")[:80]

        c.execute(
            "INSERT OR REPLACE INTO agacgn_accidents "
            "(case_id,event_date,aircraft,registration,operator,location,country,"
            " narrative_text,probable_cause,source_url,report_type,site_slug,lang,built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["case_id"], event_date, aircraft, registration, operator,
             location, "GN", narr, probable_cause, r["pdf_url"],
             r["report_type"] or "Final report",
             slug, r["lang"] or "fr", now()),
        )
        c.execute(
            "UPDATE agacgn_reports SET status='built',updated_at=? WHERE case_id=?",
            (now(), r["case_id"]),
        )
        c.commit()
        built += 1
        print(f"[agacgn build] {r['case_id']}: date={event_date} reg={registration} "
              f"acft={aircraft} narr={len(narr)} tier={tier}", flush=True)

    return built, skipped


# ---- main ----

def main():
    import warnings
    warnings.filterwarnings("ignore")
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "fetch", "all"):
        cl = http()
        try:
            if mode in ("discover", "all"):
                n = discover(c, cl)
                print(f"discovered: {n}", flush=True)
            if mode in ("fetch", "all"):
                n = fetch(c, cl)
                print(f"fetched: {n}", flush=True)
        finally:
            cl.close()

    if mode in ("parse", "all"):
        n = parse(c, use_ocr=True)
        print(f"parsed: {n}", flush=True)

    if mode in ("build", "all"):
        b, sk = build(c)
        print(f"built: {b}  skipped: {sk}", flush=True)

    print("\nreports status:", list(c.execute(
        "SELECT status, count(*) FROM agacgn_reports GROUP BY status"
    ).fetchall()), flush=True)
    acc_count = c.execute("SELECT count(*) FROM agacgn_accidents").fetchone()[0]
    print(f"accidents: {acc_count}", flush=True)

    if mode in ("all", "build"):
        print("\n--- agacgn_accidents summary ---")
        for row in c.execute(
            "SELECT case_id, event_date, registration, aircraft, site_slug, "
            "LENGTH(narrative_text) AS nl FROM agacgn_accidents ORDER BY event_date"
        ):
            print(f"  {row[0]!s:50s} | {row[1]!s:12s} | {row[2]!s:10s} | {row[3]!s:25s} | {row[4]!s:30s} | {row[5]}")


if __name__ == "__main__":
    main()
