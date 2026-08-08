#!/usr/bin/env python3
"""UKMAA (UK Military Aviation Authority) Service Inquiry ingest — httpx, gov.uk Content API.

Source: UK Defence Safety Authority / Military Aviation Authority published on gov.uk.
Collection: https://www.gov.uk/government/collections/service-inquiry-si
Licence: Open Government Licence v3.0 (OGL v3.0), Crown Copyright.

Covers:
  - Service Inquiries (SI) — 32 aviation-relevant SIs, 2009–2023
  - Board of Inquiries (BOIs) — 4 aviation BOIs, 2005–2013
  - Military Aircraft Accident Summary (MAAS) — 2 per-accident MAAS pages + 1 index page
    (The index page "summaries in The National Archives" = digest → SKIP per policy.)

Document structure: each gov.uk page has structured attachments (Parts 1.1-1.6+).
  - Part 1.3 = Narrative of events (preferred)
  - Part 1.4 = Analysis and findings (fallback/supplement)
  - Part 1.5 = Causes (probable_cause extraction)
  - Part 1.6 = Recommendations
  Single-PDF pages: download the single PDF directly.

Case IDs: ukmaa-<gov.uk-slug>  (slug = stable, unique content identifier from base_path).

Stages: discover | fetch | parse | build
parse-skipped: re-run for OCR on scanned rows.
"""

import sys, os, re, time, sqlite3, subprocess, uuid, shlex

HOME = os.path.expanduser("~/ukmaa-ingest")
DB   = os.path.join(HOME, "ukmaa.db")
PDFDIR = os.path.join(HOME, "pdfs")
DELAY = 1.5        # seconds between requests
MAX_PDF_MB = 60    # skip individual PDFs > 60 MB
MIN_NARRATIVE = 300
FLOOR = 80

UA = "flightfinder-ukmaa-ingest/1.0 (research; +https://himaxym.com)"

# gov.uk API endpoints
COLLECTION_URL = "https://www.gov.uk/api/content/government/collections/service-inquiry-si"
CONTENT_BASE   = "https://www.gov.uk/api/content"

# Aviation-relevant collection groups
AVIATION_GROUPS = {
    "Service Inquiries (SI)",
    "Board of Inquiries (BOIs)",
    "Military Aircraft Accident Summary (MAAS)",
}

# Aircraft serial-number pattern: 2 uppercase letters + 3-4 digits (e.g. ZJ960, XX179, ZM152)
SERIAL_RE = re.compile(r'\b[A-Z]{2}\d{3,4}\b')
# Aircraft type keywords to disambiguate from non-aviation SIs
AIRCRAFT_WORDS = re.compile(
    r'\b(helicopter|aircraft|tornado|chinook|hawk|puma|lynx|merlin|typhoon|'
    r'f-35|lightning|hercules|tucano|harrier|voyager|gazelle|squirrel|sea.?king|'
    r'apache|wildcat|uas|unmanned|watchkeeper|griffin|yak|tutor|westland|'
    r'nimrod|tristar|vc10)\b', re.I
)
# Aggregate digest (index, not per-accident) — skip
AGGREGATE_SKIP_TITLES = {
    "Military aircraft accident summaries in The National Archives",
}

# ─── Database ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS ukmaa_reports (
    slug            TEXT PRIMARY KEY,  -- gov.uk slug (stable intrinsic key)
    title           TEXT,
    group_name      TEXT,
    first_published TEXT,
    body_html       TEXT,
    pdf_part        TEXT,  -- JSON: list of {title, url, size_bytes, part_key}
    main_pdf_url    TEXT,
    main_pdf_path   TEXT,
    aux_pdf_urls    TEXT,  -- JSON: list of additional PDF urls fetched
    aux_pdf_paths   TEXT,  -- JSON: corresponding local paths
    narrative_text  TEXT,
    source_tier     TEXT,  -- 'pdf' | 'ocr' | 'html' | 'scanned' | 'none'
    status          TEXT NOT NULL DEFAULT 'new',
    discovered_at   INTEGER,
    updated_at      INTEGER
);
CREATE TABLE IF NOT EXISTS ukmaa_accidents (
    case_id        TEXT PRIMARY KEY,
    event_date     TEXT,
    aircraft       TEXT,
    registration   TEXT,
    operator       TEXT,
    location       TEXT,
    country        TEXT DEFAULT 'GB',
    narrative_text TEXT,
    probable_cause TEXT,
    source_url     TEXT,
    report_type    TEXT,
    site_slug      TEXT,
    lang           TEXT DEFAULT 'en',
    built_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ukmaa_status ON ukmaa_reports(status);
"""

def now():
    return int(time.time() * 1000)

def db_connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.commit()
    return c

def http_client():
    import httpx
    return httpx.Client(headers={"User-Agent": UA}, timeout=60.0, follow_redirects=True)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def slug_from_path(base_path):
    """Extract slug from /government/publications/<slug>."""
    return (base_path or "").rstrip("/").rsplit("/", 1)[-1]

def case_id(slug):
    return f"ukmaa-{slug}"

def site_slug_fn(*parts):
    s = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(p for p in parts if p))
    return s.strip("-").lower()[:80] or None

_SERIAL_TITLE_RE = re.compile(r'\b([A-Z]{2}\d{3,4}(?:/[A-Z]{2}\d{3,4})?)\b')
# 4-digit year: "25 August 2017" / "25 Aug 2017"
_DATE_4Y_RE = re.compile(
    r'\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})\b', re.I)
# 2-digit year: "1 Dec 11" / "10 Aug 10"
_DATE_2Y_RE = re.compile(
    r'\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{2})\b', re.I)
# Slug date: "on-10-aug-10" / "on-9-february-2014" / "on-26-april-2014"
_SLUG_DATE_RE = re.compile(
    r'on-(\d{1,2})-(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)-(\d{2,4})', re.I)

_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

def _2digit_year(yy):
    """Convert 2-digit year to 4-digit: 00-30 → 2000-2030, else 1990s."""
    yy = int(yy)
    return 2000 + yy if yy <= 30 else 1990 + yy

def parse_date_from_text(txt):
    """Parse event date from title text or slug."""
    if not txt:
        return None
    # 4-digit year first
    m = _DATE_4Y_RE.search(txt)
    if m:
        d, month_str, y = int(m.group(1)), m.group(2).lower()[:3], int(m.group(3))
        mo = _MONTH_MAP.get(month_str)
        if mo and 1 <= d <= 31 and 2000 <= y <= 2030:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # 2-digit year ("1 Dec 11")
    m = _DATE_2Y_RE.search(txt)
    if m:
        d, month_str, yy = int(m.group(1)), m.group(2).lower()[:3], m.group(3)
        mo = _MONTH_MAP.get(month_str)
        y = _2digit_year(yy)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Slug date pattern
    m = _SLUG_DATE_RE.search(txt)
    if m:
        d, month_str, yr = int(m.group(1)), m.group(2).lower()[:3], m.group(3)
        mo = _MONTH_MAP.get(month_str)
        y = _2digit_year(yr) if len(yr) == 2 else int(yr)
        if mo and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def parse_registration(txt):
    """Extract military serial or civilian G- reg from text."""
    if not txt:
        return None
    # Military serials first
    m = _SERIAL_TITLE_RE.search(txt)
    if m:
        return m.group(1)
    # Civilian reg (YAK52 G-YAKB case)
    m2 = re.search(r'\b(G-[A-Z]{4})\b', txt)
    if m2:
        return m2.group(1)
    return None

def parse_aircraft_from_title(title):
    """Extract aircraft type from SI title like 'Service Inquiry: accident involving Hawk TMk1 XX177'."""
    if not title:
        return None
    # Explicit known military type names — extract full canonical name
    TYPES = [
        r'F-35B Lightning', r'F-35B', r'Hawk T Mk1A', r'Hawk TMk1A', r'Hawk T Mk1', r'Hawk TMk1',
        r'Hawk T\s+Mk\s*1[A-Z]?', r'Hawk',
        r'Tornado GR4', r'Tornado GR Mk4', r'Tornado GR4A', r'Tornado GR', r'Tornado F3', r'Tornado',
        r'Chinook ZA\d+', r'Chinook',
        r'Puma HC MK 2', r'Puma HC Mk 2', r'Puma HC2', r'Puma',
        r'Lynx Mk 9', r'Lynx',
        r'Sea King', r'Gazelle',
        r'Tucano ZF\d+', r'Tucano',
        r'Merlin', r'Apache',
        r'Watchkeeper WK\d+', r'Watchkeeper',
        r'Hercules C-130J Mk4', r'Hercules C-130', r'Hercules XV\d+', r'Hercules',
        r'Griffin MK1', r'Griffin',
        r'Squirrel HT1', r'Squirrel',
        r'Voyager',
        r'YAK52', r'Yak.52',
        r'Tutor G-BY\w+', r'Tutor',
        r'Typhoon', r'Nimrod', r'Tristar',
        r'Unmanned Air System \(UAS\) Hermes 450', r'Hermes 450',
    ]
    for pat in TYPES:
        m = re.search(r'\b' + pat + r'\b', title, re.I)
        if m:
            return m.group(0).strip()
    # Fallback: "involving <Type> <Serial>"
    m = re.search(
        r'involving\s+(?:a\s+|an\s+|the\s+)?([A-Z][A-Za-z0-9 \-/]{3,35}?)\s+(?:[A-Z]{2}\d{3,4}|on\s)',
        title
    )
    if m:
        candidate = m.group(1).strip().rstrip(".,")
        # Reject non-aircraft words
        if not re.match(r'(?i)accident|incident|occurrence|loss|crash|aircraft$|aircraft\s+accident', candidate):
            return candidate if 3 <= len(candidate) <= 50 else None
    return None

def parse_location_from_title(title):
    """Extract location from SI title."""
    if not title:
        return None
    # Remove serial numbers first to avoid matching them as locations
    clean = re.sub(r'\b[A-Z]{2}\d{3,4}\b', '', title)
    clean = re.sub(r'\bG-[A-Z0-9]{3,5}\b', '', clean)
    clean = re.sub(r'\bWK\d{3,4}\b', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()

    # "in <city>, <country>" pattern — extract city only (before comma+country)
    m_in_city = re.search(
        r'\bin\s+([A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?),\s+(?:Afghanistan|Iraq|Germany|Libya|Cyprus|Pakistan|Belize|Kenya|Oman|Wales|Scotland|England)',
        clean
    )
    if m_in_city:
        return m_in_city.group(1).strip()

    # "at <location>" pattern — stop before "on <date>" or end-of-string
    m_at = re.search(r'\bat\s+([A-Z][A-Za-z0-9 \-,]{3,50}?)(?:\s+on\s+\d|\s*$)', clean)
    if m_at:
        loc = m_at.group(1).strip().rstrip(",.")
        # Strip trailing country names that sneak in
        loc = re.sub(r',?\s+(?:Wales|Scotland|England|Afghanistan|Iraq|Germany)\s*$', '', loc).strip()
        if re.match(r'^[A-Z]{2}\d', loc) or loc.lower() in ("gb", "uk"):
            pass
        elif 3 <= len(loc) <= 60:
            return loc

    # "near <location>"
    m_near = re.search(r'\bnear\s+([A-Z][A-Za-z0-9 \-]{3,40}?)(?:\s+on\s|\s*,|\s*$)', clean)
    if m_near:
        loc = m_near.group(1).strip().rstrip(",.")
        if 3 <= len(loc) <= 60:
            return loc

    # Trailing ", <location>" (e.g. ", Peebles" or ", Catterick")
    m_comma = re.search(r',\s+([A-Z][A-Za-z][A-Za-z0-9 \-]{2,35}?)\s*$', clean)
    if m_comma:
        loc = m_comma.group(1).strip().rstrip(",.")
        if re.match(r'^[A-Z]{2}\d', loc) or loc.lower() in ("gb", "uk"):
            pass
        elif 3 <= len(loc) <= 60:
            return loc

    return None

def parse_operator_from_title(title):
    """Try to identify service branch from title text."""
    t = title.lower()
    if any(x in t for x in ["raf", "royal air force", "air force"]):
        return "RAF"
    if any(x in t for x in ["royal navy", "rnas", "fleet air arm", "naval air"]):
        return "Royal Navy"
    if any(x in t for x in ["army", "army air corps", "aac"]):
        return "Army Air Corps"
    if "736 naval" in t or "825 naval" in t or "naval air squadron" in t:
        return "Royal Navy"
    return None

def parse_country_from_text(txt, title=""):
    """Try to determine accident country from narrative/title text.
    Returns ISO alpha-2 code or 'GB' as default.
    """
    combined = (title + " " + (txt or ""))[:3000]
    country_hints = {
        "Afghanistan": "AF", "Kabul": "AF", "Helmand": "AF", "Kandahar": "AF",
        "Iraq": "IQ", "Basra": "IQ",
        "Libya": "LY",
        "Germany": "DE", "Sennelager": "DE",
        "Falkland": "FK", "Mount Pleasant": "FK",
        "Cyprus": "CY",
        "Belize": "BZ",
        "Kenya": "KE",
        "Oman": "OM",
        "Bahrain": "BH",
        "Norway": "NO",
    }
    for keyword, code in country_hints.items():
        if re.search(r'\b' + keyword + r'\b', combined, re.I):
            return code
    return "GB"

def is_aviation_doc(title, group):
    """Filter to aviation-relevant documents only."""
    if group not in AVIATION_GROUPS:
        return False
    if title in AGGREGATE_SKIP_TITLES:
        return False
    has_serial = bool(SERIAL_RE.search(title))
    has_aircraft = bool(AIRCRAFT_WORDS.search(title))
    # MAAS are always aviation
    if "MAAS" in group or "Military Aircraft" in group:
        return True
    return has_serial or has_aircraft

# ─── PDF helpers ─────────────────────────────────────────────────────────────

def extract_text(path):
    if not path or not os.path.exists(path):
        return ""
    try:
        out = subprocess.run(
            ["pdftotext", "-q", str(path), "-"],
            capture_output=True, timeout=180,
        )
    except Exception:
        return ""
    return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""

def _ocr_remote(pdf_path, host):
    remote = f"/tmp/ocr-{uuid.uuid4().hex}.pdf"
    try:
        cp = subprocess.run(
            ["scp", "-q", str(pdf_path), f"{host}:{remote}"],
            capture_output=True, timeout=180,
        )
        if cp.returncode != 0:
            return ""
        cmd = (
            'f=$(mktemp); '
            'nice -n 19 ionice -c3 ocrmypdf --force-ocr --language eng '
            f'--sidecar "$f" --output-type none {shlex.quote(remote)} - >/dev/null 2>&1; '
            f'cat "$f"; rm -f "$f" {shlex.quote(remote)}'
        )
        run = subprocess.run(["ssh", host, cmd], capture_output=True, timeout=900)
        return run.stdout.decode("utf-8", "replace").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        try:
            subprocess.run(["ssh", host, f"rm -f {shlex.quote(remote)}"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        return ""

def ocr_extract(pdf_path):
    if not pdf_path:
        return ""
    host = os.environ.get("OCR_REMOTE")
    if host:
        return _ocr_remote(pdf_path, host)
    import tempfile
    fd, sidecar = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        try:
            subprocess.run(
                ["ocrmypdf", "--force-ocr", "--language", "eng",
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

# ─── Attachment selection ─────────────────────────────────────────────────────

def classify_part(title):
    """Return a part key from an attachment title.
    Returns one of: 'narrative', 'findings', 'causes', 'recommendations',
                    'cover', 'convening', 'authority', 'main', 'unknown'
    """
    t = title.lower()
    if "narrative" in t:
        return "narrative"
    if "finding" in t or "analysis" in t:
        return "findings"
    if "cause" in t:
        return "causes"
    if "recommendation" in t:
        return "recommendations"
    if "cover note" in t or "covering note" in t:
        return "cover"
    if "convening" in t or "terms of reference" in t:
        return "convening"
    if "authority comment" in t or "reviewing authority" in t:
        return "authority"
    # Single-PDF reports labelled after incident title
    return "main"

PREFERRED_PARTS = ["narrative", "findings", "causes", "main"]

def pick_pdf_strategy(attachments):
    """Return list of (url, part_key, size_bytes) for PDFs to fetch.

    Strategy:
    - Single PDF: fetch it (unless >60MB).
    - Multi-part: fetch narrative + findings + causes parts (prefer narrative first).
      Skip individual parts >60MB.
    All returned PDFs are within size limits.
    """
    pdfs = [
        {
            "title": a.get("title", ""),
            "url": a.get("url", ""),
            "size": a.get("file_size", 0) or 0,
            "part": classify_part(a.get("title", "")),
        }
        for a in attachments
        if isinstance(a, dict) and a.get("content_type") == "application/pdf"
           and a.get("url")
    ]
    if not pdfs:
        return []

    MAX_BYTES = MAX_PDF_MB * 1024 * 1024

    if len(pdfs) == 1:
        p = pdfs[0]
        if p["size"] > MAX_BYTES:
            return []  # will note as too large
        return [p]

    # Multi-part: collect narrative + findings + causes
    selected = []
    for key in PREFERRED_PARTS:
        parts_for_key = [p for p in pdfs if p["part"] == key]
        for p in parts_for_key:
            if p["size"] > MAX_BYTES:
                print(f"[ukmaa] SKIP part {p['title'][:50]}: {round(p['size']/1024/1024,1)}MB > {MAX_PDF_MB}MB",
                      file=sys.stderr, flush=True)
                continue
            selected.append(p)

    # If nothing matched preferred parts, take all under limit
    if not selected:
        selected = [p for p in pdfs if p["size"] <= MAX_BYTES]

    return selected

# ─── Stage: discover ──────────────────────────────────────────────────────────

def discover(c, cl):
    resp = cl.get(COLLECTION_URL)
    resp.raise_for_status()
    data = resp.json()

    all_docs = data.get("links", {}).get("documents", [])
    details = data.get("details", {})
    groups = details.get("collection_groups", [])

    # Build content_id → group_title map
    group_map = {}
    for g in groups:
        gtitle = g.get("title", "?")
        for cid in g.get("documents", []):
            group_map[cid] = gtitle

    inserted = 0
    for d in all_docs:
        if not isinstance(d, dict):
            continue
        dtitle = d.get("title", "")
        base_path = d.get("base_path", "")
        api_path = d.get("api_path", "")
        cid = d.get("content_id", "")
        group = group_map.get(cid, "Unknown")

        if not is_aviation_doc(dtitle, group):
            continue

        if dtitle in AGGREGATE_SKIP_TITLES:
            print(f"[ukmaa discover] SKIP aggregate digest: {dtitle[:60]}", flush=True)
            continue

        slug = slug_from_path(base_path)
        if not slug:
            continue

        if c.execute("SELECT 1 FROM ukmaa_reports WHERE slug=?", (slug,)).fetchone():
            continue

        # Fetch content page to get full attachment metadata
        time.sleep(DELAY)
        try:
            cresp = cl.get(f"https://www.gov.uk{api_path}")
            cresp.raise_for_status()
            cdata = cresp.json()
        except Exception as e:
            print(f"[ukmaa discover] ERROR fetching {api_path}: {e}", file=sys.stderr, flush=True)
            continue

        cdetails = cdata.get("details", {})
        body_html = cdetails.get("body", "")
        first_pub = cdata.get("first_published_at", "")[:10]
        attachments = cdetails.get("attachments", [])

        import json as _json
        pdf_parts = pick_pdf_strategy(attachments)
        pdf_part_json = _json.dumps([
            {"title": p["title"], "url": p["url"], "size": p["size"], "part": p["part"]}
            for p in pdf_parts
        ])

        # Primary URL = first narrative/main PDF; source URL = gov.uk page
        source_url = f"https://www.gov.uk{base_path}"
        main_pdf_url = pdf_parts[0]["url"] if pdf_parts else None

        report_type = _map_report_type(group, dtitle)

        c.execute(
            "INSERT OR IGNORE INTO ukmaa_reports "
            "(slug, title, group_name, first_published, body_html, pdf_part, "
            " main_pdf_url, status, discovered_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (slug, dtitle, group, first_pub, body_html, pdf_part_json,
             main_pdf_url, "new", now(), now())
        )
        c.commit()
        inserted += 1
        print(f"[ukmaa discover] +{slug[:60]}", flush=True)

    return inserted

def _map_report_type(group, title):
    t = title.lower()
    if "board of inquiry" in t or "Board of Inquiries" in group:
        return "Board of Inquiry"
    if "military aircraft accident summary" in t or "MAAS" in group or "Military Aircraft" in group:
        return "MAAS summary"
    return "Service Inquiry"

# ─── Stage: fetch ─────────────────────────────────────────────────────────────

def fetch(c, cl):
    import json as _json
    os.makedirs(PDFDIR, exist_ok=True)
    rows = c.execute(
        "SELECT slug, pdf_part FROM ukmaa_reports WHERE status='new'"
    ).fetchall()
    done = 0
    fails = 0
    for row in rows:
        slug = row["slug"]
        pdf_parts = _json.loads(row["pdf_part"] or "[]")

        if not pdf_parts:
            # No PDFs within size limit — mark fetched anyway (will use HTML)
            c.execute("UPDATE ukmaa_reports SET status='fetched', updated_at=? WHERE slug=?",
                      (now(), slug))
            c.commit()
            done += 1
            continue

        fetched_paths = []
        all_ok = True
        for part in pdf_parts:
            url = part["url"]
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", slug) + "__" + part["part"]
            # Append index if multiple same-part files
            dest = os.path.join(PDFDIR, f"{safe_name}.pdf")
            # Avoid clobbering same-part duplicates
            idx = 0
            while os.path.exists(dest) and dest not in [p["path"] for p in fetched_paths if "path" in p]:
                idx += 1
                dest = os.path.join(PDFDIR, f"{safe_name}_{idx}.pdf")

            time.sleep(DELAY)
            try:
                r = cl.get(url)
                ct = r.headers.get("content-type", "")
                if "pdf" not in ct.lower() and r.content[:4] != b"%PDF":
                    raise ValueError(f"not a PDF (ct={ct} status={r.status_code})")
                with open(dest, "wb") as fh:
                    fh.write(r.content)
                fetched_paths.append({"url": url, "path": dest, "part": part["part"]})
                print(f"[ukmaa fetch] {slug[:40]}/{part['part']}: {round(len(r.content)/1024,0)}KB", flush=True)
                fails = 0
            except Exception as e:
                print(f"[ukmaa fetch] ERROR {slug}/{part['part']}: {e}", file=sys.stderr, flush=True)
                all_ok = False
                fails += 1
                if fails >= 5:
                    print("[ukmaa fetch] 5 consecutive failures, aborting", file=sys.stderr)
                    return done

        if fetched_paths:
            main_path = fetched_paths[0]["path"]
            aux_paths = _json.dumps([p["path"] for p in fetched_paths[1:]])
            aux_urls  = _json.dumps([p["url"]  for p in fetched_paths[1:]])
            c.execute(
                "UPDATE ukmaa_reports SET main_pdf_path=?, aux_pdf_paths=?, aux_pdf_urls=?, "
                "status='fetched', updated_at=? WHERE slug=?",
                (main_path, aux_paths, aux_urls, now(), slug)
            )
        else:
            c.execute("UPDATE ukmaa_reports SET status='fetched', updated_at=? WHERE slug=?",
                      (now(), slug))
        c.commit()
        done += 1

    return done

# ─── Stage: parse ─────────────────────────────────────────────────────────────

def _concatenate_pdfs(main_path, aux_paths_json):
    """Concatenate pdftotext output from main + aux PDFs."""
    import json as _json
    texts = []
    for path in [main_path] + (_json.loads(aux_paths_json or "[]")):
        if path and os.path.exists(path):
            t = extract_text(path)
            if t:
                texts.append(t)
    return "\n\n".join(texts)

def parse(c, use_ocr=False):
    import json as _json
    rows = c.execute(
        "SELECT slug, main_pdf_path, aux_pdf_paths, body_html "
        "FROM ukmaa_reports WHERE status='fetched'"
    ).fetchall()
    parsed = 0
    for row in rows:
        slug = row["slug"]
        main_path = row["main_pdf_path"]
        aux_paths  = row["aux_pdf_paths"] or "[]"
        body_html  = row["body_html"] or ""

        # Concatenate all fetched PDFs
        txt = _concatenate_pdfs(main_path, aux_paths)

        if len(txt) >= FLOOR:
            tier = "pdf" if len(txt) >= MIN_NARRATIVE else "pdf"
            c.execute(
                "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
                "status='parsed', updated_at=? WHERE slug=?",
                (txt, tier, now(), slug)
            )
            c.commit()
            parsed += 1
            continue

        # Try OCR if enabled
        if use_ocr and main_path and os.path.exists(main_path):
            ocr_txt = ocr_extract(main_path)
            if len(ocr_txt) >= FLOOR:
                print(f"[ukmaa parse] {slug}: OCR yielded {len(ocr_txt)} chars", flush=True)
                c.execute(
                    "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
                    "status='parsed', updated_at=? WHERE slug=?",
                    (ocr_txt, "ocr", now(), slug)
                )
                c.commit()
                parsed += 1
                continue

        # Fall back to HTML body if it has useful content (>80 chars)
        from html.parser import HTMLParser
        class _P(HTMLParser):
            def __init__(self):
                super().__init__(); self.parts = []
            def handle_data(self, data):
                t = data.strip()
                if t: self.parts.append(t)

        p = _P()
        p.feed(body_html)
        plain_html = " ".join(p.parts).strip()
        if len(plain_html) >= FLOOR:
            print(f"[ukmaa parse] {slug}: using HTML body ({len(plain_html)} chars)", flush=True)
            c.execute(
                "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
                "status='parsed', updated_at=? WHERE slug=?",
                (plain_html, "html", now(), slug)
            )
            c.commit()
            parsed += 1
            continue

        # Below floor
        print(f"[ukmaa parse] {slug}: below floor (txt={len(txt)} html={len(plain_html)})",
              file=sys.stderr, flush=True)
        c.execute(
            "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
            "status='skipped', updated_at=? WHERE slug=?",
            (txt or plain_html, "none", now(), slug)
        )
        c.commit()

    return parsed

def parse_skipped(c):
    """Re-try OCR on skipped rows."""
    rows = c.execute(
        "SELECT slug, main_pdf_path FROM ukmaa_reports WHERE status='skipped'"
    ).fetchall()
    ok = 0; still_blank = 0
    for row in rows:
        slug = row["slug"]
        path = row["main_pdf_path"]
        if not path or not os.path.exists(path):
            print(f"[ukmaa parse-skipped] {slug}: PDF missing", file=sys.stderr, flush=True)
            continue
        ocr_txt = ocr_extract(path)
        if len(ocr_txt) >= FLOOR:
            c.execute(
                "UPDATE ukmaa_reports SET narrative_text=?, source_tier=?, "
                "status='parsed', updated_at=? WHERE slug=?",
                (ocr_txt, "ocr", now(), slug)
            )
            c.commit()
            ok += 1
        else:
            still_blank += 1
            print(f"[ukmaa parse-skipped] {slug}: OCR also blank ({len(ocr_txt)} chars)",
                  file=sys.stderr, flush=True)
    print(f"[ukmaa parse-skipped] ocr_ok={ok} still_blank={still_blank}", flush=True)
    return ok, still_blank

# ─── Stage: build ─────────────────────────────────────────────────────────────

_CAUSE_SECTION_RE = re.compile(
    r'(?:cause[s]?|causal factors?|summary of findings)[:\s\-]*\n([^\n]{20,}(?:\n[^\n]{10,}){0,10})',
    re.I
)

def extract_probable_cause(text):
    if not text:
        return None
    m = _CAUSE_SECTION_RE.search(text)
    if m:
        snippet = m.group(1).strip()
        snippet = re.sub(r'\s+', ' ', snippet)
        return snippet[:1000] or None
    return None

def build(c):
    rows = c.execute(
        "SELECT slug, title, group_name, first_published, narrative_text, "
        "source_tier, main_pdf_url, body_html, aux_pdf_urls "
        "FROM ukmaa_reports WHERE status='parsed'"
    ).fetchall()
    built = 0
    skipped = 0
    for r in rows:
        narr = r["narrative_text"] or ""
        tier = r["source_tier"] or "none"
        slug = r["slug"]

        if tier not in ("pdf", "ocr", "html") or len(narr) < FLOOR:
            c.execute("UPDATE ukmaa_reports SET status='skipped', updated_at=? WHERE slug=?",
                      (now(), slug))
            c.commit()
            skipped += 1
            continue

        title = r["title"] or ""
        group = r["group_name"] or ""
        body_html = r["body_html"] or ""

        # ── Metadata extraction ──
        # Try title first, then slug, then body_html, then narrative
        event_date = (parse_date_from_text(title)
                      or parse_date_from_text(slug)
                      or parse_date_from_text(body_html)
                      or parse_date_from_text(narr[:2000]))
        registration = parse_registration(title) or parse_registration(narr[:1000])
        aircraft = parse_aircraft_from_title(title)
        location = parse_location_from_title(title)
        operator = parse_operator_from_title(title) or parse_operator_from_text(narr[:2000])
        country = parse_country_from_text(narr[:3000], title)
        probable_cause = extract_probable_cause(narr)
        report_type = _map_report_type(group, title)
        source_url = f"https://www.gov.uk/government/publications/{slug}"

        c.execute(
            "INSERT OR REPLACE INTO ukmaa_accidents "
            "(case_id, event_date, aircraft, registration, operator, location, country, "
            " narrative_text, probable_cause, source_url, report_type, site_slug, lang, built_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (case_id(slug), event_date, aircraft, registration, operator, location, country,
             narr, probable_cause, source_url, report_type,
             site_slug_fn(event_date, aircraft, registration, location),
             "en", now())
        )
        c.execute("UPDATE ukmaa_reports SET status='built', updated_at=? WHERE slug=?",
                  (now(), slug))
        c.commit()
        built += 1

    return built, skipped

def parse_operator_from_text(txt):
    """Try to determine operator from narrative text."""
    if not txt:
        return None
    t = txt[:2000]
    if re.search(r'\bRoyal Air Force\b|\bRAF\b', t):
        return "RAF"
    if re.search(r'\bRoyal Navy\b|\bFleet Air Arm\b|\bRNAS\b|\bNaval\b', t):
        return "Royal Navy"
    if re.search(r'\bArmy Air Corps\b|\bAAC\b', t):
        return "Army Air Corps"
    return None

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(PDFDIR, exist_ok=True)
    c = db_connect()

    if mode in ("discover", "all"):
        cl = http_client()
        try:
            n = discover(c, cl)
            print(f"discovered: {n}")
        finally:
            cl.close()

    if mode in ("fetch", "all"):
        cl = http_client()
        try:
            n = fetch(c, cl)
            print(f"fetched: {n}")
        finally:
            cl.close()

    if mode in ("parse", "all"):
        n = parse(c, use_ocr=False)
        print(f"parsed: {n}")

    if mode == "parse-ocr":
        n = parse(c, use_ocr=True)
        print(f"parsed (with OCR): {n}")

    if mode == "parse-skipped":
        ok, blank = parse_skipped(c)
        print(f"parse-skipped: ocr_ok={ok} still_blank={blank}")

    if mode in ("build", "all", "parse-skipped", "parse-ocr"):
        b, sk = build(c)
        print(f"built: {b}  skipped: {sk}")

    print("reports:", list(c.execute("SELECT status, count(*) FROM ukmaa_reports GROUP BY status")))
    print("accidents:", c.execute("SELECT count(*) FROM ukmaa_accidents").fetchone()[0])
    print()
    print("Sample accidents:")
    for row in c.execute("SELECT case_id, event_date, aircraft, registration, location, country, report_type, lang FROM ukmaa_accidents LIMIT 5"):
        print(dict(row))

if __name__ == "__main__":
    main()
