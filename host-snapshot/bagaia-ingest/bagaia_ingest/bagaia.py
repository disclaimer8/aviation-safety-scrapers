# bagaia_ingest/bagaia.py
"""BAGAIA (Banjul Accord Group Accident Investigation Agency) ingest.

BAGAIA is a regional accident investigation authority for West/Central Africa
(similar to ECCAA for the Caribbean).  It investigates on behalf of member
states that lack the capacity, or when the state of occurrence delegates to
BAGAIA (as happened with São Tomé & Príncipe for the UR-CKC event).

Member states: Gambia, Guinea, Sierra Leone, Liberia, Ghana, Nigeria,
Cape Verde (most member-state reports go to their own sources: nsib, agacgn,
ghana, etc.).

This ingest covers reports investigated BY BAGAIA itself (not by a member-state
agency on behalf of BAGAIA), where no individual member-state source already
covers the event.

Known reports from BAGAIA itself as of 2026-06-11:
  1. UR-CKC  AN-74TK-100  CAVOK Air  São Tomé 2017-07-29
     PDF: https://nsib.gov.ng/wp-content/uploads/ninja-forms/3/CVK/2017/07/29/F.pdf
     (hosted by NSIB Nigeria; NSIB conducted the investigation on BAGAIA's behalf)
     country: ST (São Tomé and Príncipe, state of occurrence)
     case_id: bagaia-ur-ckc-2017

All case_ids are INTRINSIC and prefixed `bagaia-`.

BAGAIA dashboard API (DataTables server-side, JSON):
  GET https://dashboard.bagaia.org/publications-report
  Params: draw=1&start=0&length=200&Accept: application/json
  X-Requested-With: XMLHttpRequest
  Returns: {recordsTotal, data: [{country, date, registration_number,
            aircraft_type, aircraft_operator, occurence, report_link}, ...]}

The dashboard mixes BAGAIA-investigated events with member-state events.
Only entries whose country is NOT a major member state with its own source
(Nigeria→nsib, Ghana→ghana, Guinea→agacgn, Cape Verde→ipiaam) are candidates
for bagaia-only ingestion.  As of the initial survey (2026-06-11), UR-CKC is
the sole BAGAIA-specific candidate.
"""
import re

BAGAIA_DASHBOARD_URL = "https://dashboard.bagaia.org/publications-report"
DELAY = 2.0

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}

# Member-state sources that have their own ingest pipelines.
# Reports attributed to these countries in the dashboard are covered elsewhere.
MEMBER_STATE_COVERED = {"Nigeria", "Ghana", "Guinea", "Cape Verde"}

# ──────────────────────────────────────────────
# Static seed — BAGAIA-investigated events
# ──────────────────────────────────────────────

#: These rows are inserted by seed() and not re-discovered by discover().
SEED_REPORTS = [
    {
        "case_id": "bagaia-ur-ckc-2017",
        "pdf_url": (
            "https://nsib.gov.ng/wp-content/uploads/"
            "ninja-forms/3/CVK/2017/07/29/F.pdf"
        ),
        "report_url": (
            "https://nsib.gov.ng/wp-content/uploads/"
            "ninja-forms/3/CVK/2017/07/29/F.pdf"
        ),
        "title": (
            "BAGAIA Aircraft Accident Report CVK/2017/07/29/F — "
            "AN-74TK-100 UR-CKC CAVOK Air, São Tomé 2017-07-29"
        ),
        "event_class": "Accident",
        "aircraft": "AN-74TK-100",
        "registration": "UR-CKC",
        "operator": "CAVOK Airlines",
        "date_of_occurrence": "2017-07-29",
        "location": "São Tomé International Airport, São Tomé",
        "country": "ST",
        "lang": "en",
    },
]

# ──────────────────────────────────────────────
# Dashboard helpers
# ──────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_NONSLUG = re.compile(r"[^a-z0-9]+")


def _normalise_date(s):
    """DD/MM/YYYY → YYYY-MM-DD, or None on failure."""
    m = _DATE_RE.match((s or "").strip())
    if not m:
        return None
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def _slugify(s):
    return _NONSLUG.sub("-", (s or "").lower()).strip("-")


def _make_case_id(registration, date_iso):
    """bagaia-<reg-normalised>-<YYYY>  e.g. bagaia-ur-ckc-2017."""
    reg = _slugify((registration or "").replace(" ", ""))
    year = (date_iso or "")[:4]
    parts = ["bagaia"]
    if reg:
        parts.append(reg)
    if year:
        parts.append(year)
    return "-".join(parts)


def fetch_dashboard(client):
    """GET the BAGAIA dashboard DataTables API.  Returns list of raw rows."""
    resp = client.get(
        BAGAIA_DASHBOARD_URL,
        params={"draw": "1", "start": "0", "length": "200"},
        headers=HEADERS,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", [])


def dashboard_candidates(rows):
    """Filter dashboard rows to BAGAIA-specific candidates (not member-state).

    Returns list of dicts with normalised fields.  Rows whose country is in
    MEMBER_STATE_COVERED are skipped.  Rows without a report_link (no PDF) are
    skipped.
    """
    candidates = []
    for r in rows:
        country = (r.get("country") or "").strip()
        if country in MEMBER_STATE_COVERED:
            continue
        link = (r.get("report_link") or "").strip()
        if not link:
            continue
        date_iso = _normalise_date(r.get("date", ""))
        reg = (r.get("registration_number") or "").strip().replace(" ", "")
        candidates.append({
            "case_id": _make_case_id(reg, date_iso),
            "pdf_url": link,
            "report_url": link,
            "title": (
                f"{country} {r.get('aircraft_operator','')} "
                f"{r.get('aircraft_type','')} {reg}"
            ).strip(),
            "event_class": (r.get("occurence") or "Accident").capitalize(),
            "aircraft": (r.get("aircraft_type") or "").strip() or None,
            "registration": reg or None,
            "operator": (r.get("aircraft_operator") or "").strip() or None,
            "date_of_occurrence": date_iso,
            "location": None,
            "country": None,  # unknown ISO from dashboard
            "lang": "en",
        })
    return candidates


def make_client(proxy=None):
    import httpx
    transport = None
    if proxy:
        transport = httpx.HTTPTransport(proxy=proxy)
    return httpx.Client(
        headers=HEADERS,
        follow_redirects=True,
        timeout=60.0,
        transport=transport,
    )
