# pkbwl_ingest/pkbwl.py
"""
PKBWL Poland (Państwowa Komisja Badania Wypadków Lotniczych, the State
Commission on Aircraft Accidents Investigation) accident-report listing +
per-report metadata/PDF parser.

⚠️ CANONICAL DOMAIN: https://pkbwl.gov.pl/ (the commission's own WordPress
site). NEVER use gov.pl/web/pkbwl — that alias 301-redirects datacenter IPs to
the gov.pl root (a bot trap). All URLs here are built on pkbwl.gov.pl.

LISTING (walked, never guessed — numbering is NOT contiguous):
    https://pkbwl.gov.pl/raporty/            (page 1)
    https://pkbwl.gov.pl/raporty/page/N/     (page N>=2)
10 reports/page, ~236 pages, 2012-2026. Past-the-end page = HTTP 404 (clean
stop). The /page/1/ -> /raporty/ 301 is normal. WP REST API + sitemap are 404
(disabled). Report slugs are extracted from the listing HTML via the regex
/raporty/(YYYY-NNNN)/. The /rejestr-zdarzen/ register is AJAX-only and ignored.

DETAIL PAGE  https://pkbwl.gov.pl/raporty/{YYYY-NNNN}/ :
    Bilingual PL/EN structured metadata INLINE as <dl> blocks — each block has
    two <dt> (Polish label, then grey English label) and a <dd> value:
        Aircraft Type / Aircraft Registration Marks (e.g. SP-NHM, SE-EOT) /
        Occurrence Place / Occurrence Date (already ISO YYYY-MM-DD) /
        Occurrence Time (LMT) / Occurrence Class (WYPADEK/ACCIDENT,
        POWAŻNY INCYDENT/SERIOUS INCIDENT, …) / Aircraft Operator/User /
        MTOW / Injury Level / Investigation Status.
    Registration / date / class / operator all come from HERE — no PDF parse
    needed for metadata.

    The DOCUMENTS (DOKUMENTY/RECORDS) rows are ALSO <dl> blocks whose <dd>
    carries the narrative PDF <a href>(s) under /wp-content/uploads/YYYY/MM/:
        Raport Wstępny / Preliminary Report   (filename suffix _RW)
        Oświadczenie Tymczasowe / Interim Statement (_OT)
        Raport Końcowy / Final Report         (_RK  ← canonical narrative)
        Uchwała / Resolution                  (_U / _U2)
    Each row may carry a PL and an EN file (_EN / _ENG / _RW_ENG). ⚠️ Hrefs are
    HARVESTED from the page, never constructed (filename prefix/suffix order
    drifts: 2019_1816_RK.pdf, 2018-0503_U_ENG.pdf, U_2020_3931.pdf, …).

PDF PREFERENCE (per report we keep ONE narrative href + its lang):
    Prefer the Final report (_RK); fall back interim/preliminary/resolution.
    Within the chosen report type prefer the ENGLISH variant — but ⚠️ some EN
    PDFs render with letter-spacing ("P R E L IM IN A RY") that wrecks
    extraction; the fetch stage sanity-checks single-char-token density and
    falls back to the Polish file when the EN text is degenerate. The stored
    lang reflects the variant whose text we actually kept.

case_id = slug = 'YYYY-NNNN' (stable, unique everywhere).
"""
import html as _html
import re
from urllib.parse import urljoin

BASE = "https://pkbwl.gov.pl"
LISTING = "https://pkbwl.gov.pl/raporty/"
DELAY = 1.2  # biggest source of the wave → be polite

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Report slug on the listing / in a detail URL.
_SLUG_RE = re.compile(r"/raporty/(\d{4}-\d{3,4})/")
# A <dl> block on the detail page (metadata or a documents row).
_DL_RE = re.compile(r"(?is)<dl\b[^>]*>(.*?)</dl>")
_DT_RE = re.compile(r"(?is)<dt\b[^>]*>(.*?)</dt>")
_DD_RE = re.compile(r"(?is)<dd\b[^>]*>(.*?)</dd>")
_PDF_HREF_RE = re.compile(r'href="([^"]+?\.pdf)"', re.IGNORECASE)
_NONSLUG = re.compile(r"[^a-z0-9]+")

# Map the English DOCUMENTS-row label → (report_type, suffix-rank). Higher rank
# wins when several rows carry a PDF; Final is canonical.
_DOC_LABELS = {
    "final report": ("Final", 1000),
    "interim statement": ("Interim", 500),
    "preliminary report": ("Preliminary", 300),
    "resolution": ("Resolution", 100),
}
# Density threshold above which an EN PDF's text is considered degenerate
# (letter-spaced) and we fall back to the Polish variant. Clean PL/EN text
# layers sit at ~0.04-0.15 single-char-token fraction; a spaced-letter render
# pushes most tokens to length 1.
SPACED_SINGLE_FRAC = 0.40


def _strip(fragment):
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = _html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def listing_url(page):
    """Listing URL for page N (page 1 is the bare /raporty/)."""
    return LISTING if page <= 1 else f"{LISTING}page/{page}/"


def extract_slugs(listing_html):
    """Report slugs (YYYY-NNNN) on a listing page, in document order, deduped."""
    out = []
    seen = set()
    for m in _SLUG_RE.finditer(listing_html or ""):
        slug = m.group(1)
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def detail_url(slug):
    return f"{BASE}/raporty/{slug}/"


def _dl_blocks(detail_html):
    """Yield (english_label, value_text, raw_dd_html) for each <dl> block."""
    for m in _DL_RE.finditer(detail_html or ""):
        block = m.group(1)
        dts = [_strip(x) for x in _DT_RE.findall(block)]
        dds = _DD_RE.findall(block)
        en_label = dts[-1].lower() if dts else ""
        value = _strip(" ".join(dds))
        raw_dd = " ".join(dds)
        yield en_label, value, raw_dd


def _norm_value(v):
    """A '-' or empty placeholder value becomes None."""
    v = (v or "").strip()
    return None if v in ("", "-", "–") else v


def parse_detail(detail_html, slug):
    """
    Parse a report detail page → metadata dict + list of document PDF candidates.

    Returns dict:
        case_id, event_date (ISO|None), aircraft, registration, operator,
        location, occurrence_class, injury_level, investigation_status,
        documents = [ {report_type, rank, pdfs=[(url, lang), …]}, … ]
    `pdfs` lang is 'en' for _EN/_ENG filenames, else 'pl'. The pipeline picks
    one narrative href across documents (highest rank, EN preferred, with a
    spaced-letter fallback applied at fetch time).
    """
    meta = {
        "case_id": slug,
        "event_date": None,
        "aircraft": None,
        "registration": None,
        "operator": None,
        "location": None,
        "occurrence_class": None,
        "injury_level": None,
        "investigation_status": None,
        "documents": [],
    }
    for en_label, value, raw_dd in _dl_blocks(detail_html):
        if en_label == "occurrence date":
            meta["event_date"] = normalize_date(value)
        elif en_label == "aircraft type":
            meta["aircraft"] = _norm_value(value)
        elif en_label == "aircraft registration marks":
            meta["registration"] = _norm_value(value)
        elif en_label == "aircraft operator/user":
            meta["operator"] = _norm_value(value)
        elif en_label == "occurrence place":
            meta["location"] = _norm_value(value)
        elif en_label == "occurrence class":
            meta["occurrence_class"] = _norm_value(value)
        elif en_label == "injury level":
            meta["injury_level"] = _norm_value(value)
        elif en_label == "investigation status":
            meta["investigation_status"] = _norm_value(value)
        elif en_label in _DOC_LABELS:
            report_type, rank = _DOC_LABELS[en_label]
            pdfs = []
            for raw in _PDF_HREF_RE.findall(raw_dd):
                href = _html.unescape(raw)
                url = urljoin(BASE, href)
                pdfs.append((url, pdf_lang(url)))
            if pdfs:
                meta["documents"].append(
                    {"report_type": report_type, "rank": rank, "pdfs": pdfs}
                )
    return meta


def pdf_lang(url_or_name):
    """'en' for an English-variant filename (_EN/_ENG), else 'pl'."""
    name = (url_or_name or "").rsplit("/", 1)[-1].lower()
    if re.search(r"_(en|eng)(?:[._-]|$)", name):
        return "en"
    return "pl"


def normalize_date(value):
    """
    PKBWL dates are already ISO (YYYY-MM-DD); return that, else None for a
    '-' / empty / unparseable cell.
    """
    if not value:
        return None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", value)
    return m.group(0) if m else None


def pick_narrative(documents):
    """
    Choose the preferred narrative PDF across the report's document rows.
    Final (RK) > Interim > Preliminary > Resolution; within the winning report
    type, the ENGLISH variant is preferred (the fetch stage may still fall back
    to PL on degenerate EN text). Returns (url, lang, report_type) or None.
    """
    if not documents:
        return None
    best_doc = max(documents, key=lambda d: d["rank"])
    pdfs = best_doc["pdfs"]
    if not pdfs:
        return None
    # EN preferred within the chosen report type.
    chosen = next((p for p in pdfs if p[1] == "en"), pdfs[0])
    return chosen[0], chosen[1], best_doc["report_type"]


def pl_fallback(documents, report_type):
    """The Polish PDF of the same report type, if any (used on degenerate EN)."""
    for d in documents:
        if d["report_type"] == report_type:
            for url, lang in d["pdfs"]:
                if lang == "pl":
                    return url
    return None


def single_char_fraction(text):
    """Fraction of whitespace-split tokens that are a single character."""
    toks = (text or "").split()
    if not toks:
        return 1.0
    single = sum(1 for t in toks if len(t) == 1)
    return single / len(toks)


def is_degenerate(text, floor=300):
    """
    True when extracted text looks like a letter-spaced render (e.g. EN PDFs
    that come out 'P R E L IM IN A RY'): too short OR too many 1-char tokens.
    """
    if len(text or "") < floor:
        return True
    return single_char_fraction(text) >= SPACED_SINGLE_FRAC


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing(client, page):
    """
    GET a listing page. Returns (status_code, html). A 404 means we walked past
    the last page (clean stop) — caller stops the walk.
    """
    resp = client.get(listing_url(page))
    if resp.status_code == 404:
        return 404, ""
    resp.raise_for_status()
    return resp.status_code, resp.text


def fetch_page(client, url):
    resp = client.get(url)
    resp.raise_for_status()
    return resp.text


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
