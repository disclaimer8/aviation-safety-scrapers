# sust_ingest/sust.py
"""
SUST / STSB Switzerland (Schweizerische Sicherheitsuntersuchungsstelle /
Swiss Transportation Safety Investigation Board) aviation report enumeration
and metadata parser — www.sust.admin.ch.

⚠️ The bare domain (sust.admin.ch, no www) FAILS DNS — always use www.

The listing is a TWO-STEP JS-backend flow (NEVER scrape the rendered table —
it is JS-injected and virtualized):

  Step 1 — skeleton (ONE GET): the reports-aviation listAvExamination action
  returns an HTML page whose <tbody> holds ~3,016
      <tr data-append-loaded="{uid}" data-lazyload="{getEntry URL w/ baked cHash}">
  rows.  parse_skeleton() returns [(uid, lazyload_url), …].  ⚠️ Re-fetch the
  skeleton each run — the per-row cHashes are baked there; NEVER forge cHashes.

  Step 2 — per row (getEntry JSON):
      {uid, date "04. 05. 2026", place, canton ("TI" / foreign-location text /
       absent), aircrafts:[{matriculation, manufacturer, type, category,
       flightcategory, flightrules}], documents:[{name "Schlussbericht"/
       "Vorbericht"/"Summarischer Bericht"/"Faktenbericht"/"Notification"
       (LOCALIZED de/fr/it/en), url "/inhalte/AV-berichte/HB-ZEJ_VB_I.pdf",
       extension, size, releasedate}]}

⚠️ NEVER construct PDF URLs.  Old era = numeric /inhalte/AV-berichte/1.pdf;
new = {REG}_{TYPE}_{LANG}.pdf.  Use documents[].url verbatim, absolute-ified
against BASE.

Document preference (best first):
    Schlussbericht > Summarischer Bericht > Faktenbericht > Vorbericht >
    Notification
keyed on the localized name AND the filename code (_FB_=final, _VB_=prelim).

Language from filename suffix _D/_F/_I/_E (CASE-INSENSITIVE) → de/fr/it/en;
numeric old filenames carry no suffix → 'de' default.  PDFs are all
text-layer (incl. 1959 — no OCR).

case_id = str(uid) — NUMERIC (1..3844).  Prod slug-prefixing is Phase 2.
"""
import html
import re
from urllib.parse import urljoin

BASE = "https://www.sust.admin.ch"
# Step-1 skeleton: listAvExamination action (cHash is the site's own; it is the
# stable entry point — re-fetched each run so its baked per-row cHashes are live)
SKELETON_URL = (
    BASE + "/en/reports/reports-aviation"
    "?tx_sustemas_listavexamination%5Baction%5D=listavexamination"
    "&tx_sustemas_listavexamination%5Bcontroller%5D=Examination"
    "&cHash=0f8ba03cb59aeea9dde21c25567865c1"
)
DELAY = 1.75  # admin.ch government host — polite

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,de;q=0.9,fr;q=0.8,it;q=0.7",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Referer": BASE + "/en/home.html",
}

# <tr ... data-append-loaded="3844" data-lazyload="…getEntry…">
_ROW_RE = re.compile(
    r'data-append-loaded="(\d+)"\s+data-lazyload="([^"]+)"'
)

# document-name → (rank, kind) — lower rank = MORE preferred.  Names are
# localized; match on substrings across de/fr/it/en spellings.
_DOC_RANKS = (
    (re.compile(r"schluss|final|finale", re.I), 0, "Final"),
    (re.compile(r"summar", re.I), 1, "Summary"),
    (re.compile(r"fakten|fact|fait", re.I), 2, "Factual"),
    (re.compile(r"vorbericht|prelimin|préliminaire|preliminare", re.I), 3, "Preliminary"),
    (re.compile(r"notification|meldung", re.I), 4, "Notification"),
)
# filename code → rank/kind (authoritative tie-breaker; _FB_=final, _VB_=prelim)
_FILE_CODE_RANKS = (
    (re.compile(r"_SB_", re.I), 0, "Final"),   # Schlussbericht
    (re.compile(r"_FB_", re.I), 0, "Final"),   # Final report (numeric/new)
    (re.compile(r"_SUB_", re.I), 1, "Summary"),
    (re.compile(r"_FAB_", re.I), 2, "Factual"),
    (re.compile(r"_VB_", re.I), 3, "Preliminary"),
)
_LANG_RE = re.compile(r"_([DFIE])\.pdf$", re.I)
_LANG_MAP = {"d": "de", "f": "fr", "i": "it", "e": "en"}

_DATE_RE = re.compile(r"^\s*(\d{2})\.\s*(\d{2})\.\s*(\d{4})\s*$")


def parse_skeleton(html_text):
    """
    Step 1: parse the skeleton HTML → [(uid:int, lazyload_url:str), …].
    Entity-decodes the data-lazyload value (it carries &amp;).
    """
    out = []
    for uid, raw in _ROW_RE.findall(html_text or ""):
        out.append((int(uid), html.unescape(raw)))
    return out


def absolute_url(url):
    """Absolute-ify a relative SUST URL against BASE (verbatim if already abs)."""
    if not url:
        return None
    return urljoin(BASE + "/", url)


def parse_date(raw):
    """'DD. MM. YYYY' (spaces) → 'YYYY-MM-DD'; None on unparseable."""
    if not raw:
        return None
    m = _DATE_RE.match(raw)
    if not m:
        return None
    dd, mm, yyyy = m.groups()
    return f"{yyyy}-{mm}-{dd}"


def lang_from_filename(url):
    """Filename suffix _D/_F/_I/_E (case-insensitive) → de/fr/it/en.
    Numeric/old filenames carry no suffix → 'de' default."""
    if not url:
        return "de"
    m = _LANG_RE.search(url)
    if m:
        return _LANG_MAP[m.group(1).lower()]
    return "de"


def _doc_rank(doc):
    """(rank, kind) for a document, lower = more preferred.
    Filename code is authoritative; falls back to localized name."""
    url = doc.get("url") or ""
    for rx, rank, kind in _FILE_CODE_RANKS:
        if rx.search(url):
            return rank, kind
    name = doc.get("name") or ""
    for rx, rank, kind in _DOC_RANKS:
        if rx.search(name):
            return rank, kind
    return 99, name or "Report"


def pick_document(documents):
    """
    Choose the best document by preference
    (Schlussbericht > Summarischer > Faktenbericht > Vorbericht > Notification).
    Returns dict {url, name, kind, lang} or None when there are no documents.
    """
    if not documents:
        return None
    best = None
    best_rank = None
    for doc in documents:
        if not (doc.get("url")):
            continue
        rank, kind = _doc_rank(doc)
        if best is None or rank < best_rank:
            best, best_rank, best_kind = doc, rank, kind
    if best is None:
        return None
    return {
        "url": absolute_url(best["url"]),
        "name": best.get("name"),
        "kind": best_kind,
        "lang": lang_from_filename(best.get("url")),
    }


def parse_entry(data):
    """
    Step 2: getEntry JSON → flat metadata dict.
        case_id, date_of_occurrence, location, registration, aircraft,
        operator (always None — SUST JSON carries no operator),
        occurrence_type (None), doc (pick_document result or None)
    """
    out = {
        "case_id": str(data.get("uid")) if data.get("uid") is not None else None,
        "date_of_occurrence": parse_date(data.get("date")),
        "location": None,
        "registration": None,
        "aircraft": None,
        "operator": None,
        "occurrence_type": None,
        "doc": None,
    }

    place = (data.get("place") or "").strip()
    canton = (data.get("canton") or "").strip()
    out["location"] = ", ".join(p for p in (place, canton) if p) or None

    aircrafts = data.get("aircrafts") or []
    if aircrafts:
        a = aircrafts[0]
        out["registration"] = (a.get("matriculation") or "").strip() or None
        manuf = (a.get("manufacturer") or "").strip()
        typ = (a.get("type") or "").strip()
        out["aircraft"] = " ".join(p for p in (manuf, typ) if p) or None
        out["occurrence_type"] = (a.get("category") or "").strip() or None

    out["doc"] = pick_document(data.get("documents"))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_skeleton(client):
    """Step 1: GET the skeleton → [(uid, lazyload_url), …]."""
    resp = client.get(SKELETON_URL)
    resp.raise_for_status()
    return parse_skeleton(resp.text)


def fetch_entry(client, lazyload_url):
    """Step 2: GET a (relative) getEntry URL → parsed metadata dict."""
    resp = client.get(absolute_url(lazyload_url))
    resp.raise_for_status()
    return parse_entry(resp.json())


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
