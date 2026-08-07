# rosap_ingest/rosap.py
"""ROSA P — US DOT National Transportation Library, CAB accident reports.

Source: https://rosap.ntl.bts.gov/cbrowse?parentId=dot:32931
Collection: "Investigations of Aircraft Accidents 1934-1965" — 791 digitized
Civil Aeronautics Board reports, DOI 10.21949/1530839. US government work, so
public domain.

Three things make this source unlike the others here:

  * **Akamai fronts it and only accepts a real browser.** curl and httpx get
    403 with any User-Agent, and so does Playwright's own page.request. Only
    browser navigation works, which is why this package uses patchright rather
    than httpx and takes PDFs through Chrome's download path.

  * **Every PDF is an image-only scan.** pdftotext returns zero characters
    from all of them, checked across the 1930s, 1950s and 1960s. OCR is not a
    fallback here, it is the only way in — parse() goes straight to it.

  * **The listing title carries the facts**, in a fixed shape:

        Investigation of Aircraft Accident: SLICK AIRWAYS: BOSTON, MASSACHUSETTS: 1964-03-10
                                            ^operator     ^location               ^date

    782 of the 791 parse this way. The rest are supplements attached to a
    case — [Amendment], [Hearing Notice], [Letter from …] — and five of those
    belong to a single 1954 accident, so treating them as accidents would
    invent four duplicates.

The collection covers US *operators*, not US territory: PAN AMERICAN AIRWAYS
at MAYADINE, SYRIA is in it. So country is derived from the location and left
null when it cannot be, never stamped as a constant.
"""
import re

BASE = "https://rosap.ntl.bts.gov"
COLLECTION_PID = "dot:32931"
COLLECTION = f"{BASE}/cbrowse?parentId={COLLECTION_PID}"
DELAY = 2.5  # the collection is small and taken once; there is no hurry
PAGE_SIZE = 20  # what the listing serves; it exposes no page-size parameter
OCR_LANG = "eng"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Take each item's link and its title from the SAME anchor. Collecting the two
# with separate sweeps and zipping them by position is what put 13 rows under
# a neighbour's operator and date on the first run: one page yielded an extra
# link and everything after it shifted by one.
ANCHOR_JS = 'els => els.map(e => [e.getAttribute("href"), (e.innerText||"").trim()])'
ANCHOR_SELECTOR = 'a[href*="/view/dot/"]'

_PID_RE = re.compile(r"/view/dot/(\d+)")

TITLE_RE = re.compile(
    r"Investigation of Aircraft Accident:\s*(?P<operator>[^:]+):\s*"
    r"(?P<location>.+?):\s*(?P<date>\d{4}-\d{2}-\d{2})\s*"
    r"(?:\[(?P<kind>[^\]]+)\])?\s*$",
    re.I,
)

# US states and territories as they are written in these titles, so a location
# can be resolved to a country instead of assuming one. Measured over all 791
# locations in the collection. The separator is a comma OR plain space,
# because the titles write it both ways ("BOSTON, MASSACHUSETTS" and
# "DETROIT MICHIGAN"), and the territories are the ones that were US at the
# time these reports were written.
_US_TAIL = re.compile(
    r"(?:^|,\s*|\s+)(?:ALABAMA|ALASKA|ARIZONA|ARKANSAS|CALIFORNIA|COLORADO|"
    r"CONNECTICUT|DELAWARE|FLORIDA|GEORGIA|HAWAII|IDAHO|ILLINOIS|INDIANA|IOWA|"
    r"KANSAS|KENTUCKY|LOUISIANA|MAINE|MARYLAND|MASSACHUSETTS|MICHIGAN|"
    r"MINNESOTA|MISSISSIPPI|MISSOURI|MONTANA|NEBRASKA|NEVADA|NEW HAMPSHIRE|"
    r"NEW JERSEY|NEW MEXICO|NEW YORK|NORTH CAROLINA|NORTH DAKOTA|OHIO|"
    r"OKLAHOMA|OREGON|PENNSYLVANIA|RHODE ISLAND|SOUTH CAROLINA|SOUTH DAKOTA|"
    r"TENNESSEE|TEXAS|UTAH|VERMONT|VIRGINIA|WASHINGTON|WEST VIRGINIA|"
    r"WISCONSIN|WYOMING|D\.?C\.?|DISTRICT OF COLUMBIA|PUERTO RICO|ALASKA "
    r"TERRITORY|HAWAII TERRITORY|GUAM|CANAL ZONE|VIRGIN ISLANDS|"
    r"ALA\.?|ARIZ\.?|ARK\.?|CALIF\.?|COLO\.?|CONN\.?|DEL\.?|FLA\.?|GA\.?|ILL\.?|IND\.?|"
    r"KAN\.?|KY\.?|LA\.?|MD\.?|MASS\.?|MICH\.?|MINN\.?|MISS\.?|MO\.?|MONT\.?|NEB\.?|"
    r"NEV\.?|N\.?H\.?|N\.?J\.?|N\.?M\.?|N\.?Y\.?|N\.?C\.?|N\.?D\.?|OKLA\.?|ORE\.?|PA\.?|"
    r"R\.?I\.?|S\.?C\.?|S\.?D\.?|TENN\.?|TEX\.?|VT\.?|VA\.?|WASH\.?|W\.?VA\.?|WIS\.?|WYO\.?)"
    r"\s*$",
    re.I,
)


def item_url(pid):
    return f"{BASE}/view/dot/{pid}"


def pdf_url(pid):
    return f"{BASE}/view/dot/{pid}/dot_{pid}_DS1.pdf"


def listing_url(offset=0):
    return COLLECTION if not offset else f"{COLLECTION}&start={offset}"


def pid_from_href(href):
    m = _PID_RE.search(href or "")
    return m.group(1) if m else None


def parse_title(title):
    """Split a listing title into its parts.

    Returns {operator, location, event_date, doc_kind} — doc_kind is 'report'
    for the report itself and the bracketed label for a supplement. All None
    when the title is not of this collection's shape.
    """
    text = " ".join((title or "").split())
    m = TITLE_RE.match(text)
    if not m:
        return {"operator": None, "location": None, "event_date": None,
                "doc_kind": None}
    return {
        "operator": m.group("operator").strip(" ,"),
        "location": m.group("location").strip(" ,"),
        "event_date": m.group("date"),
        "doc_kind": (m.group("kind") or "report").strip(),
    }


def country_of(location):
    """'US' when the location ends in a US state, else None.

    Deliberately not a constant. This collection is organised by operator, not
    by territory, so it carries accidents at SHANNON, IRELAND and MAYADINE,
    SYRIA. Stamping every row US is the same error that put South African
    country codes on foreign reports in sacaa — better to leave it unset and
    let a later pass resolve the place.
    """
    if not location:
        return None
    return "US" if _US_TAIL.search(" ".join(location.split())) else None


def parse_listing(pairs):
    """(href, title) pairs from the listing anchors → ordered item dicts."""
    out, seen = [], set()
    for href, title in pairs or []:
        pid = pid_from_href(href)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        row = {"pid": pid, "title": " ".join((title or "").split()),
               "pdf_url": pdf_url(pid)}
        row.update(parse_title(title))
        out.append(row)
    return out
