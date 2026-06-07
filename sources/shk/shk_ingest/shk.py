# shk_ingest/shk.py
"""
SHK Sweden (Statens haverikommission, shk.se) aviation investigation parser.

Enumeration: the JS listing is NOT parseable — use the sitemap instead:
    https://shk.se/sitemap1.xml.gz  →  383 aviation detail URLs
    …/search-investigation/aviation/{slug}
⚠️ slug date prefixes are the 2023-11 site-migration batch date, NOT the
occurrence date — strip them for the case_id.

Detail pages (SiteVision CMS, server-rendered for our needs):
  - <h1> title ("Helicopter accident to SE-HLK at Joesjö")
  - investigation-information block: "Date of occurrence:" +
    <time datetime=…>7 July 2004</time> (use the DISPLAY text — the
    datetime attr is UTC-shifted a day) and the diarienummer (L-22/04)
  - download links /download/<id>/<ts>/<file>.pdf with link text like
    "2005-03-03 Final report Final Report RL 2005:08 (pdf, 337kB)".
    ⚠️ Filenames are inconsistent (rl2005_08e.pdf / Summary.pdf / MBF.pdf)
    — select by suffix/text, not by pattern:
       full-EN ('…e.pdf' / 'eng' / text mentions English)  >
       EN Summary ('summary' in name/text)                 >
       Swedish full report (first PDF; SV→EN at Phase 3 like NSIA's NO).
Ongoing investigations have no report PDFs → row stays unfetched-complete
('pending' semantics via pdf_url NULL + short text) and self-heals on the
weekly re-fetch when the report publishes (rows without a PDF stay 'new').
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://shk.se"
SITEMAP_URL = BASE + "/sitemap1.xml.gz"
AVIATION_PATH = "/search-investigation/aviation/"
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,sv;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
_SLUG_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")
_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
_TIME_RE = re.compile(r"<time[^>]*>([^<]+)</time>")
_DIARIE_RE = re.compile(r"\b([LMO]-\d{1,4}/\d{2})\b")
_RL_RE = re.compile(r"\bRL[ \xa0]?(\d{4})[:.](\d{2,3})\b")
# registration in titles: SE-HLK, SE-IFD, foreign PH-IHO / YR-BCM …
_REG_RE = re.compile(r"\b([A-Z]{1,2}-[A-Z0-9]{3,5})\b")
_DL_LINK_RE = re.compile(
    r'<a[^>]*href="(/download/[^"]+\.pdf[^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_sitemap(xml):
    """Sitemap XML → sorted unique aviation detail URLs."""
    urls = sorted(
        {u for u in _LOC_RE.findall(xml or "") if AVIATION_PATH in u}
    )
    return urls


def case_id_from_url(url, taken=None):
    """Slug minus the migration-date prefix, capped, collision-suffixed."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    base = _SLUG_DATE_RE.sub("", slug)[:80].strip("-") or slug[:80]
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


def _display_date_to_iso(s):
    """'7 July 2004' → '2004-07-06'? NO — keep the display date verbatim."""
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s or "")
    if not m:
        return None
    d, mon, y = m.groups()
    mo = _MONTHS.get(mon.lower())
    if not mo:
        return None
    return f"{int(y):04d}-{mo:02d}-{int(d):02d}"


def pick_pdf(links):
    """
    links: [(href, link_text)] → (href, lang, kind) best report PDF or
    (None, None, None).  Preference: full-EN > EN Summary > Swedish full.
    """
    scored = []
    for href, text in links:
        fname = href.rsplit("/", 1)[-1].lower()
        t = (text or "").lower()
        if "css" in fname or "js" in fname:
            continue
        # ⚠️ order: summary check BEFORE the text-english check — summary
        # links are titled "Summary in English" and would masquerade as
        # full-EN otherwise.
        if "summary" in fname or "summary" in t:
            score, lang = 1, "en-summary"
        elif re.search(r"e\.pdf$|_eng|english", fname) or "english" in t:
            score, lang = 0, "en"
        else:
            score, lang = 2, "sv"
        kind = "Final" if "final" in t or fname.startswith("rl") else None
        scored.append((score, href, lang, kind))
    if not scored:
        return None, None, None
    scored.sort(key=lambda x: x[0])
    _, href, lang, kind = scored[0]
    return href, lang, kind


def parse_detail(html):
    """
    Detail page → dict: title, event_date (ISO from the DISPLAY text),
    diarienummer, rl_number, registration, pdf_href (best, relative),
    lang, report_kind.  Ongoing investigations → pdf_href None.
    """
    out = {"title": None, "event_date": None, "diarienummer": None,
           "rl_number": None, "registration": None, "pdf_href": None,
           "lang": None, "report_kind": None}
    t = _TITLE_RE.search(html or "")
    if t:
        out["title"] = _strip(t.group(1))
        reg = _REG_RE.search(out["title"])
        if reg:
            out["registration"] = reg.group(1)

    # occurrence date: the <time> inside the investigation-information block
    info = re.search(
        r'investigation-information.*?<time[^>]*>([^<]+)</time>',
        html or "", re.DOTALL,
    )
    if info:
        out["event_date"] = _display_date_to_iso(_strip(info.group(1)))

    d = _DIARIE_RE.search(_strip(html or ""))
    if d:
        out["diarienummer"] = d.group(1)
    rl = _RL_RE.search(_strip(html or ""))
    if rl:
        out["rl_number"] = f"RL {rl.group(1)}:{rl.group(2)}"

    links = [(m.group(1), _strip(m.group(2)))
             for m in _DL_LINK_RE.finditer(html or "")]
    href, lang, kind = pick_pdf(links)
    if href:
        out["pdf_href"] = _html.unescape(href)
        out["lang"] = lang
        out["report_kind"] = kind or "Final"
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_sitemap(client):
    import gzip
    import io

    resp = client.get(SITEMAP_URL)
    resp.raise_for_status()
    raw = resp.content
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return raw.decode("utf-8", "replace")


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, href, dest_path):
    resp = client.get(urljoin(BASE, href))
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
