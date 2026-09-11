# nbaai_ingest/nbaai.py
"""NBAAI (Ukraine) HTML scraper.

Source: https://nbaai.gov.ua  — National Bureau of Air Accident Investigation
of Ukraine (Національне бюро з розслідування авіаційних подій … / НБРТ).

Discovery: the WordPress 'enquiry' custom-post sitemap
(https://nbaai.gov.ua/enquiry-sitemap.xml) lists every investigation detail
page (~77 reports).  Each /enquiry/<slug>/ detail page is server-rendered and
contains:
  • <h1> event title (aircraft + registration + location, Ukrainian).
  • a <div class="...content-text"> narrative body in clean Unicode Ukrainian
    (present for MOST reports — NBAAI publishes the narrative inline).
  • optionally an attached final-report PDF under /wp-content/uploads/*.pdf
    (rare: only a minority of reports).  These PDFs are EITHER clean Unicode
    Cyrillic, OR scanned-image / non-Unicode-font → handled by the OCR fallback
    in pdf.py at parse() time.
  • a publish date (JSON-LD datePublished) and an event date 'DD.MM.YYYY'
    embedded in the body.

case_id is INTRINSIC: registration (Latin form from the URL slug) + event date,
falling back to the slug when neither is available.
"""
import datetime
import html as _html
import re

BASE = "https://nbaai.gov.ua"
SITEMAP_URL = BASE + "/enquiry-sitemap.xml"
REFERER = BASE + "/enquiry/"
DELAY = 1.8

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# ── regexes ──────────────────────────────────────────────────────────────────

_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>", re.IGNORECASE)

# Detail-page enquiry URLs (exclude the listing root itself and paginated archives)
_ENQUIRY_URL_RE = re.compile(
    r"^https?://nbaai\.gov\.ua/enquiry/(?!page/)([^/]+)/?$", re.IGNORECASE
)

_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)

# Narrative body block: <div ... class="... content-text ...">(body)</div>.
# We locate the OPENING tag only and then walk div-nesting depth (see
# _extract_content_text); a naive non-greedy ".*?</div>" would stop at the FIRST
# nested </div> and silently drop everything after it (e.g. the causal-factors
# section).  This source is 75/76 HTML-bulk, so robustness here is load-bearing.
_CONTENT_OPEN_RE = re.compile(
    r'<div\b[^>]*class="[^"]*content-text[^"]*"[^>]*>', re.IGNORECASE
)
_DIV_TOKEN_RE = re.compile(r"<div\b|</div\s*>", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)

# JSON-LD published date
_DATEPUB_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')

# Event date DD.MM.YYYY anywhere in the narrative body
_EVENT_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")

# Latin registration mark embedded in the URL slug (covers UR- + foreign)
_SLUG_REG_RE = re.compile(
    r"(?:^|-)("
    r"(?:ur|n|d|f|ok|g|sp|yr|ra|ha|4x|oe|oy|es|ly|ew|hb|ph|cs|tc|ec|i|lz|9a|om|s5)"
    r"-[a-z0-9]{2,6})(?:-|$)",
    re.IGNORECASE,
)

# Report-PDF hrefs under /wp-content/uploads/ (any year), excluding images,
# the boilerplate "rules" PDF, and monthly information-bulletin digests.
_PDF_HREF_RE = re.compile(
    r'href="([^"]*/wp-content/uploads/[^"]+\.pdf)"', re.IGNORECASE
)

# Event classification keywords (Ukrainian)
_CATASTROPHE = ("катастроф",)            # fatal accident
_ACCIDENT = ("аварі", "авіаційна подія")  # accident
_INCIDENT = ("інцидент",)                # (serious) incident


def make_client():
    """Return an httpx.Client configured with browser UA + Referer."""
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


# ── discovery ────────────────────────────────────────────────────────────────

def iter_enquiry_urls(sitemap_xml: str) -> list[str]:
    """Parse enquiry-sitemap.xml → ordered, de-duplicated list of detail URLs.

    Excludes the /enquiry/ listing root and /enquiry/page/N/ archive pages.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for m in _LOC_RE.finditer(sitemap_xml):
        loc = _html.unescape(m.group(1)).strip()
        if not _ENQUIRY_URL_RE.match(loc):
            continue
        url = loc if loc.endswith("/") else loc + "/"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ── detail-page parsing ──────────────────────────────────────────────────────

def slug_of(url: str) -> str:
    m = re.search(r"/enquiry/([^/]+)/?$", url)
    return m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]


def _reg_from_slug(slug: str) -> str | None:
    m = _SLUG_REG_RE.search(slug)
    return m.group(1).upper() if m else None


def _classify(text: str) -> str:
    low = (text or "").lower()
    if any(k in low for k in _CATASTROPHE):
        return "Accident (fatal)"
    if any(k in low for k in _INCIDENT):
        return "Serious incident" if "серйоз" in low else "Incident"
    if any(k in low for k in _ACCIDENT):
        return "Accident"
    return "Accident"


def _aircraft_from_title(title: str, registration: str | None) -> str | None:
    """Heuristic aircraft-type extraction from the H1 title.

    Title patterns: 'Катастрофа вертольота R-44 UR-KTB',
    'Серйозний інцидент з літаком Boeing 737-800 UR-SQP'.
    We grab the token cluster preceding the registration, after a type word.
    """
    if not title:
        return None
    t = re.sub(r"\s+", " ", title).strip()
    # Cut at the registration if known
    if registration:
        idx = t.upper().find(registration.upper())
        if idx > 0:
            t = t[:idx].strip()
    # Drop leading classification + connector words
    t = re.sub(
        r"^(?:.*?(?:літаком|літака|вертольота|вертольотом|вертолота|з\s+ПС|з\s+літаком|"
        r"з\s+вертольотом|літак|вертоліт|ПС|планера|планером))\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = t.strip(" ,.-")
    return t or None


def _extract_content_text(html_text: str) -> str:
    """Return the COMPLETE inner HTML of <div class="...content-text...">.

    Depth-aware: from the opening content-text <div>, walk every <div>/</div>
    token tracking nesting depth and stop at the </div> that brings depth back to
    0 — so nested <div> blocks (and anything after them) are preserved.  A naive
    non-greedy regex truncates at the first nested </div>.  <script>/<style>
    blocks (e.g. an embedded Leaflet map) are removed.
    """
    if not html_text:
        return ""
    om = _CONTENT_OPEN_RE.search(html_text)
    if not om:
        return ""
    inner_start = om.end()
    depth = 1
    end = len(html_text)
    for tm in _DIV_TOKEN_RE.finditer(html_text, inner_start):
        if tm.group().lower().startswith("</div"):
            depth -= 1
            if depth == 0:
                end = tm.start()
                break
        else:
            depth += 1
    inner = html_text[inner_start:end]
    return _SCRIPT_STYLE_RE.sub(" ", inner)


# ── registration from the narrative body (secondary source) ───────────────────

# Cyrillic→Latin map for registration-mark letters that visually collide
# (e.g. Cyrillic А/В/Е/К/М/Н/О/Р/С/Т/Х/І/Ї → Latin equivalents).
_CYR2LAT = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I",
    "Ї": "I", "У": "Y", "а": "A", "в": "B", "е": "E", "к": "K",
    "м": "M", "н": "H", "о": "O", "р": "P", "с": "C", "т": "T",
    "х": "X", "і": "I", "ї": "I", "у": "Y",
})

# Registration marks in the narrative body — Ukrainian UR-/УР- plus common
# foreign prefixes.  Accepts a Latin OR Cyrillic-rendered prefix and suffix; the
# whole match is transliterated to Latin before validation.  Bounded by
# non-alphanumeric so we don't grab fragments of longer tokens.
_BODY_REG_RE = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яІіЇї])"
    r"((?:UR|УР|4X|RA|EW|N|D|F|OK|G|SP|YR|HA|OE|OY|ES|LY|HB|PH|CS|TC|EC|LZ|9A|OM|S5)"
    r"[\u2010-\u2015\-]"
    r"[0-9A-Za-zА-Яа-яІіЇї]{2,6})"
    r"(?![0-9A-Za-zА-Яа-яІіЇї])",
    re.IGNORECASE,
)


def _reg_from_body(text_plain: str) -> str | None:
    """Extract a registration mark from the narrative body (Latin or Cyrillic).

    Cyrillic-rendered marks (e.g. 'UR-СІС') are transliterated to Latin
    ('UR-SIS').  Returns the first plausible mark, normalised to a single ASCII
    dash + upper-case, or None.  Display/dedup field only — never feeds case_id.
    """
    if not text_plain:
        return None
    m = _BODY_REG_RE.search(text_plain)
    if not m:
        return None
    raw = m.group(1)
    # Normalise dash variants to ASCII '-' first.
    raw = re.sub(r"[\u2010-\u2015]", "-", raw)
    # The Ukrainian nationality prefix is rendered '\u0423\u0420' in Cyrillic but is the
    # ICAO 'UR' mark \u2014 map it explicitly (the general visual map would give 'YP').
    raw = re.sub(r"^\u0423\u0420(?=-)", "UR", raw, flags=re.IGNORECASE)
    lat = raw.translate(_CYR2LAT).upper()
    lat = re.sub(r"-{2,}", "-", lat)
    # Reject if a non-ASCII alnum survived translation (not a real Latin reg).
    if re.search(r"[^A-Z0-9-]", lat):
        return None
    return lat


def parse_detail(html_text: str, url: str) -> dict:
    """Parse one /enquiry/<slug>/ detail page → a report dict.

    Keys: case_id, report_url, pdf_url, title, event_class, aircraft,
          registration, date_of_occurrence, location, narrative_html.
    """
    slug = slug_of(url)

    # title
    h1_m = _H1_RE.search(html_text)
    title = ""
    if h1_m:
        title = _html.unescape(re.sub(r"<[^>]+>", "", h1_m.group(1))).strip()

    # narrative body (HTML fragment; pipeline strips tags) — depth-aware so a
    # nested <div> inside content-text does not truncate the narrative.
    narrative_html = _extract_content_text(html_text)

    # registration: the Latin form from the URL slug is the STABLE source and is
    # the ONLY one that feeds case_id (slug_reg).  When the slug has no Latin reg
    # (Cyrillic-titled reports), fall back to a mark mined from the narrative
    # body for the `registration` display/dedup column ONLY — body-reg must NEVER
    # influence case_id (keeps case_ids intrinsic & stable across re-ingests).
    slug_reg = _reg_from_slug(slug)
    registration = slug_reg

    # event date: prefer DD.MM.YYYY from the narrative body, else publish date
    date_iso = None
    body_plain = re.sub(r"<[^>]+>", " ", narrative_html)

    if not registration:
        body_reg = _reg_from_body(body_plain) or _reg_from_body(title)
        if body_reg:
            registration = body_reg
    dm = _EVENT_DATE_RE.search(body_plain) or _EVENT_DATE_RE.search(title)
    if dm:
        d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        try:
            date_iso = datetime.date(y, mo, d).isoformat()
        except ValueError:
            date_iso = None
    if date_iso is None:
        pm = _DATEPUB_RE.search(html_text)
        if pm:
            date_iso = pm.group(1)[:10]

    # PDF report (first non-image, non-boilerplate uploads PDF)
    pdf_url = None
    for m in _PDF_HREF_RE.finditer(html_text):
        href = _html.unescape(m.group(1))
        low = href.lower()
        if "rules-" in low or "cropped" in low or "№610" in low:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = BASE + href
        pdf_url = href
        break

    aircraft = _aircraft_from_title(title, registration)
    event_class = _classify(title)

    return {
        "case_id": make_case_id(slug_reg, date_iso, slug),
        "report_url": url,
        "pdf_url": pdf_url,
        "title": title,
        "event_class": event_class,
        "aircraft": aircraft,
        "registration": registration,
        "date_of_occurrence": date_iso,
        "location": None,
        "narrative_html": narrative_html,
    }


# ── case_id ──────────────────────────────────────────────────────────────────

def make_case_id(registration, date_iso, slug):
    """Construct an INTRINSIC case_id: NBAAI-<REG>-<YYYY-MM-DD>.

    Falls back to NBAAI-<REG> when no date, NBAAI-<slug> when no registration.
    """
    reg = _normalize_case_id(registration) if registration else None
    if reg and date_iso:
        return f"NBAAI-{reg}-{date_iso}"
    if reg:
        return f"NBAAI-{reg}"
    return f"NBAAI-{_normalize_case_id(slug)}"


def _normalize_case_id(raw):
    """Idempotent normaliser: upper-case, collapse separators to single dash."""
    if not raw:
        return None
    s = re.sub(r"[\s_]+", "-", str(raw).strip().upper())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


# ── download ─────────────────────────────────────────────────────────────────

def download(client, pdf_url: str, dest) -> None:
    """GET pdf_url with Referer and write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
