# ntsbcarol_ingest/carol.py
"""CAROL (data.ntsb.gov) API client for ntsbcarol-ingest.

Two API layers:
  1. data.ntsb.gov/carol-main-public — older public REST API (no auth key needed)
     Session/CreateSession  → session_id
     Query/Main             → paginated search with filter rules
  2. www.ntsb.gov/investigations/AccidentReports/Reports/{report_no}.pdf
     Direct PDF download (no auth, public NTSB domain).

Board-level aviation cases are identified by:
  - NtsbNo prefix = DCA, ANC, ERA, or CEN (HQ-assigned office codes)
  - Mode = Aviation
  - ReportType = Report  (published final report exists)
  - EV_ID = ""           (not in avall.mdb, confirmed by query result)

The full corpus is ~268 completed cases (as of 2026-06-11).
"""
import json
import time
import urllib.error
import urllib.request

# ── constants ───────────────────────────────────────────────────────────────

BASE = "https://data.ntsb.gov/carol-main-public/api"
PDF_BASE = "https://www.ntsb.gov/investigations/AccidentReports/Reports"
UA = "Mozilla/5.0 (compatible; ntsbcarol-ingest/1.0; research)"
DELAY = 1.5           # polite inter-request delay (seconds)
PAGE_SIZE = 50        # max rows per API call (tested up to 50)

# Prefixes that identify NTSB HQ board-level aviation investigations
BOARD_PREFIXES = ["DCA", "ANC", "ERA", "CEN"]


# ── low-level helpers ────────────────────────────────────────────────────────

def _json_post(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _json_get(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ── session ──────────────────────────────────────────────────────────────────

def create_session():
    """Return a CAROL session ID (integer).  No auth key needed."""
    result = _json_post(f"{BASE}/Session/CreateSession", {})
    return result  # raw integer


# ── query ────────────────────────────────────────────────────────────────────

def _query_rule(field, column, value, operator="search engine"):
    return {"FieldName": field, "RuleType": 0, "Values": [value], "Columns": [column], "Operator": operator}


def _query_rule_starts_with(field, column, value):
    return _query_rule(field, column, value, operator="starts with")


def enumerate_board_cases(session_id, prefix, offset=0, page_size=PAGE_SIZE):
    """Return one page of board-level Aviation+Report cases for given prefix.

    Returns dict with keys: Results (list), ResultListCount (int), MaxResultCountReached (bool).
    """
    payload = {
        "ResultSetSize": page_size,
        "ResultSetOffset": offset,
        "QueryGroups": [
            {
                "QueryRules": [
                    _query_rule("Mode", "Event.Mode", "Aviation"),
                    _query_rule_starts_with("NtsbNo", "Event.NTSBNumber", prefix),
                    _query_rule("ReportType", "Event.ReportType", "Report"),
                ],
                "AndOr": "And",
            }
        ],
        "TargetCollection": "cases",
        "AndOr": "and",
        "SortColumn": None,
        "SortDescending": True,
        "SessionId": session_id,
    }
    return _json_post(f"{BASE}/Query/Main", payload)


def get_all_board_cases(session_id, prefixes=BOARD_PREFIXES, progress_cb=None):
    """Page through CAROL and return all board-level Aviation+Report cases.

    Yields dicts (one per CAROL result row) with standardised keys.
    """
    for prefix in prefixes:
        offset = 0
        while True:
            result = enumerate_board_cases(session_id, prefix, offset=offset)
            rows = result.get("Results", [])
            total = result.get("ResultListCount", 0)
            if progress_cb:
                progress_cb(prefix, offset, len(rows), total)
            for row in rows:
                yield _parse_row(row)
            offset += len(rows)
            if not rows or offset >= total:
                break
            time.sleep(DELAY)


def _parse_row(row):
    """Flatten a CAROL result row to a plain dict."""
    fmap = {}
    for f in row.get("Fields", []):
        vals = f.get("Values", [])
        fmap[f["FieldName"]] = vals[0] if vals else ""
    return {
        "ntsb_no": fmap.get("NtsbNo", ""),
        "mkey": fmap.get("Mkey", ""),
        "event_date_raw": fmap.get("EventDate", ""),
        "city": fmap.get("City", ""),
        "state": fmap.get("State", ""),
        "country_name": fmap.get("Country", "United States"),
        "registration": fmap.get("N#", ""),
        "make": fmap.get("VehicleMake", ""),
        "model": fmap.get("VehicleModel", ""),
        "event_type": fmap.get("EventType", ""),
        "highest_injury": fmap.get("HighestInjuryLevel", ""),
        "fatalities_str": fmap.get("InjuryOnboardCount", ""),
        "report_no": fmap.get("ReportNo", "") or fmap.get("ReportNumber", ""),
        "completion_status": fmap.get("CompletionStatus", ""),
        "most_recent_report_type": fmap.get("MostRecentReportType", ""),
        "ev_id": fmap.get("EV_ID", ""),
        "entry_id": row.get("EntryId", ""),
    }


# ── date parsing ─────────────────────────────────────────────────────────────

def parse_event_date(raw):
    """Convert CAROL EventDate (ISO 8601 with Z) to YYYY-MM-DD string."""
    if not raw:
        return None
    # CAROL returns: "2019-12-26T17:57:00Z"
    return raw[:10]


# ── PDF URL ──────────────────────────────────────────────────────────────────

def pdf_url(report_no):
    """Return the direct PDF URL for a report number, or None."""
    if not report_no:
        return None
    return f"{PDF_BASE}/{report_no}.pdf"


def check_pdf_exists(report_no, timeout=20):
    """HEAD-check the PDF URL.  Returns (url, size_bytes or None)."""
    url = pdf_url(report_no)
    if not url:
        return None, None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            size = r.headers.get("Content-Length")
            return url, int(size) if size else None
    except Exception:
        return url, None


def download_pdf(report_no, dest_path, timeout=60):
    """Download the PDF to dest_path.  Returns True on success."""
    url = pdf_url(report_no)
    if not url:
        return False
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        with open(dest_path, "wb") as fh:
            fh.write(r.read())
    return True


# ── slug helper ──────────────────────────────────────────────────────────────

def case_id(ntsb_no):
    """Return the canonical case_id for a CAROL board case."""
    return f"ntsbcarol-{ntsb_no.lower()}"
