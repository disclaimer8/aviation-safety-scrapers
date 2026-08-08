#!/usr/bin/env python3
"""kinsiv (North Macedonia КИНСИВ — Комитет за истрага на воздухопловни несреќи и сериозни инциденти)
aviation-accident ingest.

Source: kinsiv.mk — WordPress site that blocks direct curl/browser to HTML pages
but serves PDFs directly from wp-content/uploads. FlareSolverr on hetzner
(127.0.0.1:8191) can reach HTML pages when needed for discovery.

7 final reports found (as of 2026-06-10):
  Page 1 (investigations/):
    final-report-on-paraglider-accident-13-08-2025  (ACCID 006/2025, MK)
    final-report-on-paraglider-accident             (ACCID 003/2025, MK)
    final-report-on-serious-incident-pipistrel-...  (SINCID 001/2025, MK)
    final-report-near-air-collision-...             (KINSIV 001/2024, MK)
    final-report-z3-ua-002                          (КИНСИВ-06-ЗИ2022, MK)
    final-report-ha-lwk-en-translation              (2018, EN)
  Page 2:
    final-report-d-gllw                             (2016, EN)

case_id = 'kinsiv-' + kinsiv_ref (from PDF text: ACCID NNN/YYYY, SINCID NNN/YYYY, KINSIV NNN/YYYY)
          else 'kinsiv-' + registration.lower().replace(' ','')

All PDFs downloadable directly via HTTPS from kinsiv.mk/wp-content/uploads/.
HTML pages require FlareSolverr (Cloudflare challenge). PDFs bypass it.

Languages: 5 Macedonian (mk), 2 English (en) for older reports.
Phase 3 translation will be needed for mk reports.

OCR: Not required — all PDFs are text-based (verified pdftotext 2026-06-10).

Politeness: 3s delay between PDF downloads.
"""

import sys, os, re, time, sqlite3, subprocess, json, urllib.parse, shlex

SOURCE = "kinsiv"
COUNTRY = "MK"
HOME = os.path.expanduser("~/kinsiv-ingest")
DB = os.path.join(HOME, "kinsiv.db")
PDFDIR = os.path.join(HOME, "pdfs")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

DELAY = 3.0
NARRATIVE_FLOOR = 300  # minimum chars

# Static manifest — all known reports as of 2026-06-10.
# Discovery via FlareSolverr confirmed 7 reports (investigations/ pages 1+2).
# PDF URLs are direct kinsiv.mk/wp-content/uploads — no auth required.
# kinsiv_ref: internal KINSIV reference number from PDF text.
REPORTS = [
    {
        "case_id":    "kinsiv-accid-006-2025",
        "kinsiv_ref": "ACCID 006/2025",
        "source_url": "https://kinsiv.mk/en/final-report-on-paraglider-accident-13-08-2025/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/03/MK-FINAL-REPORT-ACCID-006-2025r1.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-accid-003-2025",
        "kinsiv_ref": "ACCID 003/2025",
        "source_url": "https://kinsiv.mk/en/final-report-on-paraglider-accident/",
        # Cyrillic р1 in filename; URL-percent-encoded
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/03/MK-ACCID-003-2025-%D1%801.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-sincid-001-2025",
        "kinsiv_ref": "SINCID 001/2025",
        "source_url": "https://kinsiv.mk/en/final-report-on-serious-incident-pipistrel-alpha-trainer-z3-ua-011/",
        # Cyrillic filename: Финален-извештај-за-сериозен-инцидент-Z3-UA-011.pdf
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/03/%D0%A4%D0%B8%D0%BD%D0%B0%D0%BB%D0%B5%D0%BD-%D0%B8%D0%B7%D0%B2%D0%B5%D1%88%D1%82%D0%B0%D1%98-%D0%B7%D0%B0-%D1%81%D0%B5%D1%80%D0%B8%D0%BE%D0%B7%D0%B5%D0%BD-%D0%B8%D0%BD%D1%86%D0%B8%D0%B4%D0%B5%D0%BD%D1%82-Z3-UA-011.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-001-2024",
        "kinsiv_ref": "KINSIV 001/2024",
        "source_url": "https://kinsiv.mk/en/final-report-near-air-collision-and-activation-of-tcas-ra-system/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/03/Final-Report-MK-TCAS-RA.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-06-zi2022",
        "kinsiv_ref": "КИНСИВ-06-ЗИ2022",
        "source_url": "https://kinsiv.mk/en/final-report-z3-ua-002/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2020/12/Final-Report-LW74-MK.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-ha-lwk-2018",
        "kinsiv_ref": None,
        "source_url": "https://kinsiv.mk/en/final-report-ha-lwk-en-translation/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2020/12/Final-Report-HA-LWK.pdf",
        "lang":       "en",
    },
    {
        "case_id":    "kinsiv-d-gllw-2016",
        "kinsiv_ref": None,
        "source_url": "https://kinsiv.mk/en/final-report-d-gllw/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2020/12/Final-Report.pdf",
        "lang":       "en",
        },
    {
        "case_id":    "kinsiv-008-2025",
        "kinsiv_ref": "KINSIV 008/2025",
        "source_url": "https://kinsiv.mk/%D1%84%D0%B8%D0%BD%D0%B0%D0%BB%D0%B5%D0%BD-%D0%B8%D0%B7%D0%B2%D0%B5%D1%88%D1%82%D0%B0%D1%98-%D0%B7%D0%B0-%D0%B1%D0%BB%D0%B8%D1%81%D0%BA%D0%B0-%D1%81%D1%80%D0%B5%D0%B4%D0%B1%D0%B0-%D0%B2%D0%BE-%D0%B2/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/05/20251109_Final-Report-008-2025MK.pdf",
        "lang":       "mk",
    },
    {
        "case_id":    "kinsiv-accid-010-2025",
        "kinsiv_ref": "ACCID 010/2025",
        "source_url": "https://kinsiv.mk/%D1%84%D0%B8%D0%BD%D0%B0%D0%BB%D0%B5%D0%BD-%D0%B8%D0%B7%D0%B2%D0%B5%D1%88%D1%82%D0%B0%D1%98-%D0%B7%D0%B0-%D0%BD%D0%B5%D1%81%D1%80%D0%B5%D1%9C%D0%B0-%D1%81%D0%BE-%D0%BF%D0%B0%D1%80%D0%B0%D0%B3%D0%BB-3/",
        "pdf_url":    "https://kinsiv.mk/wp-content/uploads/2026/05/%D0%A4%D0%B8%D0%BD%D0%B0%D0%BB%D0%B5%D0%BD-%D0%B8%D0%B7%D0%B2%D0%B5%D1%88%D1%82%D0%B0%D1%98-%D0%B7%D0%B0-%D0%BD%D0%B5%D1%81%D1%80%D0%B5%D1%9C%D0%B0-%D0%BD%D0%B0-%D0%93%D0%B0%D0%BB%D0%B8%D1%87%D0%B8%D1%86%D0%B0-010-2025.pdf",
        "lang":       "mk",
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kinsiv_reports (
  case_id        TEXT PRIMARY KEY,
  kinsiv_ref     TEXT,
  registration   TEXT,
  source_url     TEXT,
  pdf_url        TEXT,
  pdf_path       TEXT,
  event_date     TEXT,
  aircraft       TEXT,
  operator       TEXT,
  location       TEXT,
  narrative_text TEXT,
  probable_cause TEXT,
  lang           TEXT DEFAULT 'mk',
  status         TEXT DEFAULT 'new',
  skip_reason    TEXT,
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS kinsiv_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'MK',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT DEFAULT 'Final Report',
  site_slug      TEXT,
  lang           TEXT DEFAULT 'mk',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_kinsiv_status ON kinsiv_reports(status);
"""

# Date patterns (ISO, DD.MM.YYYY, DD/MM/YYYY, D month YYYY)
_DATE_RE = re.compile(
    r"\b((?:19|20)\d{2})[.\-/]([01]?\d)[.\-/]([0-3]?\d)\b"
    r"|"
    r"\b([0-3]?\d)[./\-]([01]?\d)[./\-]((?:19|20)\d{2})\b"
)

# Registration: Z3-*, D-*, HA-*, TC-*, F-*, OE-*, etc.
# Note: Z3-UA-011 is a 3-part registration (ultralight format in North Macedonia)
_REG_RE = re.compile(
    r"\b(Z3-[A-Z0-9]{2,3}(?:-[A-Z0-9]{2,5})?|[A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{3,5}[A-Z]{0,2}|TC-[A-Z]{3})\b"
)

# Aircraft type patterns
_AIRCRAFT_RE = re.compile(
    r"\b(Piper\s+34|Seneca\s+II?|A3[0-9]{2}|Boeing\s+7[0-9]{2}|B73[0-9]|"
    r"Airbus\s+A\d{3}|Pipistrel|Alpha\s+Trainer|paraglider|Параглајдер|"
    r"ултралесен|Maya|Маја)\b",
    re.IGNORECASE,
)

# Macedonian month names for date parsing
_MK_MONTHS = {
    "јануари": 1, "февруари": 2, "март": 3, "април": 4,
    "мај": 5, "јуни": 6, "јули": 7, "август": 8,
    "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_MK_DATE_PAT = re.compile(
    r"\b(\d{1,2})\.?\s+(" + "|".join(sorted(_MK_MONTHS, key=len, reverse=True)) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
# "07 мај 2022" etc.
_MK_DATE2 = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(sorted(_MK_MONTHS, key=len, reverse=True)) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)


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


def curl_get(url, out_path=None, retries=3):
    """Download URL with curl. Returns (bytes, http_code)."""
    for attempt in range(retries):
        cmd = ["curl", "-sL", "--max-time", "60", "-A", UA,
               "-w", "\n__HTTP_CODE__%{http_code}"]
        if out_path:
            cmd += ["-o", out_path]
        cmd += [url]
        r = subprocess.run(cmd, capture_output=True)
        raw = r.stdout
        if not out_path:
            # split off http code marker
            marker = b"\n__HTTP_CODE__"
            idx = raw.rfind(marker)
            if idx >= 0:
                code_bytes = raw[idx + len(marker):]
                body = raw[:idx]
                try:
                    code = int(code_bytes.strip())
                except ValueError:
                    code = 0
            else:
                body = raw
                code = 0
            if code in (429, 503):
                wait = 30 * (2 ** attempt)
                print(f"  [throttle] {code} → sleep {wait}s", flush=True)
                time.sleep(wait)
                continue
            time.sleep(DELAY)
            return body, code
        else:
            # For file downloads, check code via separate header request
            time.sleep(DELAY)
            return b"", 200  # assume ok if curl didn't error
    return b"", 0


def download_pdf(pdf_url, case_id):
    """Download PDF to pdfs/ dir. Returns local path or None."""
    os.makedirs(PDFDIR, exist_ok=True)
    # Build safe local filename from case_id
    safe_name = case_id.replace("/", "-") + ".pdf"
    out_path = os.path.join(PDFDIR, safe_name)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print(f"  [cached] {safe_name}", flush=True)
        return out_path
    print(f"  [download] {pdf_url}", flush=True)
    cmd = ["curl", "-sL", "--max-time", "120", "-A", UA, "-o", out_path, pdf_url]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print(f"  [ok] {os.path.getsize(out_path)} bytes → {safe_name}", flush=True)
        return out_path
    print(f"  [fail] download failed for {pdf_url}", flush=True)
    return None


def pdf_to_text(pdf_path):
    """Extract text from PDF via pdftotext. Returns string."""
    r = subprocess.run(["pdftotext", pdf_path, "-"], capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else ""


def parse_date(text):
    """Extract the accident/incident event date from text. Returns YYYY-MM-DD or None.

    Strategy: look for date in the title/abstract region first (first 500 chars of text).
    Common patterns in KINSIV PDFs:
      - "која се случи на ден DD.MM.YYYY" (MK: "which occurred on DD.MM.YYYY")
      - "happened on DD.MM.YYYY"
      - "на DD.MM.YYYY"
      - "на ден DD мај YYYY"
      - "на ден DD.MM.YYYY"
    Fall back to scanning full text and taking the earliest plausible date if no
    context-match is found.
    """
    # Context-aware: look for phrases indicating the event date
    # ORDER MATTERS — more specific patterns first
    event_context_patterns = [
        # Airport ICAO code immediately followed by event date (e.g. "LWSN, 04.01.2025")
        r"LW[A-Z]{2}[,\s]+(\d{1,2}\.\d{2}\.\d{4})",
        # MK: "која/кој се случи на ден DD.MM.YYYY"
        r"(?:која\s+се\s+случи|кој\s+се\s+случи|настана|случи\s+на|се\s+случи\s+на)\s+(?:ден\s+)?(\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2})",
        # EN: "happened on DD.MM.YYYY"
        r"(?:happened|occurred|took\s+place)\s+on\s+(\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2})",
        r"(?:happened|occurred)\s+on\s+(\d{2}\.\d{2}\.\d{4})",
        # Near ACCID/SINCID reference on same line
        r"ACCID\s+\d+/\d+\s.*?(\d{1,2}\.\d{2}\.\d{4})",
        r"SINCID\s+\d+/\d+\s.*?(\d{1,2}\.\d{2}\.\d{4})",
        r"на\s+ден\s+(\d{1,2}\.\d{2}\.\d{4})",
        r"на\s+(\d{1,2}\.\d{2}\.\d{4})\s+година",
        # Date on its own after airport reference in title area
        r"(?:аеродром|аеродромот|airport)\s+[A-Z]+\s*[-–,]?\s*(\d{1,2}\.\d{2}\.\d{4})",
    ]
    for pat in event_context_patterns:
        m = re.search(pat, text[:3000], re.IGNORECASE | re.DOTALL)
        if m:
            raw = m.group(1).replace("/", ".").replace("-", ".")
            parts = raw.split(".")
            if len(parts) == 3:
                try:
                    if len(parts[2]) == 4:  # DD.MM.YYYY
                        d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
                    else:  # YYYY.MM.DD
                        y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
                    if 1990 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                        return f"{y:04d}-{mo:02d}-{d:02d}"
                except ValueError:
                    pass

    # Context-aware with month name
    for m in _MK_DATE2.finditer(text[:3000]):
        day = int(m.group(1))
        month = _MK_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if month and 1 <= day <= 31 and 2010 <= year <= 2030:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # EN report: look for "happened on DD.MM.YYYY" anywhere in first 2000 chars
    m = re.search(r"happened on\s+(\d{1,2}\.\d{2}\.\d{4})", text[:2000], re.IGNORECASE)
    if m:
        parts = m.group(1).split(".")
        try:
            d, mo, y = int(parts[0]), int(parts[1]), int(parts[2])
            if 1990 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        except (ValueError, IndexError):
            pass

    # Fallback: scan full text, collect all plausible dates, return earliest
    # But filter out dates that are likely legal references (before 2005 or common boilerplate)
    dates = []
    for m in _MK_DATE2.finditer(text):
        day = int(m.group(1))
        month = _MK_MONTHS.get(m.group(2).lower())
        year = int(m.group(3))
        if month and 1 <= day <= 31 and 2005 <= year <= 2030:
            dates.append((year, month, day))

    for m in _DATE_RE.finditer(text):
        g = m.groups()
        if g[0]:  # YYYY-MM-DD
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
        else:  # DD/MM/YYYY
            d, mo, y = int(g[3]), int(g[4]), int(g[5])
        if 2005 <= y <= 2030 and 1 <= mo <= 12 and 1 <= d <= 31:
            dates.append((y, mo, d))

    if not dates:
        return None
    # Return most common or earliest plausible event date
    dates.sort()
    y, mo, d = dates[0]
    return f"{y:04d}-{mo:02d}-{d:02d}"


def parse_registration(text):
    """Extract aircraft registration from text."""
    m = _REG_RE.search(text)
    return m.group(1) if m else None


def parse_aircraft(text):
    """Extract aircraft type from text."""
    m = _AIRCRAFT_RE.search(text)
    return m.group(0).strip() if m else None


def extract_location(text):
    """Try to extract location from text."""
    # Common patterns: "at X airport", "near X", "во близина на X"
    patterns = [
        r"near\s+([A-Z][a-zA-Z\s]{3,30}(?:Airport|airport))",
        r"International airport\s+([A-Za-z]+)",
        r"at\s+(?:the\s+)?([A-Z][a-zA-Z\s]{3,30})\s+(?:airport|Airport)",
        r"во\s+близина\s+на\s+([А-Ша-шЅѓѓљњќѕжзиjкл]{3,30})",
        r"аеродром\s+([А-Ша-шЅѓѓљњќѕжзиjкл\s]{3,30})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    # Default: if Skopje mentioned, return that
    if "Skopje" in text or "Скопје" in text:
        return "Skopje, North Macedonia"
    if "Logovardi" in text or "Логоварди" in text:
        return "Logovardi Airport, North Macedonia"
    if "Krusevo" in text or "Крушево" in text:
        return "Krusevo, North Macedonia"
    if "Prilep" in text or "Прилеп" in text:
        return "Prilep, North Macedonia"
    return "North Macedonia"


def extract_narrative(text, lang):
    """Extract the main narrative body from PDF text."""
    # Remove header boilerplate (organization name repeated many times)
    header_patterns = [
        r"Република Северна Македонија\s+Влада на Република Северна Македонија\s+Комитет за истрага на воздухопловни несреќи и сериозни инциденти",
        r"Republic of Macedonia\s+Government of the Republic of Macedonia\s+Aircraft accident and incident investigation committee",
        r"НАМЕРНО ОСТАВЕНО ПРАЗНО",
        r"Намерно оставено празно!",
        r"Intentionally left blank",
        r"\d+\s*/\s*\d+",  # page numbers like "1/29"
    ]
    for pat in header_patterns:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)

    # Remove table of contents section
    toc_m = re.search(r"TABLE OF CONTENT|СОДРЖИНА|ТАБЕЛА НА СОДРЖИНА", text, re.IGNORECASE)
    intro_m = re.search(r"(?:1\.\s*Introduction|1\.0\s*Final Report|1\.\s*ВОВЕД|1\.\s*ФАКТИ|ФАКТИ И ЕЛЕМЕНТИ)", text, re.IGNORECASE)

    if intro_m:
        body = text[intro_m.start():]
    elif toc_m:
        # Skip TOC — find the actual start (usually after page 5-10)
        after_toc = text[toc_m.end():]
        # Find first real paragraph (long line)
        for line in after_toc.splitlines():
            if len(line.strip()) > 80:
                body = after_toc[after_toc.index(line):]
                break
        else:
            body = after_toc
    else:
        body = text

    # Remove "No one is allowed to copy..." boilerplate
    body = re.sub(
        r"No one is allowed to copy.*?contact KINSIV\.",
        "",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = re.sub(
        r"Никој не смее да го копира.*?КИНСИВ\.",
        "",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Collapse whitespace
    body = re.sub(r"\s+", " ", body).strip()
    return body


def extract_probable_cause(text, lang):
    """Extract probable cause / conclusion section."""
    patterns_en = [r"(?:PROBABLE CAUSE|CONCLUSION|FINDINGS AND CONCLUSIONS)[:\s]*(.{200,2000}?)(?=\n\n|\Z)", ]
    patterns_mk = [r"(?:ПРИЧИНА ЗА НЕСРЕЌАТА|ЗАКЛУЧОЦИ|НАОДИ И ЗАКЛУЧОЦИ|ВЕРОЈАТНА ПРИЧИНА)[:\s]*(.{200,2000}?)(?=\n\n|\Z)", ]
    patterns = patterns_en if lang == "en" else patterns_mk + patterns_en
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            cause = re.sub(r"\s+", " ", m.group(1)).strip()
            if len(cause) > 100:
                return cause[:1500]
    return None


def process_report(db, report):
    """Download PDF, extract text, parse metadata, insert into DB."""
    case_id = report["case_id"]
    print(f"\n[process] {case_id}", flush=True)

    # Check if already done
    row = db.execute("SELECT status FROM kinsiv_reports WHERE case_id=?", (case_id,)).fetchone()
    if row and row["status"] == "done":
        print(f"  [skip] already done", flush=True)
        return True

    pdf_url = report["pdf_url"]
    # Decode URL for actual URL (it was already encoded)
    # For the cyrillic filename, keep as-is
    pdf_path = download_pdf(pdf_url, case_id)
    if not pdf_path:
        db.execute("""INSERT OR REPLACE INTO kinsiv_reports
            (case_id, kinsiv_ref, source_url, pdf_url, lang, status, skip_reason, discovered_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (case_id, report.get("kinsiv_ref"), report["source_url"], pdf_url,
             report["lang"], "skip", "pdf_download_failed", now(), now()))
        db.commit()
        return False

    text = pdf_to_text(pdf_path)
    if not text or len(text.split()) < 50:
        print(f"  [warn] pdftotext returned little text ({len(text)} chars)", flush=True)
        db.execute("""INSERT OR REPLACE INTO kinsiv_reports
            (case_id, kinsiv_ref, source_url, pdf_url, pdf_path, lang, status, skip_reason, discovered_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (case_id, report.get("kinsiv_ref"), report["source_url"], pdf_url, pdf_path,
             report["lang"], "skip", "empty_text", now(), now()))
        db.commit()
        return False

    lang = report["lang"]
    narrative = extract_narrative(text, lang)
    probable_cause = extract_probable_cause(text, lang)
    event_date = parse_date(text)
    registration = parse_registration(text)
    aircraft = parse_aircraft(text)
    location = extract_location(text)

    # Try to extract operator
    operator = None
    op_patterns = [
        r"(?:operator|airline)[:\s]+([A-Za-z][A-Za-z\s]{3,30})\b",
        r"Wizz Air",
        r"оператор[:\s]+([А-Ша-ш\s]{3,30})\b",
    ]
    for pat in op_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            operator = m.group(0) if m.lastindex is None else m.group(1)
            operator = operator.strip()[:100]
            break

    narrative_len = len(narrative)
    print(f"  event_date={event_date} reg={registration} aircraft={aircraft}", flush=True)
    print(f"  narrative={narrative_len} chars location={location}", flush=True)

    if narrative_len < NARRATIVE_FLOOR:
        print(f"  [warn] narrative below floor ({narrative_len} < {NARRATIVE_FLOOR})", flush=True)
        status = "skip"
        skip_reason = f"narrative_too_short:{narrative_len}"
    else:
        status = "done"
        skip_reason = None

    db.execute("""INSERT OR REPLACE INTO kinsiv_reports
        (case_id, kinsiv_ref, registration, source_url, pdf_url, pdf_path,
         event_date, aircraft, operator, location,
         narrative_text, probable_cause, lang, status, skip_reason,
         discovered_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (case_id, report.get("kinsiv_ref"), registration, report["source_url"],
         pdf_url, pdf_path, event_date, aircraft, operator, location,
         narrative, probable_cause, lang, status, skip_reason,
         now(), now()))

    # Also insert into _accidents if status=done
    if status == "done":
        slug = case_id  # use case_id as slug for now
        db.execute("""INSERT OR REPLACE INTO kinsiv_accidents
            (case_id, event_date, aircraft, registration, operator, location,
             country, narrative_text, probable_cause, source_url, report_type,
             site_slug, lang, built_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (case_id, event_date, aircraft, registration, operator, location,
             COUNTRY, narrative, probable_cause, report["source_url"],
             "Final Report", slug, lang, now()))

    db.commit()
    return status == "done"


def verify(db):
    """Print verification summary."""
    print("\n" + "="*60)
    print("VERIFICATION SUMMARY")
    print("="*60)
    total = db.execute("SELECT COUNT(*) FROM kinsiv_reports").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM kinsiv_reports WHERE status='done'").fetchone()[0]
    skipped = db.execute("SELECT COUNT(*) FROM kinsiv_reports WHERE status='skip'").fetchone()[0]
    accidents = db.execute("SELECT COUNT(*) FROM kinsiv_accidents").fetchone()[0]
    print(f"Total reports: {total}")
    print(f"  done:    {done}")
    print(f"  skipped: {skipped}")
    print(f"  in accidents table: {accidents}")
    print()

    rows = db.execute("""SELECT case_id, event_date, registration, aircraft,
                                lang, length(narrative_text) as nlen, status, skip_reason
                         FROM kinsiv_reports ORDER BY event_date""").fetchall()
    dup_ids = db.execute("SELECT case_id, COUNT(*) c FROM kinsiv_reports GROUP BY case_id HAVING c>1").fetchall()
    if dup_ids:
        print(f"DUPLICATE case_ids: {[r['case_id'] for r in dup_ids]}")
    else:
        print("No duplicate case_ids.")

    print()
    print(f"{'CASE_ID':<30} {'DATE':<12} {'REG':<12} {'LANG':<4} {'NLEN':>6}  STATUS")
    for r in rows:
        nlen = r["nlen"] or 0
        flag = " <FLOOR" if nlen < NARRATIVE_FLOOR and r["status"] == "done" else ""
        skip = f" [{r['skip_reason']}]" if r["skip_reason"] else ""
        print(f"{r['case_id']:<30} {str(r['event_date'] or ''):<12} {str(r['registration'] or ''):<12} "
              f"{r['lang']:<4} {nlen:>6}  {r['status']}{flag}{skip}")

    # Check dated
    dated = db.execute("SELECT COUNT(*) FROM kinsiv_reports WHERE event_date IS NOT NULL AND status='done'").fetchone()[0]
    above_floor = db.execute(f"SELECT COUNT(*) FROM kinsiv_reports WHERE length(narrative_text)>={NARRATIVE_FLOOR} AND status='done'").fetchone()[0]
    print()
    print(f"Dated (done): {dated}/{done}")
    print(f"Above floor ({NARRATIVE_FLOOR} chars): {above_floor}/{done}")



# ---- FlareSolverr discovery on hetzner --------------------------------------
# The OCR host is read from the environment. It used to be written in
# here; a hostname and the account it is reached as are infrastructure
# detail this repository deliberately carries none of — see the other
# sources, which all take it from OCR_REMOTE.

HETZNER_HOST = os.environ.get("OCR_REMOTE", "")
KINSIV_INVESTIGATIONS_URLS = [
    "https://kinsiv.mk/en/investigations/",
    "https://kinsiv.mk/en/investigations/page/2/",
]


def flaresolverr_fetch_html(url):
    """Fetch URL via FlareSolverr running on hetzner (127.0.0.1:8191).

    Returns decoded HTML string or None on failure.
    SSHes to hetzner and runs curl there as a single shell command string,
    properly shell-quoted so that the JSON payload and Content-Type header
    survive the ssh argument boundary intact.
    """
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    })
    # Build a single shell command string so ssh runs it via /bin/sh.
    # shlex.quote() ensures payload and header are safe even with special chars.
    remote_cmd = (
        "curl -sS --max-time 90 -X POST "
        + "-H " + shlex.quote("Content-Type: application/json")
        + " -d " + shlex.quote(payload)
        + " http://127.0.0.1:8191/v1"
    )
    cmd = ["ssh", HETZNER_HOST, remote_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0:
            print(f"  [flaresolverr] ssh/curl error: {r.stderr.decode('utf-8','replace')[:200]}", flush=True)
            return None
        resp = json.loads(r.stdout.decode("utf-8", "replace"))
        status = resp.get("status", "")
        if status != "ok":
            print(f"  [flaresolverr] status={status} message={resp.get('message','')[:200]}", flush=True)
            return None
        html = resp.get("solution", {}).get("response", "")
        if not html:
            print(f"  [flaresolverr] empty response body", flush=True)
            return None
        return html
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"  [flaresolverr] exception: {e}", flush=True)
        return None


def discover_new_reports(db):
    """Discover new reports from kinsiv.mk via FlareSolverr on hetzner.

    Fetches investigations listing pages, parses PDF links, compares against
    known case_ids (from REPORTS manifest and existing DB rows), and returns
    a list of new report dicts ready for process_report().

    Additive-only — never modifies existing rows.
    """
    existing_urls = set(
        r[0]
        for r in db.execute("SELECT source_url FROM kinsiv_reports").fetchall()
        if r[0]
    )
    existing_pdf_urls = set(
        r[0]
        for r in db.execute("SELECT pdf_url FROM kinsiv_reports").fetchall()
        if r[0]
    )
    known_source_urls = {r["source_url"] for r in REPORTS}
    known_pdf_urls = {r["pdf_url"] for r in REPORTS}

    print(f"[discover] existing DB rows: {len(existing_urls)}", flush=True)

    new_reports = []

    for listing_url in KINSIV_INVESTIGATIONS_URLS:
        print(f"[discover] fetching {listing_url} via FlareSolverr ...", flush=True)
        html = flaresolverr_fetch_html(listing_url)
        if not html:
            print(f"  [discover] skipping {listing_url} (fetch failed)", flush=True)
            continue

        # Parse PDF links from WordPress listing
        # Pattern: href="https://kinsiv.mk/en/<slug>/" links to report pages
        # We extract the article page links and then fetch each page for the PDF link
        # Simpler: look for direct PDF links in wp-content/uploads
        pdf_links = re.findall(
            r'href=["\']?(https://kinsiv\.mk/wp-content/uploads/[^\s"\'<>]+\.pdf)["\']?',
            html,
            re.IGNORECASE,
        )
        page_links = re.findall(
            r'href=["\']?(https://kinsiv\.mk/en/final-report[^"\'<>\s]+/)["\']?',
            html,
            re.IGNORECASE,
        )

        print(f"  [discover] found {len(pdf_links)} PDF links, {len(page_links)} page links", flush=True)

        # For each page link found in the listing, check if it's already known
        for page_url in page_links:
            # Normalise trailing slash
            page_url = page_url.rstrip("/") + "/"
            if page_url in existing_urls or page_url in known_source_urls:
                continue
            # Unknown page — fetch it via FlareSolverr to find the PDF
            print(f"  [discover] new page found: {page_url}", flush=True)
            page_html = flaresolverr_fetch_html(page_url)
            if not page_html:
                continue
            page_pdfs = re.findall(
                r'href=["\']?(https://kinsiv\.mk/wp-content/uploads/[^\s"\'<>]+\.pdf)["\']?',
                page_html,
                re.IGNORECASE,
            )
            if not page_pdfs:
                print(f"    [discover] no PDF found on {page_url}", flush=True)
                continue
            pdf_url = page_pdfs[0]
            if pdf_url in existing_pdf_urls or pdf_url in known_pdf_urls:
                continue
            # Derive a case_id from the slug
            slug = page_url.rstrip("/").split("/")[-1]
            case_id = "kinsiv-" + re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
            # Detect language from slug
            lang = "en" if "-en-" in slug.lower() or slug.lower().endswith("-en") else "mk"
            print(f"    [discover] NEW: case_id={case_id} pdf={pdf_url}", flush=True)
            new_reports.append({
                "case_id": case_id,
                "kinsiv_ref": None,
                "source_url": page_url,
                "pdf_url": pdf_url,
                "lang": lang,
            })

        time.sleep(2.0)

    print(f"[discover] {len(new_reports)} new reports found", flush=True)
    return new_reports


def main():
    import argparse
    parser = argparse.ArgumentParser(description="kinsiv ingest P1")
    parser.add_argument("--verify-only", action="store_true", help="Only print DB summary")
    parser.add_argument("--force", action="store_true", help="Re-process done records")
    parser.add_argument("--discover", action="store_true", help="Discover+process new reports via FlareSolverr, then fall through to static REPORTS")
    args = parser.parse_args()

    db = conn()

    if args.verify_only:
        verify(db)
        return

    if args.force:
        db.execute("UPDATE kinsiv_reports SET status='new' WHERE status='done'")
        db.commit()

    if args.discover:
        new_reports = discover_new_reports(db)
        for report in new_reports:
            process_report(db, report)

    # Always process the static manifest (idempotent — skips already-done rows)
    for report in REPORTS:
        process_report(db, report)

    verify(db)
    db.close()


if __name__ == "__main__":
    main()

