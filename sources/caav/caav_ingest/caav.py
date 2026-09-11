# caav_ingest/caav.py
"""CAAV (Civil Aviation Authority of Vietnam) HTML scraper.

Source: https://english.caa.gov.vn/doc/investigation-reports.htm  (ENGLISH)
- The investigation-reports listing is a server-rendered <table>; each <tr>
  has an <a href="/doc-detail/<slug>-<id>.htm"> plus two dd.mm.yyyy date cells
  (publication / signing dates).
- ⚠️ Only listing page 1 is server-rendered.  The pager links to
  /doc/investigation-reports/trang-N.htm but those routes return an empty JS
  shell (no listing, no loader) for a non-JS client, so pages 2-4 are NOT
  reachable with httpx.  See SMOKE.md / module note in iter_listing_urls().
- Each detail page links its report PDF on the third-party CDN
  imgcaa.minhvujsc.com, via <a href>, <iframe src> or <embed src>.  Some PDF
  filenames contain spaces — URLs are %-encoded before download.
- case_id is INTRINSIC and order-independent: VN-<reg>-<event_date> when both a
  Vietnamese-style registration and an event date can be derived from the
  report title; otherwise a short sha1 of the pdf_url.  No encounter-order
  suffixes (recurring program trap: order-dependent keys swap between cases).
"""
import datetime
import hashlib
import html as _html
import re
import urllib.parse
from pathlib import Path

BASE = "https://english.caa.gov.vn"
INDEX_URL = BASE + "/doc/investigation-reports.htm"
REFERER = INDEX_URL
CDN_REFERER = BASE + "/"
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
# Compiled regexes
# ──────────────────────────────────────────────

# Listing table rows
_TR_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_DETAIL_HREF_RE = re.compile(r"href=[\"'](/doc-detail/[^\"']+\.htm)[\"']", re.IGNORECASE)
_ANCHOR_RE = re.compile(
    r"<a\s+[^>]*href=[\"'](/doc-detail/[^\"']+\.htm)[\"'][^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)
_DATE_CELL_RE = re.compile(r"<td[^>]*>\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*</td>")

# Detail page: report title (og:title preferred, else <title>)
_OG_TITLE_RE = re.compile(
    r"property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)[\"']", re.IGNORECASE
)
_OG_TITLE_REV_RE = re.compile(
    r"content=[\"']([^\"']+)[\"'][^>]*property=[\"']og:title[\"']", re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)

# Detail page: report PDF on the imgcaa CDN, from href / iframe src / embed src.
# Allow spaces and parentheses in the path (some filenames contain them).
_CDN_PDF_RE = re.compile(
    r"""(?:href|src)=[\"'](https?://imgcaa\.minhvujsc\.com/[^\"']+?\.pdf)[\"']""",
    re.IGNORECASE,
)

# Title metadata
# Vietnamese aircraft registration shape.  Two forms the source uses:
#   * LETTER-mark form  VN-A870, VN-B218, VN-A653, VN-A639  (a LETTER after the
#     dash, then 3-4 digits).  The leading dash is optional in source text.
#   * NUMERIC-mark form VN-8650  (digits only) — but this MUST have the dash,
#     otherwise it is indistinguishable from a flight number like VN125.
# Flight numbers (VN125, VJ260, HVN1266 …) are deliberately EXCLUDED:
#   - letter form requires a letter immediately after VN/VN-
#   - numeric form requires the dash (VN-#### not VN####)
_REG_LETTER_RE = re.compile(r"\bVN-?([A-Z]\d{3,4})\b", re.IGNORECASE)
_REG_NUM_RE = re.compile(r"\bVN-(\d{3,4})\b", re.IGNORECASE)
# Context cues that strongly mark the following/enclosed token as a registration.
_REG_CONTEXT_RE = re.compile(
    r"(?:REGISTRATION|REGISTERED|AIRCRAFT|REG\.?|\()\s*"
    r"(VN-?[A-Z]\d{3,4}|VN-\d{3,4})\b",
    re.IGNORECASE,
)
_DATE_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_DATE_WORD_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?,?\s+(\d{4})\b",
    re.IGNORECASE,
)
_AIRCRAFT_RE = re.compile(
    r"\b(?:HELICOPTER\s+)?"
    r"(AIRBUS\s+A\d{3}|BOEING\s+\d{3}|BELL\s+\d{3}|ATR\s*\d{2,3}|"
    r"EMBRAER\s+[A-Z0-9]+|CESSNA\s+\d+)\b",
    re.IGNORECASE,
)

# A detail page is a genuine investigation report when its title matches this
# report-type pattern (FINAL/INTERIM REPORT|STATEMENT, FINAL INVESTIGATION
# REPORT, ACCIDENT / SERIOUS INCIDENT).  Advisory circulars (AC NN-NNN),
# decisions (QĐ), forms etc. are excluded.
_REPORT_TYPE_RE = re.compile(
    r"\b(?:"
    r"FINAL\s+REPORT|INTERIM\s+REPORT|INTERIM\s+STATEMENT|FINAL\s+STATEMENT|"
    r"\d+(?:ST|ND|RD|TH)\s+INTERIM\s+REPORT|"
    r"FINAL\s+INVESTIGATION\s+REPORT|"
    r"SERIOUS\s+INCIDENT|ACCIDENT"
    r")\b",
    re.IGNORECASE,
)

# Detail-page URL id suffix: /doc-detail/<slug>-<id>.htm (slug ignored).
_DETAIL_ID_RE = re.compile(r"-(\d+)\.htm$", re.IGNORECASE)

# Bounded ID-enumeration window, derived from the page-1 seed ids.
ID_WINDOW_BACK = 40    # ids below the max seed to (re)scan for the report cluster
ID_WINDOW_FWD = 40     # ids above the max seed to scan for NEW reports
ID_FWD_EMPTY_STOP = 8  # stop the forward scan after this many consecutive empty
                       # shells (no CDN PDF) — new ids appear contiguously

_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


# ──────────────────────────────────────────────
# Client factory
# ──────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0)


# ──────────────────────────────────────────────
# case_id helpers
# ──────────────────────────────────────────────

def _normalize_case_id(case_id: str) -> str:
    """Upper-case and collapse to a stable [A-Z0-9-] token."""
    if not case_id:
        return ""
    cid = case_id.strip().upper()
    cid = re.sub(r"[^A-Z0-9]+", "-", cid).strip("-")
    return cid


def make_case_id(registration: str | None, event_date: str | None,
                 pdf_url: str | None) -> str:
    """Derive an INTRINSIC, order-independent case_id.

    Preference order:
      1. VN-<registration>-<event_date>  when both are present
      2. sha1(pdf_url)[:12]              fallback (still intrinsic — no order)
    """
    reg = (registration or "").strip()
    date = (event_date or "").strip()
    if reg and date:
        # strip a leading VN / VN- so the VN- country prefix is not doubled
        reg = re.sub(r"^VN-?", "", reg, flags=re.IGNORECASE)
        return _normalize_case_id(f"VN-{reg}-{date}")
    if pdf_url:
        h = hashlib.sha1(pdf_url.encode("utf-8")).hexdigest()[:12]
        return _normalize_case_id(f"VN-{h}")
    return ""


# ──────────────────────────────────────────────
# Discovery
# ──────────────────────────────────────────────

def iter_listing_urls(index_html: str = "") -> list[str]:
    """Return the listing page URLs to crawl.

    ⚠️ Only page 1 is server-rendered.  The on-page pager links
    (/doc/investigation-reports/trang-N.htm) return an empty JS shell with no
    report rows and no data-loading JS, so they cannot be fetched with httpx.
    We therefore crawl page 1 only.  index_html is accepted for API parity.
    """
    return [INDEX_URL]


def parse_listing(html: str, listing_url: str = "") -> list[dict]:
    """Parse a listing page → list of {detail_url, listing_title, pub_date}.

    Report rows are <tr> blocks that contain BOTH a /doc-detail/ anchor and at
    least one dd.mm.yyyy date cell (this distinguishes real report rows from
    navigation menu anchors).  Order-preserving, de-duplicated by detail_url.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for tr_m in _TR_RE.finditer(html):
        tr = tr_m.group(1)
        a_m = _ANCHOR_RE.search(tr)
        if not a_m:
            continue
        dates = _DATE_CELL_RE.findall(tr)
        if not dates:
            continue  # nav anchor, not a report row
        detail_url = BASE + _html.unescape(a_m.group(1))
        if detail_url in seen:
            continue
        seen.add(detail_url)
        title = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", a_m.group(2)))).strip()
        rows.append({
            "detail_url": detail_url,
            "listing_title": title,
            "pub_date": _dotted_date_to_iso(dates[0]),
        })
    return rows


def detail_id_from_url(detail_url: str) -> int | None:
    """Return the numeric id from a /doc-detail/<slug>-<id>.htm url, else None."""
    if not detail_url:
        return None
    m = _DETAIL_ID_RE.search(detail_url)
    return int(m.group(1)) if m else None


def detail_url_for_id(doc_id: int) -> str:
    """Build a detail-page URL for a numeric id (slug is ignored by the site)."""
    return f"{BASE}/doc-detail/r-{doc_id}.htm"


def id_window_from_seeds(seed_ids) -> list[int]:
    """Derive a BOUNDED ID-enumeration window from the page-1 seed ids.

    The window is anchored on the DENSE report cluster (the max seed id), not the
    absolute min, so a stale outlier id (e.g. a years-old report still linked on
    page 1) does not blow the window up to thousands of ids.  Returns the
    inclusive range [anchor - ID_WINDOW_BACK, anchor + ID_WINDOW_FWD].  Forward
    ids are also scanned so NEW reports with higher ids get picked up over time
    (the discover() forward scan additionally stops on a run of empty shells).
    """
    ids = sorted({int(i) for i in seed_ids if i is not None})
    if not ids:
        return []
    anchor = ids[-1]
    lo = anchor - ID_WINDOW_BACK
    hi = anchor + ID_WINDOW_FWD
    return list(range(lo, hi + 1))


def is_report_detail(meta: dict) -> bool:
    """True when a parsed detail page is a genuine investigation report.

    Keep a row when a CDN PDF is present AND either the title matches the
    report-type pattern OR the title is empty (titleless-but-valid accident PDFs
    such as ids 30251/30252 must not be dropped).  No PDF → not a report (this
    also rejects the ~10 KB empty-shell sentinel returned for unallocated ids).
    """
    if not meta.get("pdf_url"):
        return False
    title = (meta.get("title") or "").strip()
    if not title:
        return True
    return bool(_REPORT_TYPE_RE.search(title))


def event_class_from_title(title: str) -> str:
    """Derive event_class from a report title (accident vs serious incident)."""
    t = (title or "").upper()
    # "SERIOUS INCIDENT" must be checked before bare "INCIDENT"/"ACCIDENT" since
    # some titles contain both words.
    if "SERIOUS INCIDENT" in t:
        return "Serious incident"
    if "ACCIDENT" in t:
        return "Accident"
    return "Serious incident"


def _dotted_date_to_iso(s: str) -> str | None:
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", (s or "").strip())
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


# ──────────────────────────────────────────────
# Detail page parsing
# ──────────────────────────────────────────────

def _best_title(detail_html: str) -> str:
    for rx in (_OG_TITLE_RE, _OG_TITLE_REV_RE, _TITLE_RE):
        m = rx.search(detail_html)
        if m:
            t = _html.unescape(m.group(1))
            t = re.sub(r"\s+", " ", t).split("|")[0].strip()
            if t:
                return t
    return ""


def _norm_reg(token: str) -> str:
    """Normalise a matched registration token to canonical VN-<mark> form."""
    t = token.upper().replace(" ", "")
    t = re.sub(r"^VN-?", "", t)
    return f"VN-{t}"


def _extract_registration(title: str) -> str | None:
    """Extract a Vietnamese aircraft registration from a report title.

    Strategy (in priority order):
      1. A reg that appears in a registration context
         (after REGISTRATION/AIRCRAFT/REG or inside parens) — preferred so a
         flight number sitting earlier (e.g. "FLIGHT NUMBER VN125 AIRCRAFT
         VN-A870") never wins.
      2. Otherwise the first LETTER-mark reg (VN-A###) anywhere in the title.
      3. Otherwise the first NUMERIC-mark reg (VN-#### with a dash).
    Flight numbers (VN125, VJ260, HVN1266…) are excluded by construction.
    Returns None when no clean registration is present (caller falls back to the
    sha1(pdf_url) case_id rather than emitting garbage).
    """
    if not title:
        return None
    ctx = _REG_CONTEXT_RE.search(title)
    if ctx:
        return _norm_reg(ctx.group(1))
    lm = _REG_LETTER_RE.search(title)
    if lm:
        return _norm_reg(lm.group(1))
    nm = _REG_NUM_RE.search(title)
    if nm:
        return _norm_reg(nm.group(1))
    return None


def _parse_title_meta(title: str) -> dict:
    """Extract registration, event_date (ISO), aircraft from a report title."""
    out = {"registration": None, "date_iso": None, "aircraft": None}
    if not title:
        return out

    out["registration"] = _extract_registration(title)

    dm = _DATE_SLASH_RE.search(title)
    if dm:
        d, mo, y = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        try:
            out["date_iso"] = datetime.date(y, mo, d).isoformat()
        except ValueError:
            pass
    if out["date_iso"] is None:
        wm = _DATE_WORD_RE.search(title)
        if wm:
            d = int(wm.group(1))
            mo = _MONTHS.get(wm.group(2).upper()[:3])
            y = int(wm.group(3))
            if mo:
                try:
                    out["date_iso"] = datetime.date(y, mo, d).isoformat()
                except ValueError:
                    pass

    ac_m = _AIRCRAFT_RE.search(title)
    if ac_m:
        out["aircraft"] = re.sub(r"\s+", " ", ac_m.group(1)).strip().title()

    return out


def parse_detail(detail_html: str) -> dict:
    """Parse a detail page → {title, pdf_url, registration, date_iso, aircraft}.

    pdf_url is the first imgcaa CDN .pdf found in href / iframe / embed.  It is
    returned RAW (may contain spaces); download() handles encoding.
    """
    title = _best_title(detail_html)
    pdf_m = _CDN_PDF_RE.search(detail_html)
    pdf_url = _html.unescape(pdf_m.group(1)).strip() if pdf_m else None

    meta = _parse_title_meta(title)
    meta["title"] = title
    meta["pdf_url"] = pdf_url
    return meta


# ──────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────

def _encode_url(url: str) -> str:
    """%-encode unsafe characters (notably spaces / parens) in the path."""
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, parts.query, parts.fragment)
    )


def download(client, pdf_url: str, dest: str | Path) -> None:
    """GET the CDN PDF (Referer required) and write bytes to dest.

    Spaces / parentheses in the path are %-encoded before the request.
    Raises on non-2xx.
    """
    enc = _encode_url(pdf_url)
    resp = client.get(enc, headers={"Referer": CDN_REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
