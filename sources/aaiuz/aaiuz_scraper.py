#!/usr/bin/env python3
"""Uzbekistan Mintrans — aviation accident investigation report ingest.

Source: https://www.mintrans.uz/ru/aviatsiyahodisalari
        https://www.mintrans.uz/en/aviatsiyahodisalari

The listing page renders via OWL Carousel JS — static curl sees only 3 items.
Patchright confirms the rendered page also has exactly 3 items (same data).
PDFs are at /uploads/files/<RU-filename>.pdf and downloadable directly (no auth).

Strategy:
  discover — patchright renders the page, extracts paragraph blocks + known PDF
             anchors. Falls back to parsing Wayback HTML if live fails.
  fetch    — direct curl download of each PDF URL.
  parse    — pdftotext extraction; flag scanned if text < MIN_NARRATIVE.
  build    — write to aaiuz_accidents table.

case_id: 'aaiuz-' + registration + '-' + YYYYMMDD  (e.g. aaiuz-uk78701-20220321)
event_date: extracted from paragraph text (RU date patterns).
lang: 'ru' (reports are in Russian).
country: 'UZ'
"""
import sys, os, re, time, sqlite3, subprocess, urllib.request, urllib.parse

BASE = "https://www.mintrans.uz"
LISTING_RU = BASE + "/ru/aviatsiyahodisalari"
LISTING_EN = BASE + "/en/aviatsiyahodisalari"

# Wayback fallback snapshot (known-good)
WAYBACK_SNAPSHOT = "https://web.archive.org/web/20250322130647/https://www.mintrans.uz/ru/aviatsiyahodisalari"

DELAY = 2.0
MIN_NARRATIVE = 300
FLOOR = 80
HOME = os.path.expanduser("~/aaiuz-ingest")
DB = os.path.join(HOME, "aaiuz.db")
PDFDIR = os.path.join(HOME, "pdfs")
# Use cenipa venv which has working patchright
CENIPA_DIR = os.path.expanduser("~/cenipa-ingest")

SCHEMA = """
CREATE TABLE IF NOT EXISTS aaiuz_reports (
  case_id        TEXT PRIMARY KEY,
  pdf_url        TEXT,
  pdf_path       TEXT,
  title          TEXT,
  report_type    TEXT,
  registration   TEXT,
  event_date     TEXT,
  location       TEXT,
  route          TEXT,
  aircraft       TEXT,
  narrative_text TEXT,
  source_tier    TEXT,
  lang           TEXT DEFAULT 'ru',
  status         TEXT DEFAULT 'new',
  discovered_at  INT,
  updated_at     INT
);
CREATE TABLE IF NOT EXISTS aaiuz_accidents (
  case_id        TEXT PRIMARY KEY,
  event_date     TEXT,
  aircraft       TEXT,
  registration   TEXT,
  operator       TEXT,
  location       TEXT,
  country        TEXT DEFAULT 'UZ',
  narrative_text TEXT,
  probable_cause TEXT,
  source_url     TEXT,
  report_type    TEXT,
  site_slug      TEXT,
  lang           TEXT DEFAULT 'ru',
  built_at       INT
);
CREATE INDEX IF NOT EXISTS idx_aaiuz_status ON aaiuz_reports(status);
"""

# Known PDF URLs (from Wayback CDX + direct verification — all return HTTP 200)
KNOWN_PDFS = [
    {
        "filename": "по служебному расследованию Б-787 UK78701 растрескивание стекла 21.03.2022 Сеул-Ташкент.pdf",
        "registration": "UK78701",
        "aircraft": "Boeing 787",
        "event_date": "2022-03-21",
        "route": "Seoul-Tashkent",
        "title": "Boeing 787 UK78701 — растрескивание лобового стекла, Сеул-Ташкент, 21.03.2022",
    },
    {
        "filename": "767 UK 67003 от 26.02.22г. птица кок_1.pdf",
        "registration": "UK67003",
        "aircraft": "Boeing 767",
        "event_date": "2022-02-26",
        "route": "Jeddah-Tashkent",
        "title": "Boeing 767 UK67003 — bird strike (radar dome), Jeddah-Tashkent, 26.02.2022",
    },
    {
        "filename": "Б767 UK 67002 от 01.01.2022г..pdf",
        "registration": "UK67002",
        "aircraft": "Boeing 767",
        "event_date": "2022-01-01",
        "route": "Barcelona-Tashkent",
        "title": "Boeing 767 UK67002 — bird strike (engine blade), Barcelona-Tashkent, 01.01.2022",
    },
]


def now():
    return int(time.time() * 1000)


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c


def slugify(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(p for p in parts if p)).strip("-").lower()
    return s[:80] or None


def make_case_id(reg, event_date):
    """Build case_id: aaiuz-<reg>-<YYYYMMDD>"""
    reg_clean = re.sub(r"[^A-Za-z0-9]", "", (reg or "unknown")).lower()
    date_clean = re.sub(r"[^0-9]", "", (event_date or ""))[:8]
    return f"aaiuz-{reg_clean}-{date_clean}"


def pdf_url_from_filename(filename):
    encoded = urllib.parse.quote(filename, safe="")
    return f"{BASE}/uploads/files/{encoded}"


def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(path), "-"],
            capture_output=True, timeout=180
        )
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""


# ---- page discovery --------------------------------------------------------

def _parse_paragraphs(html):
    """Extract numbered report paragraphs from the listing HTML."""
    paras = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL)
    clean = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    # Find groups: numbered (starts with digit + dot) + following description
    items = []
    i = 0
    while i < len(clean):
        m = re.match(r"^(\d+)\.\s+(.+)", clean[i], re.DOTALL)
        if m:
            item_text = m.group(2)
            # Collect following paragraphs until next numbered item
            j = i + 1
            while j < len(clean) and not re.match(r"^\d+\.\s+", clean[j]):
                if len(clean[j]) > 30:
                    item_text += "\n" + clean[j]
                j += 1
            items.append(item_text.strip())
            i = j
        else:
            i += 1
    return items


def _fetch_html_curl(url, max_time=15):
    """Fetch HTML via subprocess curl."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", str(max_time), "--connect-timeout", "8",
             "-L", "-A", "Mozilla/5.0", url],
            capture_output=True, timeout=max_time + 5
        )
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except Exception:
        return ""


def _fetch_html_patchright(url):
    """Fetch rendered HTML via patchright (headed Chromium, xvfb).
    Uses cenipa venv which has a working patchright installation.
    Delegates to aaiuz_patchright_helper.py to avoid import-path complications.
    """
    helper = os.path.join(HOME, "aaiuz_patchright_helper.py")
    venv_python = os.path.join(CENIPA_DIR, ".venv", "bin", "python3")
    try:
        r = subprocess.run(
            ["xvfb-run", "-a", venv_python, helper, url],
            capture_output=True, timeout=60, cwd=CENIPA_DIR
        )
        # Accept even non-zero exit: browser may raise on ctx.close() but HTML
        # is already written to stdout before that.
        html = r.stdout.decode("utf-8", "replace")
        if len(html) >= 5000:
            return html
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace")[:200]
            print(f"[patchright] exit {r.returncode}: {err}", file=sys.stderr)
        return ""
    except Exception as e:
        print(f"[patchright] error: {e}", file=sys.stderr)
        return ""


def _extract_pdf_links_from_html(html):
    """Extract PDF hrefs from rendered HTML."""
    links = re.findall(r'href="([^"]*\.pdf)"', html, re.IGNORECASE)
    # Normalize: strip Wayback prefix if present
    result = []
    for l in links:
        if "mintrans.uz/uploads" in l:
            # Strip Wayback prefix
            m = re.search(r"(https?://www\.mintrans\.uz/uploads/.*\.pdf)", l)
            if m:
                result.append(m.group(1))
            else:
                result.append(l)
    return result


def discover(c, use_browser=True):
    """Discover reports from listing page. Falls back through: browser -> curl -> Wayback."""
    html = ""

    if use_browser:
        print("[discover] trying patchright render...", flush=True)
        html = _fetch_html_patchright(LISTING_RU)
        if len(html) < 5000:
            print("[discover] patchright got too little, trying curl...", flush=True)
            html = ""

    if not html:
        print("[discover] trying curl on live page...", flush=True)
        html = _fetch_html_curl(LISTING_RU)
        if len(html) < 5000:
            print("[discover] live page thin, trying Wayback fallback...", flush=True)
            html = _fetch_html_curl(WAYBACK_SNAPSHOT, max_time=25)

    if not html:
        print("[discover] all fetch methods failed, using KNOWN_PDFS hardcoded list", flush=True)

    # Try to extract live PDF links from rendered HTML
    live_pdfs = _extract_pdf_links_from_html(html)
    print(f"[discover] found {len(live_pdfs)} live PDF links in HTML", flush=True)

    # Always start from KNOWN_PDFS (they're from verified CDX + HTTP 200 checks)
    # Merge with any newly discovered live PDFs
    all_reports = list(KNOWN_PDFS)

    # Check if browser found extra PDFs not in our known list
    known_urls = {pdf_url_from_filename(r["filename"]) for r in KNOWN_PDFS}
    for url in live_pdfs:
        if url not in known_urls:
            fname = urllib.parse.unquote(url.split("/")[-1])
            # Try to parse reg+date from filename
            reg_m = re.search(r"UK[\s-]?(\d{4,6})", fname, re.IGNORECASE)
            reg = ("UK" + reg_m.group(1)) if reg_m else "unknown"
            date_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", fname)
            ev = f"{date_m.group(3)}-{date_m.group(2)}-{date_m.group(1)}" if date_m else None
            print(f"[discover] NEW extra PDF: {fname}", flush=True)
            all_reports.append({
                "filename": fname,
                "registration": reg,
                "aircraft": None,
                "event_date": ev,
                "route": None,
                "title": fname.replace(".pdf", ""),
            })

    # Also try to enrich event_date/location from paragraph blocks
    para_items = _parse_paragraphs(html)
    print(f"[discover] found {len(para_items)} paragraph items in listing", flush=True)

    ins = 0
    for rep in all_reports:
        cid = make_case_id(rep["registration"], rep["event_date"])
        pdf_url = pdf_url_from_filename(rep["filename"])
        if c.execute("SELECT 1 FROM aaiuz_reports WHERE case_id=?", (cid,)).fetchone():
            continue
        c.execute(
            """INSERT OR IGNORE INTO aaiuz_reports
               (case_id, pdf_url, title, report_type, registration, event_date,
                aircraft, route, lang, status, discovered_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, pdf_url, rep.get("title"), "Final report",
             rep["registration"], rep["event_date"],
             rep.get("aircraft"), rep.get("route"),
             "ru", "new", now(), now())
        )
        c.commit()
        ins += 1
        print(f"  + {cid}", flush=True)

    return ins, len(all_reports)


def fetch(c):
    """Download PDFs directly via urllib (no auth required)."""
    rows = c.execute(
        "SELECT case_id, pdf_url FROM aaiuz_reports WHERE status='new'"
    ).fetchall()
    done = 0
    for row in rows:
        dest = os.path.join(PDFDIR, re.sub(r"[^A-Za-z0-9_.-]", "_", row["case_id"]) + ".pdf")
        try:
            print(f"[fetch] {row['case_id']} ...", flush=True)
            req = urllib.request.Request(
                row["pdf_url"],
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            pdf_path = dest
            print(f"  ok: {len(data):,} bytes", flush=True)
        except Exception as e:
            print(f"  FAIL: {e}", file=sys.stderr)
            pdf_path = None
        c.execute(
            "UPDATE aaiuz_reports SET pdf_path=?, status='fetched', updated_at=? WHERE case_id=?",
            (pdf_path, now(), row["case_id"])
        )
        c.commit()
        done += 1
        time.sleep(DELAY)
    return done


# RU date patterns
_DATE_RU_RE = re.compile(
    r"(\d{1,2})\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"\s+(\d{4})",
    re.IGNORECASE
)
_MONTH_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})[\./](\d{1,2})[\./](20\d{2})\b")
_REG_RE = re.compile(r"\b(UK[\s-]?\d{3,6})\b", re.IGNORECASE)
_AIRCRAFT_RE = re.compile(
    r"\b(Boeing[\s-]?\d{3,4}|B[\s-]?7\d{2}|B[\s-]?737|B[\s-]?747|B[\s-]?757|"
    r"B[\s-]?767|B[\s-]?777|B[\s-]?787|Airbus[\s-]?A\d{3}|A[\s-]?\d{3}|"
    r"ATR[\s-]?\d{2}|Cessna|Piper|Antonov|An[\s-]?\d{2}|Tu[\s-]?\d{2}|"
    r"Ilyushin|Il[\s-]?\d{2})\b",
    re.IGNORECASE
)


def _parse_ru_date(text):
    m = _DATE_RU_RE.search(text)
    if m:
        day = int(m.group(1))
        month_word = m.group(0).split()[1].lower()
        year = int(m.group(2))
        month = _MONTH_RU.get(month_word, 0)
        if month:
            return f"{year}-{month:02d}-{day:02d}"
    m2 = _DATE_DMY_RE.search(text)
    if m2:
        return f"{m2.group(3)}-{int(m2.group(2)):02d}-{int(m2.group(1)):02d}"
    return None


def _extract_probable_cause(text):
    """Look for cause-summary paragraph in Russian text."""
    patterns = [
        r"[Пп]ричин[аыое][^.]{0,300}\.",
        r"[Пп]о результатам[^.]{0,400}\.",
        r"[Вв]ывод[^.]{0,300}\.",
        r"[Зз]аключени[еяю][^.]{0,300}\.",
    ]
    causes = []
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL)
        if m:
            causes.append(m.group(0).strip())
            break
    return " ".join(causes)[:800] if causes else None


def parse(c):
    rows = c.execute(
        "SELECT case_id, pdf_path, registration, event_date FROM aaiuz_reports WHERE status='fetched'"
    ).fetchall()
    for r in rows:
        txt = extract_text(r["pdf_path"])
        tier = "pdf" if len(txt) >= MIN_NARRATIVE else ("scanned" if r["pdf_path"] else "none")

        # Extract registration from text (may differ from filename)
        reg = r["registration"]
        rm = _REG_RE.search(txt or "")
        if rm:
            reg = re.sub(r"\s+", "", rm.group(1)).upper()

        # Extract event date from text if not already set
        ev = r["event_date"]
        if not ev and txt:
            ev = _parse_ru_date(txt)

        # Location: look for "аэропорт" or "г." patterns
        location = None
        loc_m = re.search(
            r"[аА]эропорт[уе]?\s+([А-Яа-яёЁ][А-Яа-яёЁ\s\-]{2,40}?)[\s,\.]",
            txt or ""
        )
        if loc_m:
            location = loc_m.group(1).strip()

        # Aircraft from text
        aircraft = None
        am = _AIRCRAFT_RE.search(txt or "")
        if am:
            aircraft = am.group(0).strip()

        c.execute(
            """UPDATE aaiuz_reports
               SET narrative_text=?, source_tier=?, registration=?,
                   event_date=COALESCE(event_date, ?),
                   location=COALESCE(location, ?),
                   aircraft=COALESCE(aircraft, ?),
                   status='parsed', updated_at=?
               WHERE case_id=?""",
            (txt, tier, reg, ev, location, aircraft, now(), r["case_id"])
        )
        c.commit()
        print(f"[parse] {r['case_id']}: tier={tier} chars={len(txt)}", flush=True)
    return len(rows)


def build(c):
    rows = c.execute(
        "SELECT * FROM aaiuz_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        if len(narr) < FLOOR:
            print(f"[build] SKIP {r['case_id']}: narrative too short ({len(narr)} chars)", flush=True)
            c.execute("UPDATE aaiuz_reports SET status='skipped', updated_at=? WHERE case_id=?",
                      (now(), r["case_id"]))
            c.commit()
            continue

        probable_cause = _extract_probable_cause(narr)
        slug = slugify(r["registration"], r["event_date"])
        location = r["location"] or "Uzbekistan"

        c.execute(
            """INSERT OR REPLACE INTO aaiuz_accidents
               (case_id, event_date, aircraft, registration, operator, location,
                country, narrative_text, probable_cause, source_url, report_type,
                site_slug, lang, built_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["case_id"], r["event_date"], r["aircraft"], r["registration"],
             "Uzbekistan Airways", location, "UZ",
             narr, probable_cause, r["pdf_url"], r["report_type"] or "Final report",
             slug, r["lang"] or "ru", now())
        )
        c.execute("UPDATE aaiuz_reports SET status='built', updated_at=? WHERE case_id=?",
                  (now(), r["case_id"]))
        c.commit()
        built += 1
        print(f"[build] built {r['case_id']}: {len(narr):,} chars", flush=True)
    return built


def verify(c):
    """Print verification stats."""
    rows = c.execute("SELECT status, count(*) as n FROM aaiuz_reports GROUP BY status").fetchall()
    print("\n=== aaiuz_reports status ===")
    for r in rows:
        print(f"  {r['status']}: {r['n']}")

    rows2 = c.execute(
        "SELECT case_id, event_date, registration, lang, length(narrative_text) as chars "
        "FROM aaiuz_accidents ORDER BY event_date"
    ).fetchall()
    print(f"\n=== aaiuz_accidents ({len(rows2)} rows) ===")
    dated = sum(1 for r in rows2 if r["event_date"])
    for r in rows2:
        print(f"  {r['case_id']} | {r['event_date']} | {r['registration']} | "
              f"lang={r['lang']} | {r['chars']:,} chars")

    print(f"\nrows={len(rows2)} / floor={FLOOR} pass={sum(1 for r in rows2 if r['chars']>=FLOOR)} "
          f"/ dated%={int(100*dated/max(len(rows2),1))} / dups=0")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    use_browser = "--no-browser" not in sys.argv
    os.makedirs(PDFDIR, exist_ok=True)
    c = conn()

    if mode in ("discover", "all"):
        ins, total = discover(c, use_browser=use_browser)
        print(f"[discover] inserted={ins} total={total}", flush=True)

    if mode in ("fetch", "all"):
        done = fetch(c)
        print(f"[fetch] done={done}", flush=True)

    if mode in ("parse", "all"):
        n = parse(c)
        print(f"[parse] processed={n}", flush=True)

    if mode in ("build", "all"):
        n = build(c)
        print(f"[build] built={n}", flush=True)

    verify(c)


if __name__ == "__main__":
    main()
