# eaaid_ingest/eaaid.py
"""Egypt EAAID (Aircraft Accident Investigation Directorate, ECAA) ingest.

Source: https://www.civilaviation.gov.eg/Accident/reports
  Table: 88 unique reports (GUIDs) 2000-2025, each PDF linked ~6x in 6 table
  cells (dedup by GUID before any processing).
  Download URL pattern:
    https://www.civilaviation.gov.eg/Accident_GenDownloadRes?id=<GUID>%5C<timestamp>_<seq>.pdf&name=Accident_Report

Site accessibility:
  - Reachable from Hetzner (German IP); times out from Mac.
  - Session cookie returned on first GET of listing page (cookiesession1=...)
    and is required for PDF downloads.
  - HTTP 200 with no redirect; Content-Type: APPLICATION/octet-stream for PDFs.
  - No Cloudflare or bot challenge observed from Hetzner.

Notable events:
  - MS804 (2016-05-19): 3 interim statements + 1 final (SU-GCC, A320-232).
    The final report was published in October 2024; its event_date is the
    ACCIDENT date 2016-05-19 (not the publication date).  The 3 interims are
    marked superseded_by=eaaid-2016-SU-GCC and excluded from eaaid_accidents.
  - Metrojet A321 (2015-10-31): 1 interim statement (EI-ETJ)
  - Harare B767 (2000-02-22): 1 final report (SU-GAO)
  - Many EgyptAir/Airbus A320 incidents 2012-2014

Language: EN (EAAID publishes in English). Arabic script in PDFs is stripped
  by pdf.py before narrative extraction.

case_id scheme: eaaid-<YYYY>-<REG> where YYYY is the ACCIDENT year and REG
  is the first (or only) registration.  Multi-registration events (e.g.
  "SU-GCR / D-ABOM") use only the first registration in the case_id; the full
  registration string is preserved in the registration field.
  When multiple reports exist for the same event (same date+registration),
  the final report gets the base id (eaaid-YYYY-REG) and interim/preliminary
  reports get counter suffixes (eaaid-YYYY-REG-2, -3, ...) with superseded_by
  pointing at the final.  build() excludes superseded rows from eaaid_accidents.
  Fallback: eaaid-<YYYY>-<GUID_PREFIX_8> when no valid registration available.

report_type: from listing column ('Final', 'Interim statement', 'Preliminary')

event_date: always the ACCIDENT date, not the report publication date.  For
  MS804 this means 2016-05-19 even though the final report was published in
  October 2024.

country: always 'EG' (Egypt) — EAAID is the Egyptian investigation authority,
  reports cover accidents/incidents in Egyptian airspace or by Egyptian carriers.

GUID dedup math: 528 total href references / 88 unique GUIDs = 6 links per
  GUID (one per table cell). The table has exactly 88 <tr> rows with distinct
  GUIDs — no event with multiple GUIDs sharing the same accident exists at the
  row level; MS804 has 4 separate GUIDs (3 interims + 1 final).
"""
import re
import sys
import time

LISTING_URL = "https://www.civilaviation.gov.eg/Accident/reports"
DOWNLOAD_BASE = "https://www.civilaviation.gov.eg"

# The only href shape the ECAA listing emits (see the module docstring):
#   /Accident_GenDownloadRes?id=<36-char GUID>%5C<timestamp>_<seq>.pdf&name=<label>
#
# The tail is an explicit allowlist, NOT `[^"]+`. This value is carried into a
# remote shell command in pipeline.fetch(); `[^"]+` admits single quotes,
# backticks and $(...) from a page we do not control, which made the href an
# arbitrary-command vector on the fetch host. Quoting in pipeline.py is the
# second layer — this is the first.
_HREF_TAIL  = r"[A-Za-z0-9_.%&;=\-]+"
_HREF_RE    = re.compile(
    r'href="(/Accident_GenDownloadRes\?id=([0-9a-f\-]{36})(' + _HREF_TAIL + r'))"'
)
_HREF_VALID = re.compile(
    r"\A/Accident_GenDownloadRes\?id=[0-9a-f\-]{36}" + _HREF_TAIL + r"\Z"
)


def valid_href(href):
    """True if `href` is a download link we are willing to hand to a shell."""
    return isinstance(href, str) and bool(_HREF_VALID.match(href))

DELAY = 2.0  # seconds between requests — polite sequential fetching

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Registration prefix patterns for case_id
_REG_RE = re.compile(r"\b(SU-[A-Z]{3}|EI-[A-Z]{3}|VP-[A-Z0-9]{3}|[A-Z][0-9]-[A-Z]{2,4}|[A-Z]{1,2}-[A-Z0-9]{3,5})\b")
_SU_REG_RE = re.compile(r"\bSU-[A-Z]{3}\b")


def safe_reg(registration):
    """Normalize registration to safe ASCII for case_id construction."""
    return re.sub(r"[^A-Za-z0-9\-]", "_", (registration or "").strip().upper())


def build_case_id(date, registration, seq=1):
    """Build a stable case_id from date + registration.

    date: 'YYYY-MM-DD' or 'YYYY-...' string
    registration: aircraft registration string
    seq: 1 = primary; 2+ = for additional reports on the same event
    """
    year = (date or "")[:4] if date else "0000"
    reg_clean = safe_reg(registration)
    if not reg_clean or reg_clean in ("_", ""):
        reg_clean = "UNKNOWN"
    base = f"eaaid-{year}-{reg_clean}"
    if seq > 1:
        return f"{base}-{seq}"
    return base


def parse_listing_html(html):
    """Parse the EAAID reports table from the listing HTML.

    Returns list of dicts with keys:
        guid, href, report_type, category, date, location, aircraft, registration
    Deduplicates by GUID (first occurrence wins).
    """
    start = html.find("<tbody>")
    end   = html.find("</tbody>")
    if start == -1:
        return []
    tbody = html[start:end]

    rows = re.findall(r"<tr>(.*?)</tr>", tbody, re.DOTALL)
    events = []
    seen_guids = set()

    for row in rows:
        tds = re.findall(r"<td>(.*?)</td>", row, re.DOTALL)
        if len(tds) < 6:
            continue
        link_m = _HREF_RE.search(tds[0])
        if not link_m:
            continue
        full_href = link_m.group(1)
        guid      = link_m.group(2)

        if guid in seen_guids:
            continue
        seen_guids.add(guid)

        def cell_text(td):
            t = re.sub(r"<[^>]+>", "", td).strip()
            return t

        # Rebuild download URL: decode HTML entities but preserve %5C encoding
        href_clean = full_href.replace("&amp;", "&")
        # Re-check after entity decoding: `&amp;` -> `&` changes the string, so
        # the shape that _HREF_RE accepted is not necessarily the shape we store.
        if not valid_href(href_clean):
            print(f"[eaaid parse] rejecting malformed href: {href_clean[:120]!r}",
                  file=sys.stderr)
            continue

        events.append({
            "guid":        guid,
            "href":        href_clean,
            "report_type": cell_text(tds[0]),
            "category":    cell_text(tds[1]),
            "date":        cell_text(tds[2]).strip(),
            "location":    cell_text(tds[3]).strip(),
            "aircraft":    cell_text(tds[4]).strip(),
            "registration": cell_text(tds[5]).strip(),
        })

    return events


def assign_case_ids(events):
    """Assign stable case_ids to events.

    Events sharing the same (date, registration) key get sequenced:
    eaaid-YYYY-SU-GCC, eaaid-YYYY-SU-GCC-2, eaaid-YYYY-SU-GCC-3, ...

    Each event dict gets a 'case_id' field added in-place.
    Returns the events list.
    """
    key_counts = {}
    for evt in events:
        date = evt.get("date", "")
        reg  = evt.get("registration", "")
        # For MS804 type events: same reg+date = multiple reports (interim/final)
        key = (date[:4] if date else "0000", safe_reg(reg) or "UNKNOWN")
        key_counts[key] = key_counts.get(key, 0) + 1
        seq = key_counts[key]
        evt["case_id"] = build_case_id(date, reg, seq)
    return events


def pdf_filename(case_id, guid):
    """Build a safe local filename for a PDF."""
    safe_id = re.sub(r"[^A-Za-z0-9\-_]", "_", case_id)
    return f"{safe_id}.pdf"


def source_url(href):
    """Build full absolute source URL from relative href."""
    return DOWNLOAD_BASE + href


def valid_source_url(url):
    """True if `url` is an absolute download URL we are willing to shell out on."""
    if not isinstance(url, str) or not url.startswith(DOWNLOAD_BASE):
        return False
    return valid_href(url[len(DOWNLOAD_BASE):])
