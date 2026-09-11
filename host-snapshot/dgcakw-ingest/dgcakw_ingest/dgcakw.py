# dgcakw_ingest/dgcakw.py
"""Kuwait DGCA (Directorate General of Civil Aviation) accident/incident
investigation ingest.

Source: kas2.dgca.gov.kw — KASD (Kuwait Aviation Safety Department) WordPress site.
  Main reports listing:
    https://kas2.dgca.gov.kw/kasd/aviation-safety-department/
            mor-accidentincident-or-occurrence-reporting/asd-accident-incident-reports/

The listing page contains a Ninja Table (JS-rendered) with links to PDF reports.
Two confirmed reports as of 2022:
  1. J9-787 (9K-CAK) — Jazeera A320-214, balloon cable strike, 27 Aug 2017
     PDF: /kasd/wp-content/uploads/2018/10/ANNEX-13-FINAL-REPORT-J9-787-V1.pdf
  2. 9K-AOE — Kuwait Airways B.777-300ER, catering truck impact, 04 Feb 2018
     PDF: /kasd/wp-content/uploads/2018/07/DGCA-Bulletin-2018.pdf

Live site (kas2.dgca.gov.kw) times out from all tested vantages (Mac, minipc,
hetzner) as of 2026-06-10. All PDFs are fetched from the Wayback Machine.

case_id scheme: DGCAKW-<YEAR>-<REG>
  e.g. DGCAKW-2017-9K-CAK
       DGCAKW-2018-9K-AOE

Both PDFs have EN text layer; Arabic script is stripped by pdf.py.
No OCR needed for current reports.
"""
import re

# Wayback base
WAYBACK_BASE = "https://web.archive.org/web"
CDX_BASE = "https://web.archive.org/cdx/search/cdx"

# Live listing URL (kept for reference; times out in practice)
LIVE_LISTING_URL = (
    "https://kas2.dgca.gov.kw/kasd/aviation-safety-department/"
    "mor-accidentincident-or-occurrence-reporting/asd-accident-incident-reports/"
)

# Source domain
SOURCE_HOST = "kas2.dgca.gov.kw"

DELAY = 2.0
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# Hardcoded catalog: all known reports.
# case_id, title, pdf_url (live), event_date, aircraft, registration, operator,
# location, report_type, archive_ts (best Wayback snapshot of PDF)
KNOWN_REPORTS = [
    {
        "case_id": "DGCAKW-2017-9K-CAK",
        "title": "Aircraft Mid-Air Collision with The Cable of Tethered Military Balloon (Jazeera J9-787)",
        "pdf_url_live": (
            "https://kas2.dgca.gov.kw/kasd/wp-content/uploads/"
            "2018/10/ANNEX-13-FINAL-REPORT-J9-787-V1.pdf"
        ),
        "archive_ts": "20220627061343",
        "event_date": "2017-08-27",
        "aircraft": "Airbus A320-214",
        "registration": "9K-CAK",
        "operator": "Jazeera Airways",
        "location": "Kuwait International Airport, State of Kuwait",
        "report_type": "Serious Incident Final Report",
        "flight_number": "J9-787",
    },
    {
        "case_id": "DGCAKW-2018-9K-AOE",
        "title": "Serious Incident: Boeing B.777-300ER 9K-AOE hit by catering truck at Kuwait International Airport",
        "pdf_url_live": (
            "https://kas2.dgca.gov.kw/kasd/wp-content/uploads/"
            "2018/07/DGCA-Bulletin-2018.pdf"
        ),
        "archive_ts": "20221130000617",
        "event_date": "2018-02-04",
        "aircraft": "Boeing B.777-300ER",
        "registration": "9K-AOE",
        "operator": "Kuwait Airways",
        "location": "Kuwait International Airport, Gate 5, State of Kuwait",
        "report_type": "DGCA Bulletin",
        "flight_number": "KU 788",
    },
]

# reg extraction pattern for case_id validation
_REG_RE = re.compile(r"\b(9K-[A-Z]{2,4})\b")


def wayback_pdf_url(live_url, archive_ts):
    """Build Wayback URL for a PDF snapshot."""
    return f"{WAYBACK_BASE}/{archive_ts}/{live_url}"
