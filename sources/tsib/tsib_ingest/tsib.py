# tsib_ingest/tsib.py
"""
TSIB Singapore (Transport Safety Investigation Bureau, formerly AAIB
Singapore) aviation-reports listing + report-PDF parser.

Listing:
    https://www.mot.gov.sg/what-we-do/transport-investigations/aviation/
        aviation-reports/?page=N
A Next.js / Isomer STATIC-RENDERED page.  ⚠️ Probed live 2026-06-04: the
`?page=N` query string is a NO-OP — every page returns identical HTML and
the SAME 10 rendered <a> anchors.  The FULL catalogue (~100 items) is not
client-paginated away: all of them are embedded in the page's inline RSC
JSON payload as escaped objects, e.g.

    \\"date\\":\\"$D2009-05-13T00:00:00.000Z\\",\\"category\\":\\"Incident\\",
    \\"title\\":\\"Air traffic incident  [PDF, 109 KB]\\",
    \\"description\\":\\"Airbus A320/Boeing B747-400\\",
    \\"referenceLinkHref\\":\\"https://isomer-user-content.by.gov.sg/287/
        {uuid}/{filename}.pdf\\"

So discover() parses ALL items from one fetch.  We still WALK page=1..N and
apply CLAMP-STOP (stop when a page's first PDF URL repeats one already seen)
as a safety net against the server starting to truly paginate later.

Each item → pdf_url (referenceLinkHref, the PK), event/publish date,
title, aircraft (description), report_kind (category: Incident/Accident).
Registration is best-effort from title/description/filename: (9V-XXX),
(9M-MLL), B-305J …

case_id: the formal id printed inside the PDF —
    (?:TIB|AIB|IB)/AAI/[A-Z]+\\.\\d+   (old 'AIB/AAI/CAS.058',
    new 'TIB/AAI/CAS.246').  Canonicalise '/'->'-' and '.'->'-', lowercase
    ('aib-aai-cas-058').  Fallback when absent: the UUID path segment of the
    PDF URL.  Extracted at fetch() time; NULL until then.

PDFs are native English with clean text layers (10K-35K chars).  Old
filenames contain SPACES and PARENS → href is already absolute; percent-
encode on download.
"""
import html as _html
import re
from urllib.parse import quote, urlparse

BASE = "https://www.mot.gov.sg"
LISTING_URL = (
    BASE + "/what-we-do/transport-investigations/aviation/aviation-reports/"
)
DELAY = 1.5
MAX_PAGES = 30

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-SG,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Inline RSC JSON item: fields appear in this order, escaped as \"key\":\"val\".
# date may be prefixed '$D' (a React-Flight Date marker). category/title may
# be absent on a few items, so they are tolerated as optional groups.
_ITEM_RE = re.compile(
    r'\\"date\\":\\"(?:\$D)?(?P<date>[^\\"]*)\\".{0,600}?'
    r'\\"referenceLinkHref\\":\\"(?P<href>https://isomer-user-content[^\\"]+?\.pdf)\\"',
    re.DOTALL | re.IGNORECASE,
)
# Pull category/title/description out of the same object window (best-effort).
_CAT_RE = re.compile(r'\\"category\\":\\"([^\\"]*)\\"')
_TITLE_RE = re.compile(r'\\"title\\":\\"([^\\"]*)\\"')
_DESC_RE = re.compile(r'\\"description\\":\\"([^\\"]*)\\"')

# Rendered-anchor fallback (the visible 10): aria-label + isomer pdf href.
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*\baria-label="(?P<label>[^"]*)"[^>]*\bhref="(?P<href>https://isomer-user-content[^"]+?\.pdf)"',
    re.IGNORECASE,
)
_ANCHOR_HREF_FIRST_RE = re.compile(
    r'<a\b[^>]*\bhref="(?P<href>https://isomer-user-content[^"]+?\.pdf)"[^>]*\baria-label="(?P<label>[^"]*)"',
    re.IGNORECASE,
)

_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}

# aria-label: "19 May 2025 Status Past Reports Boeing B737-800 Incident
#              (opens in new tab)"
_LABEL_DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\b")
_LABEL_KIND_RE = re.compile(r"\b(Accident|Incident)\b", re.IGNORECASE)

# registration forms: (9V-OJD), (9M-MLL), 9V-XXX, B-305J, N123AB, G-ABCD …
_REG_RE = re.compile(
    r"\b(9[VM]-[A-Z0-9]{3,4}|B-[A-Z0-9]{3,5}|[A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{2,5}[A-Z]{0,2})\b"
)

# case_id printed inside the PDF (both eras).
_CASE_ID_RE = re.compile(r"(?:TIB|AIB|IB)/AAI/[A-Z]+\.\d+")


def _unescape_json(s):
    """Decode the \\" / \\n escaping used in the inline RSC payload."""
    if s is None:
        return None
    s = s.replace('\\"', '"').replace("\\/", "/").replace("\\n", " ")
    s = s.replace("\\u0026", "&").replace("\\\\", "\\")
    return _html.unescape(s)


def _clean_title(t):
    if not t:
        return None
    t = _unescape_json(t)
    # drop trailing "[PDF, 633 KB]" size hints
    t = re.sub(r"\s*\[PDF[^\]]*\]\s*$", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip() or None


def _iso_date(raw):
    """'2009-05-13T00:00:00.000Z' or '2009-05-13' -> '2009-05-13'."""
    if not raw:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return m.group(0) if m else None


def _report_kind(category, title):
    src = f"{category or ''} {title or ''}"
    return "Accident" if re.search(r"\baccident\b", src, re.IGNORECASE) else "Incident"


def _registration(*sources):
    for s in sources:
        if not s:
            continue
        m = _REG_RE.search(s)
        if m:
            return m.group(1).upper()
    return None


def percent_encode(url):
    """Old TSIB filenames carry literal spaces/parens — encode for download."""
    return quote(_html.unescape(url), safe=":/%()&,'-_.~")


def uuid_from_url(url):
    """The {uuid} path segment of an isomer-user-content PDF URL, or None."""
    try:
        parts = urlparse(url).path.strip("/").split("/")
    except Exception:
        return None
    for p in parts:
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", p):
            return p.lower()
    return None


def canonical_case_id(raw):
    """'AIB/AAI/CAS.058' -> 'aib-aai-cas-058'."""
    if not raw:
        return None
    return raw.lower().replace("/", "-").replace(".", "-")


def extract_case_id(pdf_text):
    """
    Formal id from the first 4000 chars of pdftotext output, canonicalised.
    Returns None when absent (caller falls back to the UUID path segment).
    """
    head = (pdf_text or "")[:4000]
    m = _CASE_ID_RE.search(head)
    if not m:
        return None
    return canonical_case_id(m.group(0))


def make_case_id(pdf_text, pdf_url, taken=None):
    """
    Resolve a case_id: prefer the in-PDF formal id, else the URL UUID, else
    a url-hash fallback.  Suffix '-2', '-3' … on collision with `taken`.
    """
    base = extract_case_id(pdf_text)
    if not base:
        uuid = uuid_from_url(pdf_url)
        base = f"tsib-{uuid}" if uuid else "tsib-" + str(abs(hash(pdf_url)))[:10]
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def parse_anchor_label(label):
    """
    aria-label -> {event_date, title, report_kind, registration}.
    "19 May 2025 Status Past Reports Boeing B737-800 Incident (opens in new tab)"
    """
    out = {"event_date": None, "title": None, "report_kind": "Incident",
           "registration": None}
    if not label:
        return out
    lbl = re.sub(r"\s*\(opens in new tab\)\s*$", "", label).strip()
    m = _LABEL_DATE_RE.match(lbl)
    rest = lbl
    if m:
        d, mon, y = m.groups()
        mo = _MONTHS.get(mon.lower())
        if mo:
            out["event_date"] = f"{int(y):04d}-{mo:02d}-{int(d):02d}"
        rest = lbl[m.end():]
    # strip the "Status Past Reports" marker
    rest = re.sub(r"^\s*Status\s+Past\s+Reports\s*", "", rest,
                  flags=re.IGNORECASE)
    km = _LABEL_KIND_RE.search(rest)
    if km:
        out["report_kind"] = km.group(1).title()
        rest = rest[:km.start()] + rest[km.end():]
    out["title"] = re.sub(r"\s+", " ", rest).strip() or None
    out["registration"] = _registration(label)
    return out


def parse_listing(page_html):
    """
    All catalogue items from one listing page.  Prefers the inline RSC JSON
    (full ~100-item set); falls back to rendered <a aria-label> anchors.
    Returns a list of dicts keyed for discover():
        {pdf_url, page_url, title, aircraft, registration, report_kind,
         event_date}
    Order preserved; deduped on pdf_url (first wins).
    """
    items, seen = [], set()

    for m in _ITEM_RE.finditer(page_html or ""):
        href = _unescape_json(m.group("href"))
        if href in seen:
            continue
        # search a window after the matched href-start for cat/title/desc that
        # belong to the same object (they precede href in source order).
        window = page_html[max(0, m.start() - 700):m.end()]
        cat = _CAT_RE.search(window)
        title = _TITLE_RE.search(window)
        desc = _DESC_RE.search(window)
        cat = _unescape_json(cat.group(1)) if cat else None
        title_txt = _clean_title(title.group(1)) if title else None
        aircraft = _clean_title(desc.group(1)) if desc else None
        seen.add(href)
        items.append({
            "pdf_url": href,
            "page_url": LISTING_URL,
            "title": title_txt,
            "aircraft": aircraft,
            "registration": _registration(title_txt, aircraft, href),
            "report_kind": _report_kind(cat, title_txt),
            "event_date": _iso_date(m.group("date")),
        })

    if items:
        return items

    # Fallback: rendered anchors only.
    for rx in (_ANCHOR_RE, _ANCHOR_HREF_FIRST_RE):
        for m in rx.finditer(page_html or ""):
            href = _html.unescape(m.group("href"))
            if href in seen:
                continue
            meta = parse_anchor_label(m.group("label"))
            seen.add(href)
            items.append({
                "pdf_url": href,
                "page_url": LISTING_URL,
                "title": meta["title"],
                "aircraft": None,
                "registration": meta["registration"] or _registration(href),
                "report_kind": meta["report_kind"],
                "event_date": meta["event_date"],
            })
    return items


def first_pdf_url(page_html):
    """First PDF URL on a page (for clamp-stop), or None."""
    items = parse_listing(page_html)
    return items[0]["pdf_url"] if items else None


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_page(client, page):
    resp = client.get(LISTING_URL, params={"page": page})
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(percent_encode(url))
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
