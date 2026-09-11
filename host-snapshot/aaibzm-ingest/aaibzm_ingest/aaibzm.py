# aaibzm_ingest/aaibzm.py
"""AAIB Zambia (Aircraft Accident Investigation Board) HTML scraper.

Source: https://aaib.org.zm/
- ONE static, server-rendered HTML page (English, ~33KB) listing every
  published accident / serious-incident report as a Bootstrap "list-group"
  card.  Each card is an <a href="pages/<REG>.php"> anchor whose <h6> title
  carries the event class, aircraft type, registration, location and date,
  e.g.:

      Accident involving a Piper PA-32-300 Cherokee Six Aircraft
      Registration: 9J-RDN, 652m from Mulobezi airstrip runway 03 in
      Western Province on 27th February 2022

- 7 reports span 2019-2022; the site is "stably stale".
- The actual PDF lives at  https://aaib.org.zm/reports/<REG>.pdf  (the
  registration is the intrinsic key).  6 of 7 follow that pattern exactly;
  one (9J-RHE) is published as reports/9J-RHE-2019.pdf, so download() resolves
  the authoritative reports/*.pdf link from the detail page when the derived
  URL is unavailable.
- PDFs are text-layer English reports (~18K-… chars, source_tier "pdf").
  Unlike Guyana, the PDF body is free prose with NO labelled cover block, so
  the CARD is the authoritative source for aircraft / date / location; the
  PDF body supplies narrative_text (and a best-effort extractor fallback).

case_id model
-------------
The registration mark (e.g. 9J-YVT) is the one INTRINSIC, order-independent
identifier present on every card, so:

    case_id = make_case_id(reg)             # 'aaibzm-<reg-slug>'

e.g. 'aaibzm-9j-yvt'.  No encounter-order suffix is ever appended; the same
registration always yields the same case_id.  The registration is also stored
verbatim in the registration column.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://aaib.org.zm"
INDEX_URL = BASE + "/"
REFERER = INDEX_URL
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

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Listing cards: <a href="pages/<REG>.php" class="list-group-item ..."> ... </a>
# The href registration is the intrinsic key; the inner <h6 class="...fw-medium">
# title supplies aircraft / class / location / date.
_CARD_RE = re.compile(
    r'<a\b[^>]*\bhref="pages/(9[A-Z]-[A-Z]{2,4})\.php"[^>]*'
    r'class="[^"]*list-group-item[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

# The card title heading (<h6 class="mb-1 fw-medium"> ... </h6>).
_CARD_TITLE_RE = re.compile(
    r'<h6\b[^>]*\bclass="[^"]*fw-medium[^"]*"[^>]*>(.*?)</h6>',
    re.IGNORECASE | re.DOTALL,
)

# Inner tags (icons <i>…</i>, <sup>th</sup> ordinals) stripped from text.
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NONSLUG_RE = re.compile(r"[^a-z0-9]+")

# Zambian (9J-XXX) / foreign-on-register (9S-XXX) registration mark.  Tolerates
# the en-dash / spaced form '9J – YVT' that appears in PDF bodies.
_REG_RE = re.compile(r"\b(9[A-Z]\s*[-–]\s*[A-Z]{2,4})\b")

# Event class prefix on the card title.
_CLASS_RE = re.compile(r"\b(Serious\s+Incident|Accident|Incident)\b", re.IGNORECASE)


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA and cookie jar."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
    )


# ──────────────────────────────────────────────
# slug / case_id helpers
# ──────────────────────────────────────────────

def _slugify(s: str) -> str:
    if not s:
        return ""
    return _NONSLUG_RE.sub("-", s.lower()).strip("-")


def _canon_reg(raw: str) -> str:
    """Canonicalise a registration token to '9J-YVT' (uppercase, single dash)."""
    raw = _html.unescape(raw or "")
    raw = re.sub(r"\s+", "", raw)              # '9J – YVT' -> '9J–YVT'
    raw = raw.replace("–", "-").replace("—", "-")
    return raw.upper()


def make_case_id(reg: str) -> str:
    """
    Build the INTRINSIC, stable case_id from an aircraft registration.

    The registration is the only identifier present for every listing card, so
    it is the primary key.  e.g.
        '9J-YVT'   -> 'aaibzm-9j-yvt'
        '9j – yvt' -> 'aaibzm-9j-yvt'

    No encounter-order suffix is ever appended; the same registration always
    yields the same case_id.
    """
    slug = _slugify(_canon_reg(reg))
    return f"aaibzm-{slug}" if slug else "aaibzm"


def pdf_url_for(reg: str) -> str:
    """Derive the canonical PDF URL 'reports/<REG>.pdf' for a registration."""
    return f"{BASE}/reports/{_canon_reg(reg)}.pdf"


def detail_url_for(reg: str) -> str:
    """Derive the detail-page URL 'pages/<REG>.php' for a registration."""
    return f"{BASE}/pages/{_canon_reg(reg)}.php"


def find_registration(text: str) -> str | None:
    """Return the first aircraft registration mark found in text, or None."""
    if not text:
        return None
    m = _REG_RE.search(text)
    if not m:
        return None
    return _canon_reg(m.group(1))


# ── card-title field extraction ────────────────────────────────────
#
# Every card title follows the shape:
#   "<Class> involving [a] <Aircraft> [Aircraft] Registration: <REG>,
#    <location words> on <day><ord> <Month> <Year>"
# The fields are extracted from the cleaned (HTML-stripped) title.

def extract_event_class(title: str | None) -> str | None:
    """Event class ('Accident' / 'Serious Incident') from a card title."""
    if not title:
        return None
    m = _CLASS_RE.search(title)
    if not m:
        return None
    return " ".join(w.capitalize() for w in m.group(1).split())


def extract_aircraft_from_title(title: str | None) -> str | None:
    """
    Aircraft make/model from a card title: the words between
    '<Class> involving [a/an]' and 'Registration:' (the word 'Aircraft'
    immediately preceding 'Registration' is dropped).
    """
    if not title:
        return None
    m = re.search(
        r"(?:Serious\s+Incident|Accident|Incident)\s+involving\s+(?:an?\s+)?"
        r"(.*?)\s*,?\s*(?:Aircraft\s+)?Registration\s*:",
        title,
        re.IGNORECASE,
    )
    if not m:
        return None
    val = _WS_RE.sub(" ", m.group(1)).strip(" ,")
    return val or None


def extract_location_from_title(title: str | None) -> str | None:
    """
    Event location from a card title: the words between the registration and
    the trailing ' on <date>' clause.
    """
    if not title:
        return None
    m = re.search(
        # Loosen the reg-mark separator to '[-–\\s]*' (zero-or-more) so the
        # spaced en-dash form '9J – YVT,' matches as well as the plain
        # '9J-YVT,' form, consistent with _REG_RE / _canon_reg.
        r"Registration\s*:\s*9[A-Z][-–\s]*[A-Z]{2,4}\s*,?\s*(.*?)\s+on\s+"
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}",
        title,
        re.IGNORECASE,
    )
    if not m:
        return None
    val = _WS_RE.sub(" ", m.group(1)).strip(" ,")
    return val or None


# ── PDF cover-block field extraction (best-effort fallback) ─────────
#
# The Zambian PDF body has NO uniform labelled cover block (unlike Guyana), so
# these labelled extractors rarely fire; they are kept as a best-effort
# fallback and to mirror the template's extractor surface.

_HEAD_CHARS = 2500

# English month names -> month number (case-insensitive).
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    # common abbreviations
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Word-month date with ordinal-suffix tolerance and optional comma before year:
#   "27th February 2022", "10th JANUARY, 2020", "7th September 2021".
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\.?\s+"      # day (+ optional ordinal)
    r"([A-Za-z]+)\.?\s*,?\s+"                    # month word (+ optional comma)
    r"(\d{4})\b",                               # year
    re.IGNORECASE,
)

# "Date of Accident", "Date of Occurrence" (slash/space tolerant) label.
_DATE_LABEL_RE = re.compile(
    r"Date\s+of\s+(?:Accident|Occurrence|Incident|Event)",
    re.IGNORECASE,
)


def _iso(day: int, month: int, year: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31 and 1900 <= year <= 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _word_date_at(text: str) -> str | None:
    """Return the first word-month date in text as ISO, or None."""
    for m in _WORD_DATE_RE.finditer(text):
        month = _MONTHS.get(m.group(2).lower())
        if month is None:
            continue
        iso = _iso(int(m.group(1)), month, int(m.group(3)))
        if iso:
            return iso
    return None


def extract_event_date(text: str | None) -> str | None:
    """
    Extract the ISO 'YYYY-MM-DD' event date from a card title or report text.

    Strategy: prefer the first word-month date that follows a "Date of
    Accident"/"Date of Occurrence" label (PDF cover style), otherwise fall back
    to the first word-month date in the head.  Tolerates ordinal suffixes
    (1st/2nd/3rd/27th), an optional comma before the year and case-insensitive
    month names.  Returns None when no parseable date is present.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]

    label = _DATE_LABEL_RE.search(head)
    if label:
        window = head[label.end():label.end() + 120]
        iso = _word_date_at(window)
        if iso:
            return iso

    return _word_date_at(head)


def _extract_labelled(text: str, label_re: re.Pattern) -> str | None:
    """
    Return the value following a cover-block label (best-effort PDF fallback).

    The value may sit on the same line after a '-'/':' separator, or on the
    following non-blank line.  None when the label is absent or empty.
    """
    if not text:
        return None
    head = text[:_HEAD_CHARS]
    m = label_re.search(head)
    if not m:
        return None
    rest = head[m.end():]
    vm = re.match(
        r"\s*[-:–—]?\s*(?:\n\s*)*([^\n]+)",
        rest,
    )
    if not vm:
        return None
    val = vm.group(1)
    val = re.sub(r"^[\s\-:–—]+", "", val)
    val = _WS_RE.sub(" ", val).strip(" -:–—\t")
    return val or None


_AIRCRAFT_LABEL_RE = re.compile(
    r"Aircraft\s+(?:Model|Make)\s*/?\s*(?:Type)?",
    re.IGNORECASE,
)
_LOCATION_LABEL_RE = re.compile(
    r"Place\s+of\s+(?:Accident|Occurrence|Incident)\s*/?\s*(?:Region)?",
    re.IGNORECASE,
)
_OPERATOR_LABEL_RE = re.compile(
    r"(?:Registered\s+Owner\s*/?\s*Operator|Name\s+of\s+Operator|Operator)",
    re.IGNORECASE,
)


def extract_aircraft(text: str | None) -> str | None:
    """Aircraft make/model from a PDF cover block (best-effort fallback)."""
    return _extract_labelled(text or "", _AIRCRAFT_LABEL_RE)


def extract_location(text: str | None) -> str | None:
    """Event location from a PDF cover block (best-effort fallback)."""
    return _extract_labelled(text or "", _LOCATION_LABEL_RE)


def extract_operator(text: str | None) -> str | None:
    """Operator from a PDF cover block (best-effort fallback)."""
    return _extract_labelled(text or "", _OPERATOR_LABEL_RE)


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def _clean_title(raw: str) -> str:
    """Strip inner tags (<i>, <sup>th</sup>) and collapse whitespace."""
    return _WS_RE.sub(" ", _html.unescape(_TAG_RE.sub("", raw))).strip()


def parse_listing(index_html: str) -> list[dict]:
    """
    Parse the AAIB Zambia index page → list of report dicts.

    Each dict has:
      case_id      str   intrinsic 'aaibzm-<reg-slug>' from the registration
      registration str   canonical registration ('9J-YVT')
      pdf_url      str   derived absolute 'reports/<REG>.pdf' URL
      report_url   str   detail-page 'pages/<REG>.php' URL (authoritative
                         source for the real PDF link if the derived 404s)
      title        str   cleaned card heading text
      event_class  str   'Accident' / 'Serious Incident' (or None)
      aircraft     str   aircraft make/model from the card title (or None)
      location     str   event location from the card title (or None)
      event_date   str   ISO date from the card title (or None)

    Order preserved; de-duplicated by case_id.  Only the list-group cards are
    matched, so the nav-dropdown duplicates and the about/contact/downloads
    pages are excluded.
    """
    seen: set[str] = set()
    rows: list[dict] = []
    for m in _CARD_RE.finditer(index_html):
        reg = _canon_reg(m.group(1))
        case_id = make_case_id(reg)
        if case_id in seen:
            continue
        seen.add(case_id)

        tm = _CARD_TITLE_RE.search(m.group(2))
        title = _clean_title(tm.group(1)) if tm else _clean_title(m.group(2))

        rows.append({
            "case_id": case_id,
            "registration": reg,
            "pdf_url": pdf_url_for(reg),
            "report_url": detail_url_for(reg),
            "title": title,
            "event_class": extract_event_class(title),
            "aircraft": extract_aircraft_from_title(title),
            "location": extract_location_from_title(title),
            "event_date": extract_event_date(title),
        })
    return rows


def resolve_pdf_url(client, detail_url: str) -> str | None:
    """
    Fetch a 'pages/<REG>.php' detail page and return the absolute URL of its
    first 'reports/*.pdf' link, or None when no such link is present.
    """
    resp = client.get(detail_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    body = resp.content.decode("utf-8", "replace") if isinstance(resp.content, bytes) else resp.text
    # Detail pages link the PDF relative to /pages/, e.g.
    #   href="../reports/9J-RHE-2019.pdf"
    m = re.search(
        r'href=["\'](?:\.\./|/)?(reports/[^"\']+?\.pdf)["\']',
        body, re.IGNORECASE,
    )
    if not m:
        return None
    href = _html.unescape(m.group(1).strip())
    return f"{BASE}/{href}"


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def _encode(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(parts.path, safe="/%")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment)
    )


def download(client, pdf_url: str, dest: str | Path, detail_url: str | None = None) -> str:
    """
    GET pdf_url with Referer header and write bytes to dest.

    The path component is percent-encoded so any spaces resolve correctly while
    the listing href is stored verbatim.  When the derived 'reports/<REG>.pdf'
    URL is unavailable (HTTP error) and a detail_url is supplied, the
    authoritative reports/*.pdf link is resolved from the detail page and
    retried (covers 9J-RHE → reports/9J-RHE-2019.pdf).  Raises
    httpx.HTTPStatusError when neither URL resolves.

    Returns the URL the bytes were actually fetched from (the derived URL, or
    the resolved fallback when the derived one 404'd).
    """
    import httpx
    used = pdf_url
    try:
        resp = client.get(_encode(pdf_url), headers={"Referer": REFERER})
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        if not detail_url:
            raise
        real = resolve_pdf_url(client, detail_url)
        if not real or real == pdf_url:
            raise
        resp = client.get(_encode(real), headers={"Referer": REFERER})
        resp.raise_for_status()
        used = real

    with open(dest, "wb") as fh:
        fh.write(resp.content)
    return used
