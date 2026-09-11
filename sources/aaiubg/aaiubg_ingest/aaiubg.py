# aaiubg_ingest/aaiubg.py
"""Bulgaria AAIU (Aircraft Accident Investigation Unit, Ministry of Transport)
HTML scraper: per-year listing discovery and English PDF download.

Source key: aaiubg  (⚠️ NOT aaiu = Ireland, NOT aaiube = Belgium).

Source: https://www.mtc.government.bg/en/category/193
- The index page (/en/category/193) lists per-year sub-category pages
  (/en/category/193/...-aviation-occurrences-YYYY) — one teaser row per year.
- Each per-year sub-category page is a single Drupal node--type-document whose
  body contains every report for that year as a block:
      <p><a href="…/EN_FR_…<reg>_<date>.pdf"><strong>TITLE</strong></a></p>
  followed by narrative paragraphs.
- PDFs are official English (often bilingual EN/FR or EN/BG) text-layer reports.
  They download with a Referer header.

⚠️ case_id is INTRINSIC and order-independent: registration + ISO event date
   parsed from the report title (e.g. 'LZ-PTS_2022-08-08'); when registration is
   absent ('without registration marks') it falls back to a normalised PDF
   filename token.  No encounter-order suffixes.
"""
import html as _html
import re
import datetime
from pathlib import Path

BASE = "https://www.mtc.government.bg"
INDEX_URL = BASE + "/en/category/193"
REFERER = INDEX_URL
DELAY = 2.0

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
# Constants
# ──────────────────────────────────────────────

_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# ──────────────────────────────────────────────
# Compiled regexes
# ──────────────────────────────────────────────

# Per-year sub-category links: /en/category/193/...occurrences-YYYY[-N]
_YEAR_LINK_RE = re.compile(
    r'href=["\'](/en/category/193/[^"\']*occurrences[^"\']*)["\']',
    re.IGNORECASE,
)

# The Drupal node body of a per-year document page (from node--type-document
# article up to the footer / footer region).
_NODE_BODY_RE = re.compile(
    r"(node--type-document.*?)(?:<footer|region region-footer)",
    re.IGNORECASE | re.DOTALL,
)

# A report row: <a href="….pdf">…TITLE…</a>
_PDF_ANCHOR_RE = re.compile(
    r'<a [^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Date forms in titles:
#   DD.MM.YYYY        e.g. 08.08.2022
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
#   D Month YYYY      e.g. 12 August 2018
_DATE_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+("
    + "|".join(_MONTHS_EN)
    + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
#   Month D, YYYY     e.g. November 17, 2022
_DATE_MDY_RE = re.compile(
    r"\b(" + "|".join(_MONTHS_EN) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
    re.IGNORECASE,
)

# Registration: "registration [marks|nr.|number] LZ-XXX" (LZ + foreign marks)
_REG_RE = re.compile(
    r"registration(?:\s+marks|\s+nr\.?|\s+number)?\s+([A-Z]{1,2}-?[A-Z0-9]{2,6})",
    re.IGNORECASE,
)
_NO_REG_RE = re.compile(r"without\s+registration", re.IGNORECASE)

# Operator: "operated by X" / "used by X"
_OP_RE = re.compile(
    r"(?:operated|used)\s+by\s+(?:of\s+)?(.+?)(?=,|\.|\s+and\s+|\s+on\s+\d|\s+during|\s+after|\s+upon|$)",
    re.IGNORECASE,
)

# Aircraft type heuristics (best-effort; metadata-light is acceptable):
#   "involving (the) <type> aircraft" / "with <type> aircraft" /
#   "by (a) <type> aircraft" / "with helicopter <type>"
_AIRCRAFT_RE = re.compile(
    r"(?:involving|with|by)\s+(?:the\s+|a\s+|an\s+)?"
    r"(?:helicopter\s+)?([A-Z0-9][A-Za-z0-9 \-./()]{1,40}?)\s+"
    r"(?:aircraft|airplane|helicopter|airplane,)",
    re.IGNORECASE,
)

_NONSLUG = re.compile(r"[^a-z0-9]+")


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    """Return an httpx.Client configured with browser UA + Referer."""
    import httpx
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30.0,
    )


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_year_urls(index_html: str) -> list[str]:
    """
    Parse the AAIU index page → list of absolute per-year listing URLs.

    Extracts every /en/category/193/...occurrences-YYYY sub-category link.
    Preserves order, de-duplicates.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for m in _YEAR_LINK_RE.finditer(index_html):
        path = _html.unescape(m.group(1))
        url = BASE + path
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ──────────────────────────────────────────────
# Title-field parsing helpers
# ──────────────────────────────────────────────

def _clean_text(s: str) -> str:
    s = _TAG_RE.sub(" ", s)
    s = _html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def _parse_date(title: str) -> str | None:
    """Parse the first event date in a title → ISO 'YYYY-MM-DD', or None.

    Accepts DD.MM.YYYY, 'D Month YYYY', and 'Month D, YYYY'.
    """
    m = _DATE_NUM_RE.search(title)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    m = _DATE_DMY_RE.search(title)
    if m:
        d = int(m.group(1))
        mo = _MONTHS_EN[m.group(2).lower()]
        y = int(m.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    m = _DATE_MDY_RE.search(title)
    if m:
        mo = _MONTHS_EN[m.group(1).lower()]
        d = int(m.group(2))
        y = int(m.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    return None


def _parse_registration(title: str) -> str | None:
    """First registration mark in title, or None when 'without registration'."""
    if _NO_REG_RE.search(title):
        return None
    m = _REG_RE.search(title)
    return m.group(1).upper() if m else None


def _parse_event_class(title: str) -> str:
    tl = title.lower()
    if "serious incident" in tl or "airprox" in tl:
        return "Serious incident"
    if "accident" in tl:
        return "Accident"
    if "incident" in tl:
        return "Incident"
    return "Aviation event"


def _parse_operator(title: str) -> str | None:
    m = _OP_RE.search(title)
    if not m:
        return None
    op = m.group(1).strip().strip("„“\"' ")
    return op or None


def _parse_aircraft(title: str) -> str | None:
    m = _AIRCRAFT_RE.search(title)
    if not m:
        return None
    ac = m.group(1).strip().strip(",")
    # discard obvious noise captures
    if len(ac) < 2 or ac.lower() in ("the", "a", "an"):
        return None
    return ac


def _slug(s: str) -> str:
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_case_id(registration: str | None, date_iso: str | None,
                 pdf_filename: str) -> str:
    """
    Build an INTRINSIC, order-independent case_id.

    Preference: '<REG>_<YYYY-MM-DD>' when both are known.  Otherwise fall back
    to a normalised PDF filename token (still intrinsic — the URL is stable —
    and never depends on encounter order).
    """
    if registration and date_iso:
        return f"{registration}_{date_iso}"
    base = re.sub(r"\.pdf$", "", pdf_filename, flags=re.IGNORECASE)
    base = re.sub(r"^(en|fr|bg|final|report|draft)[_-]+", "", base, flags=re.IGNORECASE)
    return "bg-" + _slug(base)[:60]


def _normalize_case_id(case_id: str) -> str:
    """Canonical form: registration uppercased, whitespace stripped.

    Idempotent.  'lz-pts_2022-08-08' → 'LZ-PTS_2022-08-08'; filename-fallback
    ids are lowercased to their slug form.
    """
    cid = (case_id or "").strip()
    if not cid:
        return cid
    if cid.startswith("bg-"):
        return "bg-" + _slug(cid[3:])
    # registration_date form
    if "_" in cid:
        reg, _, rest = cid.partition("_")
        return f"{reg.upper()}_{rest}"
    return cid.upper()


# ──────────────────────────────────────────────
# Main listing parser
# ──────────────────────────────────────────────

def parse_listing(html: str, year_url: str = "") -> list[dict]:
    """
    Parse a per-year AAIU document page → list of report dicts.

    Each dict has:
      case_id            str   intrinsic (e.g. 'LZ-PTS_2022-08-08')
      pdf_url            str   absolute English report PDF URL
      pdf_filename       str
      event_class        str   Accident | Serious incident | Incident | Aviation event
      aircraft           str|None
      registration       str|None
      date_of_occurrence str|None  ISO YYYY-MM-DD
      operator           str|None
      title              str   full report title text

    De-duplicates within a page on pdf_url.  Rows whose title yields neither a
    date nor a registration still get a filename-derived case_id and are kept.
    """
    body_m = _NODE_BODY_RE.search(html)
    body = body_m.group(1) if body_m else html

    rows: list[dict] = []
    seen_urls: set[str] = set()
    for m in _PDF_ANCHOR_RE.finditer(body):
        pdf_path = _html.unescape(m.group(1)).strip()
        if pdf_path in seen_urls:
            continue
        seen_urls.add(pdf_path)

        title = _clean_text(m.group(2))
        if not title:
            continue

        pdf_url = pdf_path if pdf_path.startswith("http") else BASE + pdf_path
        pdf_filename = pdf_path.rsplit("/", 1)[-1]

        date_iso = _parse_date(title)
        registration = _parse_registration(title)
        case_id = _normalize_case_id(make_case_id(registration, date_iso, pdf_filename))

        rows.append({
            "case_id": case_id,
            "pdf_url": pdf_url,
            "pdf_filename": pdf_filename,
            "event_class": _parse_event_class(title),
            "aircraft": _parse_aircraft(title),
            "registration": registration,
            "date_of_occurrence": date_iso,
            "operator": _parse_operator(title),
            "title": title,
        })

    return rows


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def download(client, pdf_url: str, dest: str | Path) -> None:
    """
    GET pdf_url with a Referer header and write bytes to dest.

    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
