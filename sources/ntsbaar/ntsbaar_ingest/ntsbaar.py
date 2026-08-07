# ntsbaar_ingest/ntsbaar.py
"""NTSB Aircraft Accident Reports (AAR) — the narrative reports, 1967-2021.

This is not the NTSB bulk dump. `sources-node/ntsb` ingests avall.zip, which
carries structured fields for every US occurrence but no investigation text.
This package fetches the AARs: the Board's full written reports on major
accidents, the documents that contain the analysis, findings and probable
cause. Measured on prod, 16,929 US occurrences before 1967 hold exactly two
narratives, and the picture after 1967 is not much better.

**Where the index comes from.** NTSB's own reports page is a SharePoint app
that builds its list in the browser; the underlying file listing returns 401
and the site's REST list contains no AAR entries. Embry-Riddle's Hunt Library
publishes a static, year-by-year index of the same reports that links straight
to ntsb.gov, so the index is read from there and every PDF is fetched from the
NTSB itself. One request to the library, 415 to the primary source.

That is a dependency on a third party for discovery, so discover() fails loudly
when the index yields nothing rather than reporting an empty run.

**Two kinds of document.** Sampled 36 of the 415: 12 carry a real text layer
(200,000 characters on average) and 24 are scans that pdftotext reads as a few
dozen characters of noise. Reports from about 1995 onward tend to have text;
the 1970s and 80s are scans. So parse() tries the text layer first and falls
back to OCR — unlike rosap, where OCR is the only path.

The filename encodes the report number: AAR<YY><NN>.pdf, e.g. AAR7226 is the
26th report adopted in 1972 and AAR2101 the first of 2021. The two-digit year
wraps, so it is read against the collection's own 1967-2021 span.
"""
import re

BASE = "https://www.ntsb.gov"
INDEX_URL = ("https://huntlibrary.erau.edu/collections/aerospace-and-aviation-"
             "reports/ntsb/aircraft-accident-reports")
DELAY = 1.5
OCR_LANG = "eng"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

# Any ntsb.gov PDF on the index page. Case varies in the wild — the index
# carries both AAR2103.pdf and aar2103.pdf.
_PDF_RE = re.compile(r'https?://(?:www\.)?ntsb\.gov/[^"\'\s]*?\.pdf', re.I)

# AAR7226 -> report 26 of 1972. Some carry a suffix (AAR7226A, a revision).
_NUMBER_RE = re.compile(r"(A[AI]R)(\d{2})(\d{2})([A-Z]?)\.pdf$", re.I)

# The collection spans 1967-2021, so a two-digit year of 67 or more is 19xx.
_CENTURY_PIVOT = 67


def report_number(url):
    """'AAR7226' from a URL, upper-cased, or None."""
    m = _NUMBER_RE.search((url or "").strip())
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}".upper()


def adopted_year(url):
    """The four-digit year the report number encodes, or None.

    This is when the Board adopted the report, not when the accident happened
    — often a year or two later. It is used for ordering and for the case id,
    never as an event date.
    """
    m = _NUMBER_RE.search((url or "").strip())
    if not m:
        return None
    yy = int(m.group(2))
    return 1900 + yy if yy >= _CENTURY_PIVOT else 2000 + yy


def parse_index(html):
    """Index HTML → ordered unique report dicts.

    Deduplicates on the report number rather than the URL, because the index
    lists the same report under both upper- and lower-case filenames.
    """
    out, seen = [], set()
    for url in _PDF_RE.findall(html or ""):
        number = report_number(url)
        if not number or number in seen:
            continue
        seen.add(number)
        out.append({
            "case_id": number,
            "pdf_url": url,
            "adopted_year": adopted_year(url),
        })
    return out


def download(client, url, dest):
    """GET the PDF and write it. Raises on non-2xx or a non-PDF body."""
    resp = client.get(url, headers=HEADERS)
    resp.raise_for_status()
    body = resp.content
    if body[:5] != b"%PDF-":
        raise RuntimeError(f"{url} did not return a PDF")
    with open(dest, "wb") as fh:
        fh.write(body)
    return len(body)
