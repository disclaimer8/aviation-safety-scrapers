# aicpng_ingest/aicpng.py
"""AIC (Accident Investigation Commission, Papua New Guinea) scraper.

Source: https://aic.gov.pg/investigation?page=N
- Listing is a paginated, server-rendered Drupal VIEW table (20 rows/page,
  ?page=N 0-indexed, ~5 pages / ~91 reports).  Each <tr> carries rich columns:
  Investigation No. (case_id + /investigation/{nid} link), Occurrence Class,
  Occurrence Date (DD Month YYYY), Registration, Location, Status, View.
  A page past the end returns HTTP 200 with zero data rows → stop-on-empty.
- Detail page (/investigation/{nid}) hosts the report PDFs in "Final Reports"
  / "Preliminary Reports" / etc. VIEW tables; the PDF path is the plain text of
  a <td class="views-field-field-attachment"> cell (also the data-attachment
  attr), a site-local /sites/default/files/...pdf URL (URL-encoded).
  Final-report PDFs carry a text layer → pdftotext works.

⚠️ Drupal BigPipe: on a cache MISS the table region may arrive as JSON command
payloads in <script type="application/vnd.drupal-ajax"> tags instead of flat
HTML (zero rows for a naive parser).  We send the documented big_pipe_nojs=1
cookie AND defensively expand any placeholder payloads before parsing.

⚠️ case_id has a SPACE on-site ("AIC 26-1003").  make_case_id /
_normalize_case_id collapse whitespace around the separators to the canonical
"AIC-26-1003"; site_slug is the lowercase form "aic-26-1003".  English source.
"""
import html as _html
import json
import re
import datetime
from pathlib import Path
from urllib.parse import urljoin

BASE = "https://aic.gov.pg"
LISTING_URL = BASE + "/investigation"
REFERER = LISTING_URL
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": REFERER,
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# Drupal BigPipe: force the single-flush no-JS path so the table arrives as
# flat HTML even on a Varnish cache miss.
COOKIES = {"big_pipe_nojs": "1"}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# ── case_id normalisation ─────────────────────────────────────────────────────
# On-site form: "AIC 26-1003" (space after AIC).  Canonical: "AIC-26-1003".
_CASE_TOKEN_RE = re.compile(r"AIC[\s/-]*\d{2}[\s/-]*\d{3,4}", re.IGNORECASE)
# whitespace around any '-' or '/' separator
_SEP_WS_RE = re.compile(r"\s*([-/])\s*")


def _normalize_case_id(raw):
    """Canonicalise a raw case reference.

    Collapses whitespace around '-' and '/' separators and converts the
    "AIC NN-NNNN" space form to "AIC-NN-NNNN".  Returns uppercased str or None.
    """
    if not raw:
        return None
    s = _html.unescape(raw)
    s = re.sub(r"\s+", " ", s).strip().upper()
    if not s:
        return None
    # collapse whitespace that hugs a '-' or '/' separator, then unify the
    # separator to '-' (some references use "AIC YY/NNNN")
    s = _SEP_WS_RE.sub(r"\1", s).replace("/", "-")
    # convert the remaining "AIC <digits>" space into a hyphen
    s = re.sub(r"^AIC\s+", "AIC-", s)
    s = re.sub(r"\s+", "-", s)
    return s or None


def make_case_id(raw):
    """Extract + normalise the first AIC case reference from arbitrary text."""
    if not raw:
        return None
    m = _CASE_TOKEN_RE.search(_html.unescape(raw))
    if not m:
        return None
    return _normalize_case_id(m.group(0))


def make_site_slug(case_id):
    """Lowercase canonical case_id → site slug, e.g. 'aic-26-1003'."""
    return (case_id or "").lower()


# ── client ────────────────────────────────────────────────────────────────────

def make_client():
    import httpx
    return httpx.Client(
        headers=HEADERS,
        cookies=COOKIES,
        follow_redirects=True,
        timeout=60.0,
    )


# ── BigPipe defence ───────────────────────────────────────────────────────────

_BIG_PIPE_RE = re.compile(
    r'<script type="application/vnd\.drupal-ajax"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _expand_big_pipe(html):
    """Append HTML hidden in BigPipe placeholder payloads so the row regexes
    can see it.  No-op for flat (cached) pages."""
    extra = []
    for payload in _BIG_PIPE_RE.findall(html):
        payload = payload.strip()
        if not payload.startswith("["):
            continue
        try:
            cmds = json.loads(payload)
        except ValueError:
            continue
        for cmd in cmds:
            data = cmd.get("data") if isinstance(cmd, dict) else None
            if isinstance(data, str) and data:
                extra.append(data)
    return html + "\n".join(extra) if extra else html


# ── listing parser ────────────────────────────────────────────────────────────

_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL | re.IGNORECASE)
_NODE_LINK_RE = re.compile(r'href="(/investigation/(\d+))"')
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")


def _clean(fragment):
    txt = re.sub(r"<[^>]+>", " ", fragment or "")
    txt = _html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _parse_date(s):
    """'17 April 2026' → '2026-04-17' ISO, or None."""
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTHS.get(mon)
    if not month:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_listing(html):
    """Parse one listing page → list of row dicts.

    Each dict:
        case_id      canonical 'AIC-26-1003'
        report_url   absolute /investigation/{nid} detail URL
        event_class  Occurrence Class column ('Accident' | 'Incident' | …)
        date_of_occurrence  ISO date or None
        registration aircraft registration or None
        location     location text or None
        status       Status column ('Ongoing' | 'Closed' | …)
        title        full first-column text (with case_id)

    Header rows and rows without a parseable case_id are skipped.
    """
    html = _expand_big_pipe(html)
    rows = []
    for tr_m in _TR_RE.finditer(html):
        tr = tr_m.group(1)
        tds = _TD_RE.findall(tr)
        if len(tds) < 6:
            continue  # header row (uses <th>) or malformed
        first = tds[0]
        case_id = make_case_id(_clean(first))
        if not case_id:
            continue

        link_m = _NODE_LINK_RE.search(first)
        report_url = urljoin(BASE, link_m.group(1)) if link_m else None

        event_class = _clean(tds[1]) or None
        date_iso = _parse_date(_clean(tds[2]))
        registration = _clean(tds[3]) or None
        location = _clean(tds[4]) or None
        status = _clean(tds[5]) or None

        rows.append({
            "case_id": case_id,
            "report_url": report_url,
            "event_class": event_class,
            "date_of_occurrence": date_iso,
            "registration": registration,
            "location": location,
            "status": status,
            "title": _clean(first),
        })
    return rows


# ── detail parser (PDF discovery) ─────────────────────────────────────────────

# Plain-text attachment cells in the per-section report VIEW tables.
_ATTACH_TD_RE = re.compile(
    r'views-field-field-attachment[^>]*>\s*(/sites/default/files/[^<\s]+?\.pdf[^<\s]*)',
    re.IGNORECASE,
)
_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL | re.IGNORECASE)

# Preference order: most substantial narrative first.
_SECTION_RANK = (
    "final", "interim factual", "interim", "preliminary", "discontinued",
)


def _section_rank(header):
    h = (header or "").lower()
    for i, key in enumerate(_SECTION_RANK):
        if key in h:
            return i
    return len(_SECTION_RANK) + 1  # unknown sections last


def parse_detail(html):
    """Parse a detail page → list of (report_type, pdf_url) tuples, ordered by
    narrative-substance preference (Final → Interim → Preliminary → …).

    pdf_url is absolute and left URL-encoded (download handles it as-is).
    """
    html = _expand_big_pipe(html)
    found = []
    for m in _ATTACH_TD_RE.finditer(html):
        path = m.group(1).strip()
        # nearest preceding <h3> is the section header
        pre = html[:m.start()]
        hh = _H3_RE.findall(pre)
        header = _clean(hh[-1]) if hh else None
        url = urljoin(BASE, _html.unescape(path)) if "%" not in path else BASE + path
        found.append((header, url))
    # stable sort by section preference
    found.sort(key=lambda t: _section_rank(t[0]))
    # de-dup by url, preserve sorted order
    seen, out = set(), []
    for header, url in found:
        if url in seen:
            continue
        seen.add(url)
        out.append((header, url))
    return out


def best_pdf(html):
    """Return (report_type, pdf_url) for the preferred report, or (None, None)."""
    found = parse_detail(html)
    return found[0] if found else (None, None)


# ── HTTP helpers (live network) ───────────────────────────────────────────────

def fetch_listing_page(client, page):
    resp = client.get(LISTING_URL, params={"page": page})
    resp.raise_for_status()
    return resp.text


def fetch_detail(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download(client, pdf_url, dest):
    resp = client.get(pdf_url, headers={"Referer": REFERER})
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        fh.write(resp.content)
    return dest
