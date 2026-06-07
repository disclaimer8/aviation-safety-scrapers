# india_ingest/india.py
"""
AAIB India (Aircraft Accident Investigation Bureau, aaib.gov.in) index and
PDF-metadata parser.

The whole catalogue is ONE page — https://aaib.gov.in/index.html — with bare
relative hrefs `Reports/{YEAR}/{TYPE}/{file}.pdf` (~250 links, 2012→now).
No pagination, no JS, no anti-bot.  ⚠️ Directory names are inconsistent
(`Accident`/`accident`, `Serious Incident`/`SeriousIncident`/`INCIDENT`) —
normalize.  Preliminary/interim reports are SKIPPED by filename; everything
else (Final/Accepted/unmarked) is a published report.

Narratives are text-layer PDFs (verified old 2014 = 37K chars, new 2025 =
123K).  Metadata lives only IN the PDF; two era formats:
  - new (title-phrase): "Final Investigation Report on Accident involving
    Spice Jet's B-737-800 aircraft bearing registration VT SLH while
    en-route Durgapur on 01 May 2022"
  - old (labeled table): "Aircraft Type : PC-12/45 … Registration : VT - DAR
    … Operator : Deccan Charter Pvt. Ltd."
Both parsed best-effort; nulls are fine.
"""
import html as _html
import re
from urllib.parse import quote, urljoin

BASE = "https://aaib.gov.in/"
INDEX_URL = BASE + "index.html"
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ──────────────────────────────────────────────────────────────────────────────
# Index parsing
# ──────────────────────────────────────────────────────────────────────────────

_PDF_HREF_RE = re.compile(r'href="(Reports/[^"]+\.pdf)"', re.IGNORECASE)
_PRELIM_RE = re.compile(r"prelim|interim", re.IGNORECASE)
# registration in a filename: "VT-SLH" / "VT_SLH" / "VT SLH".
# ⚠️ no trailing \b — '_' is a word char, so "VT_RGF_Sultanpur" would fail it.
_REG_RE = re.compile(r"\bVT[\s_-]?([A-Z]{3})(?![A-Za-z])")

_KIND_MAP = {
    "accident": "Accident",
    "serious incident": "Serious Incident",
    "seriousincident": "Serious Incident",
    "incident": "Incident",
}


def _normalize_kind(raw):
    return _KIND_MAP.get((raw or "").strip().lower(), (raw or "").strip() or None)


def parse_index(html):
    """
    Parse index.html → list of dicts for NON-preliminary report PDFs:
        pdf_url      – absolute, URL-encoded
        rel_path     – raw relative href (unique key)
        year         – from the path
        report_kind  – normalized Accident / Serious Incident / Incident
        registration – "VT-XXX" from the filename or None
    Order preserved; duplicates (same rel_path) dropped.
    """
    seen = set()
    out = []
    for href in _PDF_HREF_RE.findall(html):
        rel = _html.unescape(href)
        if rel in seen:
            continue
        seen.add(rel)
        parts = rel.split("/")
        # Reports/{YEAR}/{TYPE}/{file}.pdf
        year = parts[1] if len(parts) > 2 and parts[1].isdigit() else None
        kind = _normalize_kind(parts[2]) if len(parts) > 3 else None
        fname = parts[-1]
        if _PRELIM_RE.search(fname):
            continue
        reg_m = _REG_RE.search(fname.replace(".pdf", ""))
        registration = f"VT-{reg_m.group(1)}" if reg_m else None
        out.append(
            {
                "pdf_url": urljoin(BASE, quote(rel)),
                "rel_path": rel,
                "year": year,
                "report_kind": kind,
                "registration": registration,
            }
        )
    return out


def make_case_id(year, registration, rel_path, taken=None):
    """
    Synthetic case_id (AAIB India has no official numbering):
        "{year}_{REG}"  e.g. "2022_VT-SLH"
    Fallback without a registration: "{year}_{slugged-filename[:40]}".
    `taken` (set/callable-free collection) → append _2, _3… on collision
    (same reg can have reports in the same year).
    """
    from .text import slugify

    if registration:
        base = f"{year or 'na'}_{registration}"
    else:
        fname = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        base = f"{year or 'na'}_{slugify(fname)[:40]}"
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}_{n}"
        n += 1
    return cand


# ──────────────────────────────────────────────────────────────────────────────
# PDF metadata extraction (best-effort; nulls are fine)
# ──────────────────────────────────────────────────────────────────────────────

_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}
# also short month names
_MONTHS.update({m[:3]: v for m, v in list(_MONTHS.items())})

# "on 01 May 2022" / "ON 28 June, 2022"
_DATE_WORDS_RE = re.compile(
    r"\bon\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})", re.IGNORECASE
)
# "ON 28-11-2014" / "on 28.11.2014" / "on 28/11/2014"
_DATE_NUMERIC_RE = re.compile(r"\bon\s+(\d{1,2})[-./](\d{1,2})[-./](\d{4})", re.IGNORECASE)
# labeled: "Date of Accident : 28-11-2014" (old table format)
_DATE_LABEL_RE = re.compile(
    r"Date\s+(?:of|&\s*Time\s+of)?\s*(?:the\s+)?(?:Accident|Incident|Occurrence)[^:]*:\s*"
    r"(\d{1,2})[-./\s]+([A-Za-z]+|\d{1,2})[-./\s,]+(\d{4})",
    re.IGNORECASE,
)

_REG_TEXT_RE = re.compile(r"\bVT\s*[-_]?\s*([A-Z]{3})(?![A-Za-z])")

# labeled fields (old table format; pdftotext splits "label\n\n: value")
def _labeled(text, label_re):
    m = re.search(label_re + r"\s*[\s\n]*:\s*([^\n]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


# new title-phrase: "involving Spice Jet's B-737-800 aircraft" /
# "involving M/s ... PC12/45 AIRCRAFT" / "accident involving Pawan Hans' S-76D helicopter"
# mid-2010s variant: "Accident to Pawan Hans Helicopters Limited (PHHL) Bell 407 Helicopter VT-PHH"
_INVOLVING_RE = re.compile(
    r"(?:involving|(?:accident|incident)\s+to)\s+(?:M/s\s+)?(.+?)\s+(?:aircraft|helicopter)\b",
    re.IGNORECASE | re.DOTALL,
)
# operator/aircraft split inside the "to <Operator> <Type>" blob: company
# suffix or a parenthesized abbreviation ends the operator part.
_OPERATOR_SPLIT_RE = re.compile(
    r"^(.+?(?:(?:Limited|Ltd\.?|Pvt\.?(?:\s+Ltd\.?)?)(?:\s*\([A-Z]{2,6}\))?"
    r"|\([A-Z]{2,6}\)))\s+(.+)$"
)
# location: "at Mumbai off-shore on", "AT GUWAHATI ON", "while en-route Durgapur on"
_LOCATION_RE = re.compile(
    r"\b(?:at|near|while\s+en[-\s]?route(?:\s+to)?)\s+(.{3,60}?)\s+on\s+\d",
    re.IGNORECASE,
)


def _iso_date(d, mo, y):
    try:
        d = int(d)
        mo_i = int(mo) if str(mo).isdigit() else _MONTHS.get(str(mo).lower()[:3])
        y = int(y)
        if not mo_i or not (1 <= d <= 31) or not (1 <= mo_i <= 12):
            return None
        return f"{y:04d}-{mo_i:02d}-{d:02d}"
    except (ValueError, TypeError):
        return None


def parse_pdf_meta(text):
    """
    Best-effort metadata from the first pages of an AAIB India report PDF.
    Returns dict: registration, aircraft, operator, location, event_date —
    each may be None.
    """
    head = text[:4000]
    out = {"registration": None, "aircraft": None, "operator": None,
           "location": None, "event_date": None}

    reg_m = _REG_TEXT_RE.search(head)
    if reg_m:
        out["registration"] = f"VT-{reg_m.group(1)}"

    # date: title words → title numeric → labeled
    for pat in (_DATE_WORDS_RE, _DATE_NUMERIC_RE):
        m = pat.search(head)
        if m:
            d, mo, y = m.groups()
            # words pattern: (day, monthname, year); numeric: (d, m, y)
            out["event_date"] = _iso_date(d, mo, y)
            if out["event_date"]:
                break
    if not out["event_date"]:
        m = _DATE_LABEL_RE.search(head)
        if m:
            out["event_date"] = _iso_date(*m.groups())

    # aircraft + operator: labeled table first (more precise), then title phrase
    out["aircraft"] = _labeled(head, r"Aircraft\s+Type")
    out["operator"] = _labeled(head, r"Operator")
    if not out["aircraft"]:
        m = _INVOLVING_RE.search(head)
        if m:
            blob = re.sub(r"\s+", " ", m.group(1)).strip()
            # strip trailing "bearing registration VT XXX ..." tail
            blob = re.sub(r"\s+bearing\s+registration\b.*$", "", blob,
                          flags=re.IGNORECASE)
            # possessive operator: "Spice Jet's B-737-800" / "Pawan Hans' S-76D"
            pm = re.match(r"(.+?)[’']s?\s+(.+)$", blob)
            # company-suffix operator: "Pawan Hans Helicopters Limited (PHHL) Bell 407"
            sm = _OPERATOR_SPLIT_RE.match(blob)
            if pm and not out["operator"]:
                out["operator"] = pm.group(1).strip()
                out["aircraft"] = pm.group(2).strip()
            elif sm:
                if not out["operator"]:
                    out["operator"] = sm.group(1).strip()
                out["aircraft"] = sm.group(2).strip()
            else:
                out["aircraft"] = blob

    m = _LOCATION_RE.search(head)
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")
        # don't swallow "registration VT SLH" into location
        if not _REG_TEXT_RE.search(loc):
            out["location"] = loc
    if not out["location"]:
        # date-first order: "on 30-12-2012 at Katra Valley, Jammu & Kashmir"
        m = re.search(r"\bon\s+\d[\d\-./]*\s+at\s+(.{3,60}?)(?:[\n.]|$)", head,
                      re.IGNORECASE)
        if m:
            out["location"] = re.sub(r"\s+", " ", m.group(1)).strip(" ,.")

    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_index(client):
    resp = client.get(INDEX_URL)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
