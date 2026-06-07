# rnsa_ingest/rnsa.py
"""
RNSA Iceland (Rannsóknarnefnd samgönguslysa — Icelandic Transportation Safety
Board, aviation section, rnsa.is) per-year archive listing parser.

The aviation catalogue is a set of PER-YEAR archive pages:
    https://rnsa.is/flug/slysa-og-atvikaskyrslur/{YEAR}/
Years 2009..current are live; future/unpublished years return HTTP 404 and are
walked gracefully (we also probe current-year+1 so a freshly-published year is
never missed). The paginated `?page=N` view is a SUBSET — we deliberately walk
the full per-year set instead.

⚠️ The site is plain ASP.NET server-rendered HTML, no anti-bot — curl/httpx
with a browser UA works, no TLS quirk.

Each report lives in a `<div class="item">` block carrying:
    <h3>…</h3>            — Icelandic/English title (registrations, place)
    <p>…</p>             — summary paragraph(s)
    <a … href="/media/{id}/{slug}.pdf">Skýrsla</a>  — the report PDF
The h3 link text is generic ("Skýrsla") but FILENAMES are metadata-rich:
    lokaskyrsla-tf-kff-tf-kfg-flugumferdaratvik-a-bikf-23-mai-2020.pdf
carrying registration(s) (TF-xxx, sometimes two), event type, airport ICAO
(BIKF/BIEG), and an Icelandic-or-English date. We parse best-effort metadata
from the FILENAME; the year comes authoritatively from the year-page URL.

report_type (final vs interim/preliminary) is read from filename keywords:
    lokaskyrsla / bradabirgdaskyrsla / final-report / interim-report.

⚠️ Some /media ids are notification forms / blank-forms, not reports
(tilkynning / eyðublað). We only ingest PDFs actually listed inside a year
page's item blocks, and additionally drop obvious form filenames.

case_id = the numeric media id from /media/{id}/ (stable, permanent, unique).
"""
import datetime
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://rnsa.is"
DELAY = 1.0

# First aviation archive year that exists on the site. We walk from here up to
# (current calendar year + 1) so newly-published years are picked up; 404s are
# tolerated (handled in pipeline.discover).
FIRST_YEAR = 2009


def year_pages(first_year=FIRST_YEAR, last_year=None):
    """
    The per-year archive URLs to walk, oldest→newest. `last_year` defaults to
    the current calendar year + 1 (probe the future so a freshly published
    year is never missed). 404s are tolerated by the caller.
    """
    if last_year is None:
        last_year = datetime.date.today().year + 1
    return [
        f"{BASE}/flug/slysa-og-atvikaskyrslur/{y}/"
        for y in range(first_year, last_year + 1)
    ]


# Stable hardcoded URL list for the verified live range (kept for parity with
# sibling sources / API symmetry). pipeline.discover uses year_pages().
YEAR_PAGES = year_pages()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "is,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Icelandic + English month names/abbrevs → month number (filenames mix both).
_MONTHS = {
    "januar": 1, "janar": 1, "jan": 1, "january": 1,
    "februar": 2, "feb": 2, "february": 2,
    "mars": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "jun": 6, "june": 6,
    "juli": 7, "jul": 7, "july": 7,
    "agust": 8, "aug": 8, "august": 8,
    "september": 9, "sept": 9, "sep": 9,
    "oktober": 10, "okt": 10, "oct": 10, "october": 10,
    "november": 11, "nov": 11,
    "desember": 12, "des": 12, "dec": 12, "december": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
# 'dd-mai-2020' / 'dd-february-2018' / 'february-23rd-2017' (day before OR after
# month), all hyphen/space separated, ordinal suffix tolerated.
_DATE_DM_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?[-\s_]+(" + _MONTH_ALT + r")[-\s_]+(\d{4})",
    re.IGNORECASE,
)
_DATE_MD_RE = re.compile(
    r"(" + _MONTH_ALT + r")[-\s_]+(\d{1,2})(?:st|nd|rd|th)?[-\s_]+(\d{4})",
    re.IGNORECASE,
)

# Registration: Icelandic TF-XXX plus foreign marks seen on the site
# (G-BYLP, N610LC, OY-HIT, HB-ZOO, EI-FHD, YL-PSH, C-GWRJ, TC-JJJ, HA-LXG…).
_REG_RE = re.compile(
    r"\b(?:TF|OY|G|N|HB|EI|YL|C|TC|HA|LN|SE|D|F|OE|OK|PH)-?[A-Z0-9]{2,5}\b"
)
# TF- marks are the authoritative Icelandic registration; collect all of them.
_TF_RE = re.compile(r"\bTF-?[A-Z0-9]{3}\b", re.IGNORECASE)
# Icelandic airport ICAO codes start BI** (BIKF/BIRK/BIEG/BGNO is Greenland…).
_ICAO_RE = re.compile(r"\b(BI[A-Z]{2}|BG[A-Z]{2})\b", re.IGNORECASE)

_MEDIA_RE = re.compile(r"/media/(\d+)/([^/?\"]+?\.pdf)", re.IGNORECASE)
_ITEM_RE = re.compile(
    r'(?is)<div class="item">(.*?)(?=<div class="item">|</section>|<footer|\Z)'
)
_H3_RE = re.compile(r"(?is)<h3[^>]*>(.*?)</h3>")
_P_RE = re.compile(r"(?is)<p[^>]*>(.*?)</p>")
_PDF_HREF_RE = re.compile(r'href="([^"]*/media/\d+/[^"]+\.pdf)"', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Filename keywords → report kind (final vs interim/preliminary).
_FINAL_KW = ("lokaskyrsla", "lokaskýrsla", "final-report", "final_report",
             "final-report", "finalreport")
_INTERIM_KW = ("bradabirgdaskyrsla", "bráðabirgðaskýrsla",
               "interim-report", "interim_report", "preliminary")
# Obvious non-report forms / notifications to drop even if listed.
_FORM_KW = ("tilkynning", "tillkynning", "eydublad", "eyðublað",
            "ey-ublad", "form-", "_form", "skraningarform")


def _clean(fragment):
    text = _TAG_RE.sub(" ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def year_from_url(url):
    """The 4-digit year embedded in a per-year archive URL → str | None."""
    m = re.search(r"/slysa-og-atvikaskyrslur/(\d{4})/", url or "")
    return m.group(1) if m else None


def parse_filename_date(filename):
    """
    Best-effort ISO date from a filename's date tokens (Icelandic OR English
    month names, day-before OR day-after month). Returns 'YYYY-MM-DD' | None.
    """
    if not filename:
        return None
    name = filename
    m = _DATE_DM_RE.search(name)
    if m:
        day, mon, year = m.group(1), m.group(2).lower(), m.group(3)
        month = _MONTHS.get(mon)
        if month:
            return _iso(year, month, day)
    m = _DATE_MD_RE.search(name)
    if m:
        mon, day, year = m.group(1).lower(), m.group(2), m.group(3)
        month = _MONTHS.get(mon)
        if month:
            return _iso(year, month, day)
    return None


def _iso(year, month, day):
    try:
        d = int(day)
        if 1 <= d <= 31 and 1 <= month <= 12:
            return f"{int(year):04d}-{month:02d}-{d:02d}"
    except (TypeError, ValueError):
        pass
    return None


def extract_registrations(text):
    """
    All TF- registrations found in a filename / title / PDF text, uppercased &
    normalised to 'TF-XXX' (a hyphen is inserted when the source omits it).
    Returns a list in first-seen order, deduped. Empty when none.
    """
    out = []
    for raw in _TF_RE.findall(text or ""):
        norm = raw.upper().replace("TF", "TF-", 1) if "-" not in raw \
            else raw.upper()
        norm = re.sub(r"^TF-+", "TF-", norm)
        if norm not in out:
            out.append(norm)
    return out


def extract_registration(text):
    """Primary (first) TF- registration, or None."""
    regs = extract_registrations(text)
    return regs[0] if regs else None


def extract_icao(filename):
    """First Icelandic/Greenland airport ICAO (BIKF/BIEG/BGNO…) | None."""
    if not filename:
        return None
    m = _ICAO_RE.search(filename)
    return m.group(1).upper() if m else None


def report_kind(filename, title=None):
    """
    'Final' | 'Interim' | None from filename keywords (title as fallback).
    'lokaskyrsla' / 'final-report' → Final; 'bradabirgda' / 'interim' → Interim.
    """
    for src in (filename, title):
        low = (src or "").lower()
        if any(k in low for k in _INTERIM_KW):
            return "Interim"
        if any(k in low for k in _FINAL_KW):
            return "Final"
    return None


# Stopword sniff sets for language confirmation from the PDF text layer.
_IS_STOP = {"og", "að", "var", "þann", "flugvél", "flugmaður", "skýrslan",
            "loftfars", "vegna", "við", "ekki", "sem", "þar"}
_EN_STOP = {"the", "and", "was", "report", "aircraft", "pilot", "during",
            "flight", "runway", "investigation", "incident", "final"}


def detect_lang(filename, text=None, title=None):
    """
    Per-report language: 'is' | 'en'.

    Heuristic: filename keyword first (lokaskyrsla/bradabirgda → Icelandic;
    final-report/interim-report → English), then CONFIRM/override via a
    stopword sniff of the PDF text (or title) when available. Defaults to 'is'
    (the source's native language) when nothing is decisive — the downstream
    translate pipeline handles 'is'.
    """
    low = (filename or "").lower()
    guess = None
    if "lokaskyrsla" in low or "lokaskýrsla" in low or "bradabirgda" in low \
            or "bráðabirgða" in low:
        guess = "is"
    elif "final-report" in low or "final_report" in low \
            or "interim-report" in low or "interim_report" in low:
        guess = "en"

    sample = (text or "") if text else (title or "")
    if sample:
        words = set(re.findall(r"[a-zþæðöáéíóúýA-ZÞÆÐÖÁÉÍÓÚÝ]+",
                               sample.lower())[:400])
        is_hits = len(words & _IS_STOP)
        en_hits = len(words & _EN_STOP)
        if is_hits or en_hits:
            return "is" if is_hits >= en_hits else "en"
    return guess or "is"


def is_form_pdf(filename):
    """True for obvious notification/blank-form PDFs that are NOT reports."""
    low = (filename or "").lower()
    return any(k in low for k in _FORM_KW)


def parse_year_page(year_html, year_url):
    """
    Parse one per-year archive page → list of report dicts in document order.
    Each dict:
        case_id (numeric media id), slug, year, pdf_url (absolute), filename,
        title, summary, report_kind, registration, registrations (list),
        location (ICAO|None), event_date (ISO|None), lang.

    Only PDFs inside `<div class="item">` blocks are taken (the listing's own
    report links); obvious form/notification filenames are dropped.
    """
    year = year_from_url(year_url)
    out = []
    seen_ids = set()

    for body in _ITEM_RE.findall(year_html or ""):
        href_m = _PDF_HREF_RE.search(body)
        if not href_m:
            continue
        href = _html.unescape(href_m.group(1))
        media_m = _MEDIA_RE.search(href)
        if not media_m:
            continue
        media_id = media_m.group(1)
        filename = media_m.group(2)
        if is_form_pdf(filename):
            continue
        if media_id in seen_ids:
            continue
        seen_ids.add(media_id)

        h3 = _H3_RE.search(body)
        title = _clean(h3.group(1)) if h3 else None
        # Summary = first non-empty paragraph after the h3.
        summary = None
        for p in _P_RE.findall(body):
            txt = _clean(p)
            if txt:
                summary = txt
                break

        slug = filename[:-4] if filename.lower().endswith(".pdf") else filename
        meta_src = f"{slug} {title or ''}"
        registrations = extract_registrations(meta_src)
        out.append({
            "case_id": media_id,
            "slug": slug,
            "year": year,
            "pdf_url": urljoin(BASE, href),
            "filename": filename,
            "title": title,
            "summary": summary,
            "report_kind": report_kind(filename, title),
            "registration": registrations[0] if registrations else None,
            "registrations": registrations,
            "location": extract_icao(filename),
            "event_date": parse_filename_date(filename),
            "lang": detect_lang(filename, title=title),
        })

    return out


def fallback_event_date(year):
    """When no filename date is parseable, fall back to '{year}-01-01'."""
    if not year:
        return None
    return f"{year}-01-01"


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
