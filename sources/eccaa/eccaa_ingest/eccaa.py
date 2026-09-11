# eccaa_ingest/eccaa.py
"""ECCAA (Eastern Caribbean CAA) HTML scraper.

Source: https://www.eccaa.aero  (Joomla site).
The "AIG Reports" article (option=com_content&view=article&id=175&Itemid=90)
lists the Final Accident Reports as plain <a href="...">.pdf</a> links pointing
to /images/stories/docs/far/.

⚠️ RESIDENTIAL-VANTAGE-ONLY: the host returns 000 from datacenter/Mac IPs and is
reachable ONLY from a residential IP (the mini-PC). All live fetches must run on
the mini-PC.

⚠️ The PDF hrefs contain spaces and parentheses verbatim, e.g.
   .../far/Final Accident Report Cessna 402-C (J8-SXY) 5 Aug 2010.pdf
Those MUST be percent-encoded before the HTTP GET or curl/httpx returns 000.

Each report's metadata (aircraft type, registration, event date) is parsed out
of the filename. The country is derived PER-REPORT from the registration prefix
(see text.country_from_registration) because the six OECS member states share
ECCAA.
"""
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://www.eccaa.aero"
# The Joomla "AIG Reports" article that lists the Final Accident Reports.
INDEX_URL = (
    BASE + "/index.php?option=com_content&view=article&id=175&Itemid=90"
)
REFERER = INDEX_URL
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
}

# Only the Final Accident Reports under /far/. The leading "_" files
# (_PRESS RELEASE..., _Preliminary Report...) are NOT finals and are skipped.
_PDF_HREF_RE = re.compile(
    r'href="(https?://[^"]*?/images/stories/docs/far/[^"]+?\.pdf)"',
    re.IGNORECASE,
)

# Filename → metadata.
#   Final[ ]Accident Report <AIRCRAFT> (<REG>) <DD> <Mon> <YYYY>.pdf
# Aircraft = text between the "Report " prefix and the "(REG)" group.
_FNAME_RE = re.compile(
    r"^_*Final\s+Accident\s+Report\s+(?P<aircraft>.+?)\s*"
    r"\((?P<reg>[^)]+)\)\s*"
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\s+(?P<year>\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def make_client():
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


def encode_pdf_url(url: str) -> str:
    """Percent-encode an href's path/spaces so the GET does not 000.

    Splits off scheme+host, then quotes the path while preserving the structural
    delimiters that are already correct. Spaces, parentheses and other unsafe
    chars in the filename are escaped.
    """
    parts = urllib.parse.urlsplit(url)
    # safe set keeps the path structure; everything else (incl. space, parens)
    # gets percent-encoded.
    path = urllib.parse.quote(parts.path, safe="/-_.")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
    )


def _normalize_case_id(s: str) -> str:
    """Uppercase, collapse internal whitespace to single '-', strip edges."""
    s = (s or "").strip().upper()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def make_case_id(registration: str | None, date_iso: str | None,
                 filename: str | None = None) -> str:
    """Intrinsic case_id — NO encounter-order suffix.

    Prefer registration + event date (both intrinsic to the occurrence). Fall
    back to the report filename stem when neither is available.
    """
    reg = (registration or "").strip().upper()
    if reg and date_iso:
        return _normalize_case_id(f"{reg} {date_iso}")
    if reg:
        return _normalize_case_id(reg)
    if date_iso:
        return _normalize_case_id(f"ECCAA {date_iso}")
    stem = Path(filename or "").stem
    return _normalize_case_id(stem) or "ECCAA-UNKNOWN"


def _parse_date(day: str, mon: str, year: str) -> str | None:
    m = _MONTHS.get(mon[:3].lower())
    if not m:
        return None
    try:
        return f"{int(year):04d}-{m:02d}-{int(day):02d}"
    except ValueError:
        return None


def parse_listing(html: str) -> list[dict]:
    """Parse the AIG Reports article → list of final-report dicts.

    Each dict:
      case_id            intrinsic (reg + date)
      pdf_url            ABSOLUTE, percent-encoded, ready to GET
      pdf_url_en         same as pdf_url (source is English)
      aircraft           parsed from filename, or None
      registration       parsed from filename, or None
      date_of_occurrence ISO YYYY-MM-DD, or None
      country            ISO-2, derived per-report from registration
      event_class        'Accident'
      title              the raw filename (without extension)
      report_url         the listing page URL

    Rows whose filename starts with '_' (press releases / preliminary reports)
    are skipped — only Final Accident Reports are emitted. De-duplicates by
    case_id, preserving first-seen order.
    """
    from .text import country_from_registration

    seen: set[str] = set()
    rows: list[dict] = []

    for m in _PDF_HREF_RE.finditer(html):
        raw_href = _html.unescape(m.group(1))
        filename = raw_href.rsplit("/", 1)[-1]
        # urldecode any already-encoded filename so the regex sees real spaces
        fname_dec = urllib.parse.unquote(filename)

        # Skip non-final docs (press releases / preliminary), flagged by '_'.
        if fname_dec.lstrip().startswith("_"):
            continue

        fm = _FNAME_RE.match(fname_dec.strip())
        if fm:
            aircraft = fm.group("aircraft").strip() or None
            registration = fm.group("reg").strip().upper() or None
            date_iso = _parse_date(fm.group("day"), fm.group("mon"), fm.group("year"))
        else:
            # Unrecognised filename shape — still ingest, with null metadata.
            aircraft = None
            registration = None
            date_iso = None

        pdf_url = encode_pdf_url(raw_href)
        case_id = make_case_id(registration, date_iso, fname_dec)
        if case_id in seen:
            continue
        seen.add(case_id)

        country = country_from_registration(registration)
        title = Path(fname_dec).stem.strip()

        rows.append({
            "case_id": case_id,
            "report_url": INDEX_URL,
            "pdf_url": pdf_url,
            "pdf_url_es": None,
            "pdf_url_en": pdf_url,
            "aircraft": aircraft,
            "registration": registration,
            "date_of_occurrence": date_iso,
            "location": None,
            "country": country,
            "event_class": "Accident",
            "title": title,
        })

    return rows


def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET pdf_url (already percent-encoded) with Referer; write bytes to dest."""
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
