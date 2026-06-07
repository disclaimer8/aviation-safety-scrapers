# ovv_ingest/ovv.py
"""
OVV / Dutch Safety Board (Onderzoeksraad voor Veiligheid, onderzoeksraad.nl)
aviation investigation parser.

Listing: `/en/home/investigations/?_aviation_tax=uncategorized&_page=N`
(10 links/page, ~80 pages, stop-on-empty).  ⚠️ `uncategorized` (real
investigations) NOT `*` — the `quarterly-aviation-report` sub-tax is
bundled multi-occurrence stubs we skip.  The filter IS honored server-side.

Detail pages `/en/onderzoek/{slug}/`: <h1> title, summary <p>s, and the
report documents as HASH-SLUG links `https://onderzoeksraad.nl/
{12-hex}{name}-pdf/` which 301 to `/wp-content/uploads/.../*.pdf`.
(The 2026 redesign hid them visually but they are in the raw HTML.
WP REST is 403 — don't try it.)

Document pick: prefer English (`_en`/`eng` in name) > main report
(`rapport`/`report`) > anything not an appendix/recommendations/brochure/
response-letter.  Some docs are scans/letters → caller falls through to
the next candidate when pdftotext yields nothing.

Language: `_en`-named docs are English; the rest Dutch (NL→EN at Phase 3).
case_id = the investigation slug.
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://onderzoeksraad.nl"
LISTING_URL = BASE + "/en/home/investigations/"
DELAY = 2.0  # Cloudflare present — pace politely

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,nl;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DETAIL_RE = re.compile(r'href="(https://onderzoeksraad\.nl/en/onderzoek/([^"/]+)/?)"')
# ⚠️ doc slugs come in TWO shapes: hash-prefixed ('f95ffc3669c4report_…-pdf/',
# MH17-era) and bare ('rapport_taxibaan_en_web-pdf/', most pages). The first
# backfill required the hex prefix and silently missed 677/763 pages' docs.
_DOC_RE = re.compile(
    r'href="(https://onderzoeksraad\.nl/[^"/]+-pdf/)"'
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)
# common Dutch/foreign registration shapes in titles (best-effort)
_REG_RE = re.compile(r"\b(PH-[A-Z0-9]{3,4}|[A-Z]{1,2}-[A-Z0-9]{3,5}|N\d{2,5}[A-Z]{0,2})\b")
_DATE_RE = re.compile(r"\b(\d{1,2})\s+([A-Z][a-z]+)\s+(\d{4})\b")
_MONTHS = {
    m.lower(): i + 1
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"]
    )
}

_NOISE_DOC_RE = re.compile(
    r"aanbeveling|appendix|bijlage|brochure|reactie|samenvatting|infographic",
    re.IGNORECASE,
)


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_listing(html):
    """One listing page → ordered unique detail dicts {url, slug}."""
    out = []
    seen = set()
    for m in _DETAIL_RE.finditer(html or ""):
        url, slug = m.group(1).rstrip("/") + "/", m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        out.append({"url": url, "slug": slug})
    return out


def rank_docs(hrefs):
    """
    Order candidate doc URLs best-first:
      0 English main report  1 main report  2 English other
      3 other non-noise      4 noise (appendix/letters/…)
    """
    def score(href):
        name = href.rstrip("/").rsplit("/", 1)[-1].lower()
        en = bool(re.search(r"_en\b|_en-|_eng|english", name))
        main = bool(re.search(r"report|rapport", name))
        noise = bool(_NOISE_DOC_RE.search(name))
        if noise:
            return 4
        if en and main:
            return 0
        if main:
            return 1
        if en:
            return 2
        return 3

    uniq = list(dict.fromkeys(hrefs))
    return sorted(uniq, key=score)


def doc_lang(href):
    name = href.rstrip("/").rsplit("/", 1)[-1].lower()
    return "en" if re.search(r"_en\b|_en-|_eng|english", name) else "nl"


def parse_detail(html):
    """
    Detail page → dict: title, summary, registration, event_date,
    doc_urls (ranked best-first; [] for ongoing/doc-less investigations).
    """
    out = {"title": None, "summary": None, "registration": None,
           "event_date": None, "doc_urls": []}
    h1 = _H1_RE.search(html or "")
    if h1:
        out["title"] = _strip(h1.group(1))
        reg = _REG_RE.search(out["title"])
        if reg:
            out["registration"] = reg.group(1)
        d = _DATE_RE.search(out["title"])
        if d:
            day, mon, year = d.groups()
            mo = _MONTHS.get(mon.lower())
            if mo:
                out["event_date"] = f"{int(year):04d}-{mo:02d}-{int(day):02d}"

    # summary: first substantial <p> in the content region
    body = re.sub(r"<script.*?</script>", "", html or "", flags=re.DOTALL)
    for p in _P_RE.findall(body):
        t = _strip(p)
        if len(t) > 120:
            out["summary"] = t
            break

    out["doc_urls"] = rank_docs(_DOC_RE.findall(html or ""))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing_page(client, page):
    resp = client.get(
        LISTING_URL,
        params={"_aviation_tax": "uncategorized", "_page": page},
    )
    resp.raise_for_status()
    return resp.text


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)  # follows the -pdf/ → uploads 301
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
