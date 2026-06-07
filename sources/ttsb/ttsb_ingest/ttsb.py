# ttsb_ingest/ttsb.py
"""
TTSB Taiwan (Taiwan Transportation Safety Board, ttsb.gov.tw) aviation
completed-investigations listing + bilingual report parser.

⚠️ NAME NOTE: this source is TTSB (Taiwan). It is NOT the TSB-Canada source.
Every identifier here is strictly `ttsb_` / `ttsb-` prefixed so greps never
collide with the Canadian `tsb` package.

Source shape (Umbraco CMS, server-rendered, NO anti-bot — plain curl + a
browser User-Agent → HTTP 200; verifies cleanly under httpx+certifi):

  EN completed-investigations list (paginated, 30 rows/page, ?Page=1..5):
      https://www.ttsb.gov.tw/english/16051/16052/16053/16058/Lpsimplelist
  ZH (Traditional-Chinese) mirror list (same 149-report set):
      https://www.ttsb.gov.tw/1133/1154/1155/1159/Lpsimplelist

Both lists are `<table class="rwd-table table">` whose data <tr>s carry, per
report: NO. | Date | Title (the row's detail `/post` link) | Aircraft Model |
Location | Report (an inline `/media/{id}/….pdf` link OR a 'More Reports' link
to the detail page when the PDF lives only on the detail page).

⚠️ DETAIL-NODE PREFIX ≠ LIST PATH. Each list page is served under one node
path but the per-row detail `/post` links live under a DIFFERENT node prefix:
    EN row detail:  /english/18609/18610/{id}/post
    ZH row detail:  /1243/16869/{id}/post
The page is full of OTHER `/post` links (site chrome, related items, some with
doubled slashes). We match data rows ONLY by the detail-node prefix, never by
the list URL's own path, and never by a bare `/post` suffix.

⚠️ ZH DATES ARE ROC (民國) CALENDAR: '113-11-04' = ROC-113 → 2024-11-04
(ROC year + 1911). EN dates are already Gregorian ISO ('2024-11-04').

⚠️ EN-SUMMARY vs ZH-FULL: recent EN entries are often Executive Summaries
(~5K chars) while the matching ZH PDF is the FULL report (100K+ chars). The
fetch stage downloads the EN PDF; if its text < _ZH_FULL_THRESHOLD chars AND a
matching ZH PDF exists, it ALSO downloads the ZH full report and PREFERS it as
narrative_text (lang='zh'), keeping the EN summary in en_summary_text. If the
EN PDF is already full (older finals, 100K+ chars) → narrative=EN, lang='en'.

EN↔ZH rows are matched (both lists are the same 149 set) by occurrence date +
registration, falling back to date + aircraft model when neither row carries a
registration in its title cell.

case_id derivation priority (stable across weekly re-runs):
  (1) TTSB/ASC report number extracted from the PDF text
      (e.g. 'TTSB-AOR-25-11-001', 'ASC-AOR-...');
  (2) the media-filename slug (e.g. 'b-86002', 'ci7916', 'jj2258');
  (3) last resort 'ttsb-{detailId}'.
The discover stage seeds case_id from (2)/(3) (no PDF yet); fetch upgrades it
to (1) when a report number surfaces in the text — but only when that does not
collide with an already-built case_id.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://www.ttsb.gov.tw"
DELAY = 1.0

# EN list path + its row detail-node prefix.
EN_LIST_TMPL = (
    "https://www.ttsb.gov.tw/english/16051/16052/16053/16058/"
    "Lpsimplelist?Page={page}"
)
EN_DETAIL_PREFIX = "/english/18609/18610/"
# ZH list path + its row detail-node prefix.
ZH_LIST_TMPL = "https://www.ttsb.gov.tw/1133/1154/1155/1159/Lpsimplelist?Page={page}"
ZH_DETAIL_PREFIX = "/1243/16869/"

# The EN/ZH lists currently span 5 pages (149 reports, 30/page). Walked in full.
NUM_PAGES = 5

# Below this many chars an EN report is treated as a (likely Executive Summary)
# stub and the ZH full report is preferred when one exists.
ZH_FULL_THRESHOLD = 15000

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_TR_RE = re.compile(r"(?is)<tr[>\s].*?</tr>")
_CELL_RE = re.compile(r'(?is)<td[^>]*data-th="([^"]*)"[^>]*>(.*?)</td>')
_MEDIA_HREF_RE = re.compile(r'href="(/media/[^"]+\.pdf)"', re.IGNORECASE)

# Gregorian ISO date in the EN list ('2024-11-04').
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# ROC date in the ZH list ('113-11-04', ROC year is 2-3 digits). No leading
# \b: the value may be prefixed by a CJK char ('民國113-…') which is itself a
# word char, so there is no boundary before the digits.
_ROC_DATE_RE = re.compile(r"(\d{2,3})-(\d{1,2})-(\d{1,2})\b")

# Taiwan civil registration 'B-NNNNN', plus the drone class 'B-AAANNNN'.
# Drone first so the longer prefix wins.
_REG_DRONE_RE = re.compile(r"\bB-AAA[0-9]{3,5}\b", re.IGNORECASE)
_REG_CIVIL_RE = re.compile(r"\bB-[0-9]{4,5}\b", re.IGNORECASE)

# TTSB/ASC report number inside the PDF text (case_id priority 1).
# Forms seen: TTSB-AOR-25-11-001, ASC-AOR-..., ASC-AAR-..., TTSB-MOR-...
_REPORT_NO_RE = re.compile(
    r"\b((?:TTSB|ASC)-(?:AOR|AAR|MOR|ASR)-[0-9]{2,4}-[0-9]{1,2}-[0-9]{1,3})\b",
    re.IGNORECASE,
)


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def detail_id_from_url(detail_url, prefix):
    """The numeric node id of a row's detail `/post` link under `prefix`."""
    if not detail_url:
        return None
    m = re.search(re.escape(prefix) + r"(\d+)/post\b", detail_url)
    return m.group(1) if m else None


def roc_to_iso(text):
    """
    ROC (民國) date '113-11-04' → ISO '2024-11-04' (ROC year + 1911).
    Returns None when unparseable. ROC years are 2-3 digits (≤ ~150).
    """
    if not text:
        return None
    m = _ROC_DATE_RE.search(text)
    if not m:
        return None
    roc_y, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mon <= 12 and 1 <= day <= 31):
        return None
    return f"{roc_y + 1911}-{mon:02d}-{day:02d}"


def parse_iso_date(text):
    """Gregorian 'YYYY-MM-DD' (EN list) → normalised ISO, else None."""
    if not text:
        return None
    m = _ISO_DATE_RE.search(text)
    if not m:
        return None
    year, mon, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mon <= 12 and 1 <= day <= 31):
        return None
    return f"{year}-{mon:02d}-{day:02d}"


def extract_registration(text):
    """
    Best-effort Taiwan registration from any text (title / PDF). Returns the
    canonical upper-case form ('B-86002', 'B-AAA0139') or None. Drone class
    (B-AAA…) wins over a plain civil match.
    """
    if not text:
        return None
    d = _REG_DRONE_RE.search(text)
    if d:
        return d.group(0).upper()
    c = _REG_CIVIL_RE.search(text)
    return c.group(0).upper() if c else None


def is_drone(registration):
    """True for the B-AAA… drone/UAS registration class."""
    return bool(registration) and registration.upper().startswith("B-AAA")


def media_slug(pdf_url):
    """
    The filename stem of a /media/…pdf URL, lower-cased and cleaned, used as a
    case_id fallback (priority 2). e.g.
    '/media/9314/b-86002_executivesummary.pdf' → 'b-86002'.
    Strips common report-suffix noise so the slug centres on the case token.
    """
    if not pdf_url:
        return None
    name = pdf_url.rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    low = name.lower()
    # Drop the report-type words so 'b-86002_executivesummary' → 'b-86002'.
    low = re.sub(
        r"[-_ ]*(executive[-_ ]*summary|executivesummary|final[-_ ]*report|"
        r"finalreport|investigation[-_ ]*report|report|summary)",
        "",
        low,
    )
    low = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    return low or None


def report_number(text):
    """TTSB/ASC report number from PDF text (case_id priority 1), or None."""
    if not text:
        return None
    m = _REPORT_NO_RE.search(text)
    return m.group(1).upper() if m else None


def derive_case_id(report_no, pdf_url, detail_id):
    """
    case_id priority chain: (1) report number → (2) media-filename slug →
    (3) 'ttsb-{detailId}'.
    """
    if report_no:
        return report_no.upper()
    slug = media_slug(pdf_url)
    if slug:
        return slug
    if detail_id:
        return f"ttsb-{detail_id}"
    return None


def report_kind_from_label(label, pdf_url=None):
    """
    Map the row's Report-column label / PDF filename to a report_kind.
    'Final Report' → 'Final'; 'Executive Summary' → 'Executive Summary';
    'More Reports' (older detail-only entries) → 'Report'.
    """
    low = (label or "").lower()
    if "final" in low:
        return "Final"
    if "executive" in low or "summary" in low:
        return "Executive Summary"
    name = (pdf_url or "").lower()
    if "final" in name:
        return "Final"
    if "executivesummary" in name or "executive-summary" in name \
            or "summary" in name:
        return "Executive Summary"
    return "Report"


def parse_listing(page_html, detail_prefix, lang="en"):
    """
    Parse one EN or ZH list page → list of row dicts in document order. Each:
        detail_id, detail_url, pdf_url (inline /media or None), report_label,
        report_kind, aircraft, location, registration (from title, best-effort),
        title, event_date (ISO|None), lang.

    Rows are matched ONLY by `detail_prefix` (the detail-node prefix, NOT the
    list URL path). Inline /media PDFs are harvested per row; 'More Reports'
    rows have no inline PDF (pdf_url=None) and need a detail fetch downstream.
    Dates are parsed per language: EN = Gregorian ISO, ZH = ROC → ISO.
    """
    out = []
    seen = set()
    href_re = re.compile(
        r'href="(' + re.escape(detail_prefix) + r'\d+/post)"', re.IGNORECASE
    )
    title_re = re.compile(
        r'href="' + re.escape(detail_prefix) + r'\d+/post"[^>]*title="([^"]*)"',
        re.IGNORECASE,
    )
    for tr in _TR_RE.findall(page_html or ""):
        m = href_re.search(tr)
        if not m:
            continue
        detail_url = urljoin(BASE, m.group(1))
        detail_id = detail_id_from_url(detail_url, detail_prefix)
        if detail_id in seen:
            continue
        seen.add(detail_id)

        cells = {label.strip(): _strip(val)
                 for label, val in _CELL_RE.findall(tr)}

        # Date cell label differs EN vs ZH; take whichever date-like cell parses.
        date_raw = (cells.get("Date") or cells.get("事故時間")
                    or cells.get("Date ") or "")
        event_date = (parse_iso_date(date_raw) if lang == "en"
                      else roc_to_iso(date_raw))

        aircraft = (cells.get("Aircraft Model") or cells.get("事故機型")
                    or None)
        location = (cells.get("Location") or cells.get("事故地點") or None)
        report_label = (cells.get("Report") or cells.get("事故報告") or None)

        tm = title_re.search(tr)
        title = _html.unescape(tm.group(1)).strip() if tm else (
            cells.get("Title") or cells.get("標題/案件編號") or detail_id)

        media = _MEDIA_HREF_RE.findall(tr)
        pdf_url = urljoin(BASE, _html.unescape(media[0])) if media else None

        registration = extract_registration(title) or \
            extract_registration(pdf_url)

        out.append({
            "detail_id": detail_id,
            "detail_url": detail_url,
            "pdf_url": pdf_url,
            "report_label": report_label,
            "report_kind": report_kind_from_label(report_label, pdf_url),
            "aircraft": aircraft,
            "location": location,
            "registration": registration,
            "title": title,
            "event_date": event_date,
            "lang": lang,
        })
    return out


def _match_key(rec):
    """Coarse EN↔ZH match key: (event_date, registration-or-aircraft)."""
    second = rec.get("registration") or (rec.get("aircraft") or "").lower()
    return (rec.get("event_date"), second)


def match_en_zh(en_rows, zh_rows):
    """
    Pair EN rows with their ZH counterparts (same 149-report set). Returns a
    dict en_detail_id → zh_row. Match priority per EN row:
        1. (event_date, registration)   — strongest;
        2. (event_date, aircraft model) — when both carry the same model;
        3. (event_date) alone           — last resort, since the EN aircraft
           string ('Ultra Light/Storch') and the ZH one ('超輕型載具/STORCH')
           differ across languages and many reports have no listing reg.

    A ZH row is consumed at most once, so date-only stays unambiguous when a
    date has a single report (the normal case).
    """
    by_reg, by_air, by_date = {}, {}, {}
    for z in zh_rows:
        d = z.get("event_date")
        if not d:
            continue
        if z.get("registration"):
            by_reg.setdefault((d, z["registration"]), []).append(z)
        if z.get("aircraft"):
            by_air.setdefault((d, (z["aircraft"] or "").lower()), []).append(z)
        by_date.setdefault(d, []).append(z)

    used = set()
    pairs = {}

    def _take(bucket):
        for cand in bucket:
            if id(cand) not in used:
                return cand
        return None

    for e in en_rows:
        d = e.get("event_date")
        if not d:
            continue
        z = None
        if e.get("registration"):
            z = _take(by_reg.get((d, e["registration"]), []))
        if z is None and e.get("aircraft"):
            z = _take(by_air.get((d, (e["aircraft"] or "").lower()), []))
        if z is None:
            z = _take(by_date.get(d, []))
        if z is not None:
            used.add(id(z))
            pairs[e["detail_id"]] = z
    return pairs


def choose_narrative(en_text, zh_text):
    """
    Decide the narrative_text + lang given the EN report text and (optional) ZH
    full-report text.

    - EN already full (>= ZH_FULL_THRESHOLD) → (en_text, 'en', en_summary=None).
    - EN is a stub (< threshold) AND a usable ZH full report exists AND the ZH
      text is longer → (zh_text, 'zh', en_summary=en_text).
    - Otherwise (no/short ZH) → keep EN, lang 'en', en_summary=None.
    """
    en_text = en_text or ""
    zh_text = zh_text or ""
    if len(en_text) >= ZH_FULL_THRESHOLD:
        return en_text, "en", None
    if zh_text and len(zh_text) > len(en_text):
        return zh_text, "zh", (en_text or None)
    return en_text, "en", None


# ──────────────────────────────────────────────────────────────────────────────
# Detail-page PDF harvest (older 'More Reports' rows whose list cell has no
# inline /media link — the report PDF lives on the /post detail page).
# ──────────────────────────────────────────────────────────────────────────────


def pdf_from_detail(detail_html):
    """First /media/…pdf href on a detail `/post` page (absolute), or None."""
    if not detail_html:
        return None
    m = _MEDIA_HREF_RE.search(detail_html)
    return urljoin(BASE, _html.unescape(m.group(1))) if m else None


def registration_from_detail(detail_html):
    """Best-effort registration from a detail page's narrative text."""
    return extract_registration(_strip(detail_html))


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def en_list_url(page):
    return EN_LIST_TMPL.format(page=page)


def zh_list_url(page):
    return ZH_LIST_TMPL.format(page=page)


def fetch_page(client, url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                import time
                time.sleep(DELAY * (attempt + 1))
    raise last


def download_pdf(client, url, dest_path, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            return dest_path
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                import time
                time.sleep(DELAY * (attempt + 1))
    raise last
