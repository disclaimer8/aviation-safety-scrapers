# ovv_ingest/ovv.py
"""
OVV / Dutch Safety Board (Onderzoeksraad voor Veiligheid, onderzoeksraad.nl)
aviation investigation parser.

Listing: `/en/home/investigations/?_page=N` (10 links/page, stop-on-empty).
The 2026-08 redesign removed the server-side aviation filter, so the walk
sees every theme OVV investigates: parse_listing() drops obvious
non-aviation slugs and build() publishes only reports whose TEXT is about
aviation (looks_like_aviation). The site sits behind a bot check that _get()
passes through /__hv_token.

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

# The country this source covers, as ISO 3166-1 alpha-2. Declared rather
# than inferred: the coverage database and the scraper inventory had drifted
# apart, and only 32 of 90 sources stated their country anywhere a machine
# could read. scripts/check_coverage.py reconciles the two from this.
COUNTRY_ISO2 = "NL"


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

# Documents that are a PART of an investigation rather than the report of it.
#
# This list used to be Dutch-only while rank_docs scored English highest, so an
# English sectional PDF — "recommendations_report_en", "summary_report_en" —
# outranked the full Dutch report and became the stored narrative. That is
# where narratives beginning mid-section ("5 RECOMMENDATIONS …") came from:
# the section is easily longer than the 2000-char floor, so it won and the
# walk stopped.
# "response" is deliberately absent even though response letters exist: OVV's
# "safe_flight_routes_responses_to_escalating_conflicts_2021_report" is a real
# 242,000-character report, and the word costs more than it earns. "reactie",
# the Dutch equivalent, is precise enough to keep.
_NOISE_DOC_RE = re.compile(
    r"aanbeveling|appendix|bijlage|brochure|reactie|samenvatting|infographic"
    r"|recommendation|conclusion|summary|annex|letter|factsheet",
    re.IGNORECASE,
)


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


# The listing lost its server-side aviation filter (see fetch_listing_page), so
# the walk now returns every theme OVV publishes. There is no theme marker in a
# detail page's markup — aviation and shipping pages are byte-identical in every
# attribute checked on 2026-09-04 — so the split has to be made on the slug.
# These are OVV's own non-aviation themes plus the bundled quarterly stubs the
# pipeline already skipped when the server filter still worked.
# Checked FIRST: a slug naming an aircraft, an airport or a flight phase is
# aviation even when it also contains a stop word. Without this,
# "passengers-battery-charger-caught-fire-airbus-a320-amsterdam-airport-schiphol"
# was dropped on the word "fire".
_AVIATION_RE = re.compile(
    r"aircraft|airbus|boeing|cessna|piper|embraer|fokker|helicopter|heli"
    r"|glider|balloon|drone|airport|airfield|aerodrome|runway|taxiway"
    r"|takeoff|take-off|landing|airprox|tailstrike|schiphol|eindhoven|lelystad"
    r"|rotterdam-the-hague|maastricht|groningen-airport|flight|aviation",
    re.IGNORECASE,
)

_NON_AVIATION_RE = re.compile(
    r"shipping|maritime|vessel|container|rail|railway|road|traffic|tunnel"
    r"|health|healthcare|hospital|care|defen[cs]e|digital|cyber|construction"
    r"|fire|chemical|industry|gas|pipeline|rainfall|flood|water|energy"
    r"",
    re.IGNORECASE,
)


# Aviation evidence in a report's own text, for build(). The listing has been
# mixed-theme since the 2026-08 redesign, and the 09-06 discover let 91
# non-aviation reports through to the site (a manure silo, a fast ferry, a
# bridge, COVID-19). A slug cannot settle it either way: 260 of the 443 built
# reports carry no aviation word in the slug (glider types, "ls-4-ph-1219").
# "pilot" is left out on purpose: a maritime pilot is the most common
# aviation-sounding word in OVV's shipping reports.
_AVIATION_TEXT_RE = re.compile(
    r"\b(aircraft|aeroplanes?|airplanes?|airliners?|helicopters?|gliders?|cockpit|runways?"
    r"|airports?|aerodromes?|airfields?|air traffic|flight crew|take-?off|airspace|drones?"
    r"|balloons?|ballon\w*|paraglid\w*|paramotor\w*|parachut\w*"
    r"|vliegtuig\w*|luchtvaartuig\w*|landingsbaan|luchthaven|helikopter\w*|zweefvliegtuig\w*"
    r"|luchtverkeersleid\w*|luchtruim)\b",
    re.IGNORECASE,
)
# Calibrated 2026-09-30 on ovv.db: all 344 reports of the aviation-filtered
# 06-04 discover pass; of the 09-06 batch it keeps the 5 aviation ones and 3
# edge cases (see the cleanup notes), and drops the other 91.
AVIATION_MIN_HITS = 5


def looks_like_aviation(slug, title, text):
    """True when the report's text is about aviation (see _AVIATION_TEXT_RE)."""
    hits = len(_AVIATION_TEXT_RE.findall(f"{title or ''} {(text or '')[:20000]}"))
    if hits >= AVIATION_MIN_HITS:
        return True
    return hits >= 1 and bool(_AVIATION_RE.search(slug or ""))


def parse_listing(html):
    """One listing page → ordered unique detail dicts {url, slug}."""
    out = []
    seen = set()
    for m in _DETAIL_RE.finditer(html or ""):
        url, slug = m.group(1).rstrip("/") + "/", m.group(2)
        if slug in seen:
            continue
        if slug.startswith("quarterly-aviation-report"):
            continue          # bundled multi-occurrence stubs, skipped by design
        if not _AVIATION_RE.search(slug) and _NON_AVIATION_RE.search(slug):
            continue
        seen.add(slug)
        out.append({"url": url, "slug": slug})
    return out


def is_noise_doc(href):
    """True when this document is a part of an investigation, not its report."""
    name = (href or "").rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(_NOISE_DOC_RE.search(name))


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


# onderzoeksraad.nl sits behind a Varnish bot check (seen 2026-09-30, likely
# the cause of the weekly discover failures since late August): a browser-like
# client gets a 200 "One moment... Checking your browser" page, header
# x-hv-flag: challenged, whose script fetches /__hv_token and reloads. That
# request sets an hv_pass cookie, which the httpx client keeps for the rest of
# the run. Without this step the listing read as zero links.
HV_TOKEN_URL = BASE + "/__hv_token"


def _challenged(resp):
    headers = getattr(resp, "headers", None) or {}
    if headers.get("x-hv-flag") == "challenged":
        return True
    text = getattr(resp, "text", "") or ""
    return len(text) < 5000 and "/__hv_token" in text


def _get(client, url, **kwargs):
    resp = client.get(url, **kwargs)
    if not _challenged(resp):
        return resp
    client.get(HV_TOKEN_URL)
    resp = client.get(url, **kwargs)
    if _challenged(resp):
        raise RuntimeError(f"[ovv] bot check not cleared by {HV_TOKEN_URL} for {url}")
    return resp


def fetch_listing_page(client, page):
    # The 2026-08 redesign killed the server-side aviation filter: the old
    # ?_aviation_tax=uncategorized now returns a page with ZERO investigation
    # links, which discover() correctly refuses to read as "no reports" (that
    # is why the weekly unit has been failing since 2026-08-30). Verified
    # 2026-09-04: with the parameter 0 links, without it 10 per page, and the
    # pages differ, so pagination still works. /en/thema/aviation/ does list
    # only aviation but serves the same 8 items for every page — its paging is
    # client-side, so it cannot replace the walk.
    #
    # Consequence to keep in mind: the listing is now MIXED-THEME (shipping,
    # rail, health), so aviation has to be told apart downstream rather than by
    # the server.
    resp = _get(client, LISTING_URL, params={"_page": page})
    resp.raise_for_status()
    return resp.text


def fetch_page(client, url):
    resp = _get(client, url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = _get(client, url)  # follows the -pdf/ → uploads 301
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
