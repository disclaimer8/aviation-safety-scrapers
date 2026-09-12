# aaiasb_ingest/aaiasb.py
"""Transport + HTML parsing for AAIASB (Greece).

Not bot-protected: plain httpx GET works. Pace ~1.5s between requests.
"""
import re

from . import httpc

from .text import strip_html, normalize_case_id, date_to_iso

BASE = "https://www.aaiasb.eu"
LISTING = BASE + "/en/publications/investigation-reports?start={}"
STARTS = [0, 50, 100, 150]
DELAY = 1.5
UA = "Mozilla/5.0 (compatible; FlightFinderBot/1.0; +https://flightfinder)"

_client = None


def client():
    """The shared client, not a bare httpx.Client.

    httpx's own retries= covers connect errors only, so a 502 or a read
    timeout raised on the first attempt and truncated a run. httpc (vendored
    from _common/http.py) adds response-level retries, the robots gate and the
    SSRF guard.

    harsia.gr's robots is a Cloudflare Content-Signal file: `User-agent: *`
    gets `Allow: /`, and the Disallows name AI crawlers by hand — Amazonbot,
    ClaudeBot, GPTBot, CCBot, Google-Extended, meta-externalagent,
    Bytespider, Applebot-Extended. We are none of them, and its
    `use=reference` signal matches what this data is: a safety reference, not
    training material.
    """
    global _client
    if _client is None:
        _client = httpc.make_client(
            headers={"User-Agent": UA}, timeout=60, delay=DELAY,
        )
    return _client


def get(url):
    r = client().get(url)
    r.raise_for_status()
    return r


def get_listing_html(start):
    return get(LISTING.format(start)).text


# listing parse
_ROW_RE = re.compile(r"<tr.*?</tr>", re.S)
_TD_RE = re.compile(r"<td.*?</td>", re.S)
_LINK_RE = re.compile(
    r'href="(/en/publications/investigation-reports/(\d+-[a-zA-Z0-9-]+))"'
)
_DATE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})")
_REG_RE = re.compile(r"Registration\(s\):\s*([^<]+)", re.I)
_TYPE_LABEL_RE = re.compile(
    r'uk-label[^>]*>\s*<strong>\s*(ACCIDENT|SERIOUS\s+INCIDENT|INCIDENT)\b',
    re.I,
)
_CLRFIX_RE = re.compile(r'<div class="cck-clrfix">(.*?)</div>', re.S)


def _norm_type(raw):
    if not raw:
        return None
    r = re.sub(r"\s+", " ", raw.strip()).lower()
    if r == "accident":
        return "Accident"
    if r == "serious incident":
        return "Serious incident"
    if r == "incident":
        return "Incident"
    return raw.strip().title()


def parse_listing(html):
    """Return list of row dicts: case_id, node_id, report_url, report_no,
    date_of_occurrence(ISO), report_type, location, registration, aircraft."""
    ti = html.find("<table")
    if ti < 0:
        return []
    te = html.find("</table>", ti)
    tbl = html[ti:te] if te > 0 else html[ti:]

    out = []
    for tr in _ROW_RE.findall(tbl):
        lm = _LINK_RE.search(tr)
        if not lm:
            continue
        report_url = BASE + lm.group(1)
        node_case = lm.group(2)
        node_id, _, slug = node_case.partition("-")

        tds = _TD_RE.findall(tr)
        col1 = tds[0] if len(tds) > 0 else ""
        col2 = tds[1] if len(tds) > 1 else ""
        col3 = tds[2] if len(tds) > 2 else ""

        anchor = re.search(r"<a[^>]*>(.*?)</a>", col1, re.S)
        report_no = strip_html(anchor.group(1)) if anchor else slug
        case_id = normalize_case_id(report_no) or normalize_case_id(slug)

        dm = _DATE_RE.search(col2)
        date_iso = date_to_iso(dm.group(1)) if dm else None

        tm = _TYPE_LABEL_RE.search(col2)
        report_type = _norm_type(tm.group(1)) if tm else None

        location = registration = None
        aircraft_parts = []
        for blk in _CLRFIX_RE.findall(col3):
            txt = strip_html(blk)
            if not txt:
                continue
            rm = _REG_RE.search(blk)
            if rm:
                registration = strip_html(rm.group(1)).strip()
                continue
            if "Registration(s)" in txt:
                continue
            if location is None:
                location = txt
            else:
                aircraft_parts.append(txt)
        aircraft = " ".join(aircraft_parts).strip() or None
        if registration:
            registration = registration.split(",")[0].strip() or None

        out.append({
            "case_id": case_id,
            "node_id": node_id,
            "report_url": report_url,
            "report_no": report_no,
            "date_of_occurrence": date_iso,
            "report_type": report_type,
            "location": location,
            "registration": registration,
            "aircraft": aircraft,
        })
    return out


# detail parse
_PDF_RE = re.compile(r"""(/reports/[^"'\s>]+\.pdf)""", re.I)


def parse_detail_pdfs(html):
    """Return (pdf_url_en, pdf_url_el) absolute URLs (or None each)."""
    en = el = None
    seen = []
    for p in _PDF_RE.findall(html):
        if p not in seen:
            seen.append(p)
    for p in seen:
        low = p.lower()
        absu = BASE + p
        if "english-language" in low and en is None:
            en = absu
        elif "greek-language" in low and el is None:
            el = absu
    if en is None and el is None and seen:
        el = BASE + seen[0]
    return en, el


def make_pdf_choice(pdf_url_en, pdf_url_el):
    """Prefer English. Return (chosen_url, lang)."""
    if pdf_url_en:
        return pdf_url_en, "en"
    if pdf_url_el:
        return pdf_url_el, "el"
    return None, None
