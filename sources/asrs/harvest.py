#!/usr/bin/env python3
"""
ASRS NASA Voluntary Incident Reports Harvester
https://akama.arc.nasa.gov/ASRSDBOnline/

Entry point: QueryWizard_Begin.aspx (establishes ASP.NET session)
Then: QueryWizard_Filter.aspx (postback wizard)

Flow:
  1. GET QueryWizard_Begin.aspx (sets ASP.NET_SessionId cookie)
  2. GET QueryWizard_Filter.aspx (get initial VIEWSTATE)
  3. POST QueryWizard_Filter.aspx with name="2" (click Date of Incident filter)
  4. GET QueryWizard_DatePopup.aspx?statementId=2&statementType=Filter (get popup VIEWSTATE)
  5. POST QueryWizard_DatePopup.aspx with DropDownList1-4 + SaveButton (stores dates in session)
  6. POST QueryWizard_Filter.aspx with _ctl6 (Run Search → redirects to Results page)
  7. GET QueryWizard_ExportExcel.aspx?ExportType=CSV (download CSV)

FROZEN CSV HEADERS (from 2024-05 probe, 126 columns total, 125 named):
  [  0] ACN
  [  1] Date
  [  2] Local Time Of Day
  [  3] Locale Reference
  [  4] State Reference
  [  5] Relative Position.Angle.Radial
  [  6] Relative Position.Distance.Nautical Miles
  [  7] Altitude.AGL.Single Value
  [  8] Altitude.MSL.Single Value
  [  9] Latitude / Longitude (UAS)
  [ 10] Flight Conditions
  [ 11] Weather Elements / Visibility
  [ 12] Work Environment Factor
  [ 13] Light
  [ 14] Ceiling
  [ 15] RVR.Single Value
  [ 16] ATC / Advisory
  [ 17] Aircraft Operator
  [ 18] Make Model Name
  [ 19] Aircraft Zone
  [ 20] Crew Size
  [ 21] Operating Under FAR Part
  [ 22] Flight Plan
  [ 23] Mission
  [ 24] Nav In Use
  [ 25] Flight Phase
  [ 26] Route In Use
  [ 27] Airspace
  [ 28] Maintenance Status.Maintenance Deferred
  [ 29] Maintenance Status.Records Complete
  [ 30] Maintenance Status.Released For Service
  [ 31] Maintenance Status.Required / Correct Doc On Board
  [ 32] Maintenance Status.Maintenance Type
  [ 33] Maintenance Status.Maintenance Items Involved
  [ 34] Cabin Lighting
  [ 35] Number Of Seats.Number
  [ 36] Passengers On Board.Number
  [ 37] Crew Size Flight Attendant.Number Of Crew
  [ 38] Airspace Authorization Provider (UAS)
  [ 39] Operating Under Waivers / Exemptions / Authorizations (UAS)
  [ 40] Waivers / Exemptions / Authorizations (UAS)
  [ 41] Airworthiness Certification (UAS)
  [ 42] Weight Category (UAS)
  [ 43] Configuration (UAS)
  [ 44] Flight Operated As (UAS)
  [ 45] Flight Operated with Visual Observer (UAS)
  [ 46] Control Mode (UAS)
  [ 47] Flying In / Near / Over (UAS)
  [ 48] Passenger Capable (UAS)
  [ 49] Type (UAS)
  [ 50] Number of UAS Being Controlled (UAS)
  [ 51] Aircraft Component
  [ 52] Manufacturer
  [ 53] Aircraft Reference
  [ 54] Problem
  [ 55] ATC / Advisory  [Aircraft 2]
  [ 56] Aircraft Operator  [Aircraft 2]
  [ 57] Make Model Name  [Aircraft 2]
  [ 58] Aircraft Zone  [Aircraft 2]
  [ 59] Crew Size  [Aircraft 2]
  [ 60] Operating Under FAR Part  [Aircraft 2]
  [ 61] Flight Plan  [Aircraft 2]
  [ 62] Mission  [Aircraft 2]
  [ 63] Nav In Use  [Aircraft 2]
  [ 64] Flight Phase  [Aircraft 2]
  [ 65] Route In Use  [Aircraft 2]
  [ 66] Airspace  [Aircraft 2]
  [ 67] Maintenance Status.Maintenance Deferred  [Aircraft 2]
  [ 68] Maintenance Status.Records Complete  [Aircraft 2]
  [ 69] Maintenance Status.Released For Service  [Aircraft 2]
  [ 70] Maintenance Status.Required / Correct Doc On Board  [Aircraft 2]
  [ 71] Maintenance Status.Maintenance Type  [Aircraft 2]
  [ 72] Maintenance Status.Maintenance Items Involved  [Aircraft 2]
  [ 73] Cabin Lighting  [Aircraft 2]
  [ 74] Number Of Seats.Number  [Aircraft 2]
  [ 75] Passengers On Board.Number  [Aircraft 2]
  [ 76] Crew Size Flight Attendant.Number Of Crew  [Aircraft 2]
  [ 77] Airspace Authorization Provider (UAS)  [Aircraft 2]
  [ 78] Operating Under Waivers / Exemptions / Authorizations (UAS)  [Aircraft 2]
  [ 79] Waivers / Exemptions / Authorizations (UAS)  [Aircraft 2]
  [ 80] Airworthiness Certification (UAS)  [Aircraft 2]
  [ 81] Weight Category (UAS)  [Aircraft 2]
  [ 82] Configuration (UAS)  [Aircraft 2]
  [ 83] Flight Operated As (UAS)  [Aircraft 2]
  [ 84] Flight Operated with Visual Observer (UAS)  [Aircraft 2]
  [ 85] Control Mode (UAS)  [Aircraft 2]
  [ 86] Flying In / Near / Over (UAS)  [Aircraft 2]
  [ 87] Passenger Capable (UAS)  [Aircraft 2]
  [ 88] Type (UAS)  [Aircraft 2]
  [ 89] Number of UAS Being Controlled (UAS)  [Aircraft 2]
  [ 90] Location Of Person  [Person 1]
  [ 91] Location In Aircraft  [Person 1]
  [ 92] Reporter Organization  [Person 1]
  [ 93] Function  [Person 1]
  [ 94] Qualification  [Person 1]
  [ 95] Experience  [Person 1]
  [ 96] Cabin Activity  [Person 1]
  [ 97] Human Factors  [Person 1]
  [ 98] Communication Breakdown  [Person 1]
  [ 99] UAS Communication Breakdown  [Person 1]
  [100] ASRS Report Number.Accession Number  [Person 1]
  [101] Location Of Person  [Person 2]
  [102] Location In Aircraft  [Person 2]
  [103] Reporter Organization  [Person 2]
  [104] Function  [Person 2]
  [105] Qualification  [Person 2]
  [106] Experience  [Person 2]
  [107] Cabin Activity  [Person 2]
  [108] Human Factors  [Person 2]
  [109] Communication Breakdown  [Person 2]
  [110] UAS Communication Breakdown  [Person 2]
  [111] ASRS Report Number.Accession Number  [Person 2]
  [112] Anomaly
  [113] Miss Distance
  [114] Were Passengers Involved In Event
  [115] Detector
  [116] When Detected
  [117] Result
  [118] Contributing Factors / Situations
  [119] Primary Problem
  [120] Narrative  [Report 1]
  [121] Callback  [Report 1]
  [122] Narrative  [Report 2]
  [123] Callback  [Report 2]
  [124] Synopsis
  [125] (trailing empty column)
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
import time
from datetime import datetime

import requests

BASE_URL = "https://akama.arc.nasa.gov/ASRSDBOnline"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
SLEEP_BETWEEN_REQUESTS = 2.0
MAX_RETRY_5XX = 3
BACKOFF_BASE = 30  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]


def _extract_hidden(html):
    """Extract ASP.NET hidden fields from HTML."""
    result = {}
    for field in ("__VIEWSTATE", "__EVENTVALIDATION", "__VIEWSTATEGENERATOR"):
        m = re.search(
            r'name="%s"[^>]+value="([^"]*)"' % re.escape(field), html
        )
        if m:
            result[field] = m.group(1)
    return result


def _safe_get(session, url, **kwargs):
    """GET with retry on 5xx."""
    for attempt in range(MAX_RETRY_5XX):
        r = session.get(url, **kwargs)
        if r.status_code < 500:
            return r
        log.warning("5xx on GET %s (attempt %d): %s", url, attempt + 1, r.status_code)
        time.sleep(BACKOFF_BASE * (attempt + 1))
    raise RuntimeError(f"Repeated 5xx on GET {url}")


def _safe_post(session, url, data, **kwargs):
    """POST with retry on 5xx."""
    for attempt in range(MAX_RETRY_5XX):
        r = session.post(url, data=data, **kwargs)
        if r.status_code < 500:
            return r
        log.warning("5xx on POST %s (attempt %d): %s", url, attempt + 1, r.status_code)
        time.sleep(BACKOFF_BASE * (attempt + 1))
    raise RuntimeError(f"Repeated 5xx on POST {url}")


def _is_session_expired(html):
    """Check if we got redirected to login page."""
    return "session may have timed out" in html.lower() or "login.aspx" in html.lower()


def new_session():
    """Create a new requests.Session with established ASP.NET session."""
    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    begin_url = f"{BASE_URL}/QueryWizard_Begin.aspx"
    filter_url = f"{BASE_URL}/QueryWizard_Filter.aspx"

    log.info("  Establishing ASP.NET session via QueryWizard_Begin.aspx...")
    # GET Begin page - this sets ASP.NET_SessionId and redirects to Filter
    r = session.get(begin_url, allow_redirects=True)
    # Should now be on Filter page
    if "QueryWizard_Filter" not in r.url and "filter" not in r.url.lower():
        # Try directly
        r = session.get(filter_url, allow_redirects=True)
    assert "QueryWizard" in r.text, f"Session init failed, got: {r.text[:200]}"
    log.info("  Session established, cookies: %s", dict(session.cookies))
    time.sleep(SLEEP_BETWEEN_REQUESTS)
    return session, r


def fetch_csv_for_range(year_from, month_from, year_to, month_to):
    """
    Full postback replay to get CSV for the given date range.
    Returns (raw_csv_text, acn_count_from_server).
    Raises RuntimeError if any step fails after retries.
    """
    filter_url = f"{BASE_URL}/QueryWizard_Filter.aspx"
    date_popup_url = f"{BASE_URL}/QueryWizard_DatePopup.aspx?statementId=2&statementType=Filter"
    export_url = f"{BASE_URL}/QueryWizard_ExportExcel.aspx?ExportType=CSV"

    for attempt in range(2):  # retry once on session expiry
        try:
            session, r_filter = new_session()

            # Step 2: GET the filter page fresh (already done in new_session)
            log.info("Step 2: Using filter page from session init (%d bytes)", len(r_filter.content))
            hidden_filter = _extract_hidden(r_filter.text)
            assert hidden_filter.get("__VIEWSTATE"), "No VIEWSTATE on filter page"

            # Step 3: POST to click "Date of Incident" plus button (name="2")
            log.info("Step 3: POST click Date of Incident filter")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            data3 = {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "2.x": "10",
                "2.y": "10",
            }
            data3.update(hidden_filter)
            r3 = _safe_post(session, filter_url, data=data3,
                            headers={"Referer": filter_url})
            assert r3.status_code == 200
            if _is_session_expired(r3.text):
                raise RuntimeError("Session expired after clicking date filter")
            log.info("  After clicking date filter: %d bytes, date filter present: %s",
                     len(r3.content), "Date of Incident" in r3.text)
            hidden_after_click = _extract_hidden(r3.text)

            # Step 4: GET date popup
            log.info("Step 4: GET date popup")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            r4 = _safe_get(session, date_popup_url,
                           headers={"Referer": filter_url})
            assert r4.status_code == 200
            hidden_popup = _extract_hidden(r4.text)
            assert hidden_popup.get("__VIEWSTATE"), "No VIEWSTATE on date popup"
            log.info("  Date popup: %d bytes", len(r4.content))

            # Step 5: POST date popup with date range (stores dates in server session)
            log.info("Step 5: POST date popup %04d-%02d to %04d-%02d",
                     year_from, month_from, year_to, month_to)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            data5 = {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "DropDownList1": str(year_from),
                "DropDownList2": MONTH_NAMES[month_from - 1],
                "DropDownList3": str(year_to),
                "DropDownList4": MONTH_NAMES[month_to - 1],
                "SaveButton.x": "10",
                "SaveButton.y": "10",
            }
            data5.update(hidden_popup)
            r5 = _safe_post(session, date_popup_url, data=data5,
                            headers={"Referer": date_popup_url})
            assert r5.status_code == 200
            if "QueryWizard" not in r5.text:
                log.warning("Date popup response unexpected: %s", r5.text[:300])
            log.info("  Date popup submitted OK")

            # Step 6: POST filter page with Run Search (_ctl6)
            # The first POST after date popup sets the dates on the filter page,
            # the second POST actually executes the search (two-step behavior observed empirically).
            log.info("Step 6a: POST Run Search (_ctl6) — first pass (date confirmation)")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            data6 = {
                "__EVENTTARGET": "",
                "__EVENTARGUMENT": "",
                "_ctl6.x": "10",
                "_ctl6.y": "10",
            }
            data6.update(hidden_after_click)
            r6a = _safe_post(session, filter_url, data=data6,
                             headers={"Referer": filter_url},
                             allow_redirects=True)
            assert r6a.status_code == 200
            if _is_session_expired(r6a.text):
                raise RuntimeError("Session expired at Run Search step 6a")

            # If already at results, great. Otherwise do second pass.
            if "AcnCount" in r6a.text:
                r6 = r6a
                log.info("  Results page on first pass: %d bytes", len(r6.content))
            else:
                log.info("Step 6b: POST Run Search (_ctl6) — second pass (execute search)")
                time.sleep(SLEEP_BETWEEN_REQUESTS)
                hidden_r6a = _extract_hidden(r6a.text)
                data6b = {
                    "__EVENTTARGET": "",
                    "__EVENTARGUMENT": "",
                    "_ctl6.x": "10",
                    "_ctl6.y": "10",
                }
                data6b.update(hidden_r6a)
                r6 = _safe_post(session, filter_url, data=data6b,
                                headers={"Referer": filter_url},
                                allow_redirects=True)
                assert r6.status_code == 200
                if _is_session_expired(r6.text):
                    raise RuntimeError("Session expired at Run Search step 6b")
                if "AcnCount" not in r6.text:
                    log.error("Still not on results page. URL: %s, First 500: %s",
                              r6.url, r6.text[:500])
                    raise RuntimeError("Failed to reach results page after two _ctl6 passes")

            acn_m = re.search(r'AcnCount"[^>]*value="(\d+)"', r6.text)
            acn_count = int(acn_m.group(1)) if acn_m else -1
            log.info("  Results page: %d bytes, ACN count=%d", len(r6.content), acn_count)

            # Step 7: GET CSV export
            log.info("Step 7: GET CSV export")
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            r7 = _safe_get(session, export_url,
                           headers={"Referer": r6.url})
            assert r7.status_code == 200

            # Verify it's actually CSV, not HTML
            content_type = r7.headers.get("Content-Type", "")
            first_line = r7.content[:100].decode("utf-8", errors="replace").strip()
            if "<html" in first_line.lower() or "<!doctype" in first_line.lower():
                raise RuntimeError(f"CSV export returned HTML: {first_line[:200]}")

            log.info("  CSV: %d bytes, Content-Type: %s", len(r7.content), content_type)
            return r7.content.decode("utf-8", errors="replace"), acn_count

        except RuntimeError as e:
            if attempt == 0:
                log.warning("Attempt %d failed: %s — retrying with fresh session", attempt + 1, e)
                time.sleep(SLEEP_BETWEEN_REQUESTS * 2)
            else:
                raise


def header_to_snake(raw_headers):
    """
    Convert CSV headers to unique snake_case column names.
    Duplicate names get _2, _3 suffixes.
    """
    seen = {}
    result = []
    for h in raw_headers:
        s = re.sub(r'[^a-z0-9]+', '_', h.lower()).strip('_')
        if not s:
            s = "col"
        if s in seen:
            seen[s] += 1
            s = f"{s}_{seen[s]}"
        else:
            seen[s] = 1
        result.append(s)
    return result


def parse_csv(raw_text):
    """
    Parse ASRS CSV (2 header rows: group-header line 0, column-header line 1, then data).
    Returns (headers_raw, col_names_snake, rows_as_dicts).
    """
    lines = raw_text.splitlines()
    if len(lines) < 3:
        raise ValueError(f"CSV too short: {len(lines)} lines")

    # Line 0: group headers (blank, Time, Time, Place...)
    # Line 1: column names (ACN, Date, ...)
    reader_h = csv.reader([lines[1]])
    headers_raw = list(reader_h)[0]
    col_names = header_to_snake(headers_raw)

    # Data starts at line 2
    reader_data = csv.reader(lines[2:])
    rows = []
    for row in reader_data:
        if not row or not row[0].strip():
            continue
        padded = row[:len(col_names)]
        while len(padded) < len(col_names):
            padded.append("")
        rows.append(dict(zip(col_names, padded)))

    return headers_raw, col_names, rows


def init_db(db_path):
    """Create tables if not exist. Returns connection."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS harvest_state (
            month      TEXT PRIMARY KEY,
            rows       INTEGER,
            status     TEXT,
            fetched_at TEXT
        )
    """)
    conn.commit()
    return conn


def ensure_reports_table(conn, col_names):
    """Create or update reports table to include all columns."""
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='reports'")
    if cur.fetchone() is None:
        cols_ddl = ", ".join(
            f'"{c}" TEXT' for c in col_names if c != "acn"
        )
        conn.execute(f"""
            CREATE TABLE reports (
                acn TEXT PRIMARY KEY,
                fetched_month TEXT,
                {cols_ddl}
            )
        """)
        conn.commit()
        log.info("Created reports table with %d columns", len(col_names) + 1)
    else:
        cur2 = conn.execute("PRAGMA table_info(reports)")
        existing = {row[1] for row in cur2}
        added = []
        for c in col_names + ["fetched_month"]:
            if c not in existing and c != "acn":
                conn.execute(f'ALTER TABLE reports ADD COLUMN "{c}" TEXT')
                added.append(c)
        if added:
            conn.commit()
            log.info("Added %d columns: %s", len(added), added[:5])


def upsert_rows(conn, col_names, rows, fetched_month):
    """Upsert rows into reports table."""
    if not rows:
        return 0
    all_cols = ["acn", "fetched_month"] + [c for c in col_names if c != "acn"]
    placeholders = ", ".join("?" for _ in all_cols)
    col_list = ", ".join(f'"{c}"' for c in all_cols)
    sql = f"INSERT OR REPLACE INTO reports ({col_list}) VALUES ({placeholders})"

    def make_values(row):
        vals = [row.get("acn", ""), fetched_month]
        for c in col_names:
            if c != "acn":
                vals.append(row.get(c, ""))
        return vals

    with conn:
        conn.executemany(sql, [make_values(r) for r in rows])
    return len(rows)


def set_state(conn, month, rows, status):
    conn.execute(
        "INSERT OR REPLACE INTO harvest_state (month, rows, status, fetched_at) VALUES (?,?,?,?)",
        (month, rows, status, datetime.utcnow().isoformat())
    )
    conn.commit()


def get_state(conn, month):
    cur = conn.execute("SELECT status FROM harvest_state WHERE month=?", (month,))
    row = cur.fetchone()
    return row[0] if row else None


def fetch_and_store_month(month_str, db_path, force=False, print_headers=False):
    """
    Fetch one month (YYYY-MM) and store into db_path.
    Returns number of rows inserted, or -1 if already done.
    """
    conn = init_db(db_path)
    if not force and get_state(conn, month_str) == "ok":
        log.info("Month %s already done, skipping", month_str)
        conn.close()
        return -1

    year, month = int(month_str[:4]), int(month_str[5:7])

    log.info("=== Fetching month %s ===", month_str)
    set_state(conn, month_str, 0, "in_progress")

    try:
        raw_text, acn_count_server = fetch_csv_for_range(year, month, year, month)
    except Exception as e:
        log.error("Failed to fetch %s: %s", month_str, e)
        set_state(conn, month_str, 0, "failed")
        conn.close()
        raise

    headers_raw, col_names, rows = parse_csv(raw_text)

    # Cap defense: 10000 rows = cap hit, log loudly
    if len(rows) >= 10000:
        log.error(
            "CAP HIT for %s: got %d rows (API monthly-only, cannot split further). "
            "Consider splitting manually if data is incomplete!", month_str, len(rows)
        )

    ensure_reports_table(conn, col_names)
    n = upsert_rows(conn, col_names, rows, month_str)
    set_state(conn, month_str, n, "ok")
    conn.close()

    log.info("Month %s: stored %d rows (server reported %d)", month_str, n, acn_count_server)

    if print_headers:
        print("\n=== FROZEN CSV HEADERS ===")
        for i, h in enumerate(headers_raw):
            if h.strip():
                print(f"  [{i:3d}] {h}")
        print(f"Total named headers: {len([h for h in headers_raw if h.strip()])}")
        print(f"Total columns: {len(headers_raw)}")

    return n


def main():
    parser = argparse.ArgumentParser(description="ASRS NASA incident report harvester")
    parser.add_argument("--month", required=True, help="Month to fetch (YYYY-MM)")
    parser.add_argument("--db", default="asrs.db", help="SQLite database path")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if already done")
    parser.add_argument("--print-headers", action="store_true", help="Print CSV headers after fetch")
    args = parser.parse_args()

    if not re.match(r'^\d{4}-\d{2}$', args.month):
        print(f"ERROR: month must be YYYY-MM, got: {args.month}", file=sys.stderr)
        sys.exit(1)

    try:
        n = fetch_and_store_month(args.month, args.db, force=args.force,
                                  print_headers=args.print_headers)
        if n == -1:
            print(f"Month {args.month} already in DB (use --force to re-fetch)")
        else:
            print(f"OK: stored {n} rows for {args.month}")
    except Exception as e:
        log.error("Fatal: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
