# gcaa_ingest/gcaa.py
"""
GCAA UAE (General Civil Aviation Authority — Air Accident Investigation
Sector) aviation investigation parser — a SharePoint REST list API.

The report listing is ONE SharePoint REST GET:
    GET .../airaccidentinvestigation/_api/web/lists/
        getbytitle('Incidents Investigation Reports')/items
        ?$expand=AttachmentFiles&$top=500
171 items, 2008-2024.  Browser UA; Accept: application/json;odata=verbose.

⚠️ OData VERBOSE shape: the response wraps the rows in {"d": {"results": [...]}}
(NOT a flat list, NOT {"value": [...]} which is odata=nometadata).  We tolerate
all three shapes when parsing.

⚠️ SharePoint encodes spaces in internal field names as '_x0020_':
    Reference_x0020_No       'AIFN/0007/2013'
    Registration_x0020_No    'A6-FDE'  (nulls + foreign regs 'UP-A3003' too)
    Aircraft_x0020_Type      'Boeing 737-800'
    Occurrence_x0020_Date    '2013-04-05T20:00:00Z'
    Occurrence_x0020_Category'Accident' | 'Serious Incident' | 'Incident'
    Report_x0020_Status      'Final' | 'Preliminary' | 'Summary' | 'Interim' | ...
Plus Location, Damage, OccurrenceYear, Id (SharePoint item id).

AttachmentFiles is itself an OData collection: {"results": [{FileName,
ServerRelativeUrl}, ...]}.  Rows with NO attachment are stubs and skipped.
The PDF URL = the ServerRelativeUrl percent-encoded (filenames contain spaces)
and absolute-ified against https://www.gcaa.gov.ae.

case_id = Reference_No normalized: 'AIFN/0007/2013' -> 'aifn-0007-2013'.
Fallback when the reference is null: 'gcaa-{Id}' (SharePoint item Id).
"""
import re
from urllib.parse import quote

BASE = "https://www.gcaa.gov.ae"
LIST_PATH = (
    "/en/departments/airaccidentinvestigation/_api/web/lists/"
    "getbytitle('Incidents%20Investigation%20Reports')/items"
)
ITEMS_URL = f"{BASE}{LIST_PATH}?$expand=AttachmentFiles&$top=500"
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json;odata=verbose",
    "Accept-Language": "en;q=0.9",
}

_NONSLUG = re.compile(r"[^a-z0-9]+")


def odata_results(payload):
    """
    Normalize an OData response to a plain list of item dicts, tolerating:
      verbose      -> {"d": {"results": [...]}}
      verbose-1    -> {"d": [...]}             (rare single-collection form)
      nometadata   -> {"value": [...]}
      bare list    -> [...]
    An AttachmentFiles sub-collection is normalized the same way downstream.
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if "d" in payload:
        d = payload["d"]
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("results") or []
        return []
    if "value" in payload:
        return payload.get("value") or []
    if "results" in payload:
        return payload.get("results") or []
    return []


def attachment_list(item):
    """Return the list of attachment dicts for an item (handles OData wrap)."""
    af = item.get("AttachmentFiles")
    if af is None:
        return []
    if isinstance(af, list):
        return af
    if isinstance(af, dict):
        return af.get("results") or []
    return []


def case_id_from_reference(reference_no, item_id=None):
    """
    'AIFN/0007/2013' -> 'aifn-0007-2013'.  Lowercase, every run of
    non-alphanumeric characters collapses to a single '-'.
    Falls back to 'gcaa-{item_id}' when the reference is null/blank.
    """
    if reference_no:
        slug = _NONSLUG.sub("-", str(reference_no).lower()).strip("-")
        if slug:
            return slug
    if item_id is not None:
        return f"gcaa-{item_id}"
    return None


def attachment_url(server_relative_url):
    """
    Absolute-ify + percent-encode a SharePoint ServerRelativeUrl.
    Filenames carry spaces/commas; we percent-encode each path segment while
    keeping '/' separators, then prefix the gcaa.gov.ae origin.
    """
    if not server_relative_url:
        return None
    path = str(server_relative_url)
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = "/" + path
    # safe='/' keeps separators; spaces/commas/parens get encoded
    return BASE + quote(path, safe="/")


def pick_attachment(attachments):
    """
    Choose the preferred attachment from an item's list.
    Prefer a 'Final' report, else the LAST attachment (newest revision tends
    to be appended last in SharePoint).  Returns (filename, server_relative_url)
    or (None, None) when there are no attachments.
    """
    if not attachments:
        return None, None
    finals = [a for a in attachments
              if "final" in (a.get("FileName") or "").lower()]
    chosen = finals[-1] if finals else attachments[-1]
    return chosen.get("FileName"), chosen.get("ServerRelativeUrl")


def parse_item(item):
    """
    Map a raw SharePoint item dict -> flat metadata.
    Returns dict with keys:
        case_id, reference_no, item_id, date, location, damage, aircraft,
        registration, occurrence_category, report_status, year,
        filename, server_relative_url, pdf_url, has_attachment.
    """
    item_id = item.get("Id", item.get("ID"))
    reference_no = item.get("Reference_x0020_No")
    case_id = case_id_from_reference(reference_no, item_id)

    attachments = attachment_list(item)
    filename, srv = pick_attachment(attachments)

    raw_date = item.get("Occurrence_x0020_Date") or ""
    date = raw_date[:10] or None

    return {
        "case_id": case_id,
        "reference_no": reference_no or None,
        "item_id": item_id,
        "date": date,
        "location": (item.get("Location") or "").strip() or None,
        "damage": (item.get("Damage") or "").strip() or None,
        "aircraft": (item.get("Aircraft_x0020_Type") or "").strip() or None,
        "registration": (item.get("Registration_x0020_No") or "").strip()
        or None,
        "occurrence_category": (item.get("Occurrence_x0020_Category") or "")
        .strip()
        or None,
        "report_status": (item.get("Report_x0020_Status") or "").strip()
        or None,
        "year": (str(item.get("OccurrenceYear")).strip()
                 if item.get("OccurrenceYear") else None),
        "filename": filename,
        "server_relative_url": srv,
        "pdf_url": attachment_url(srv),
        "has_attachment": bool(attachments),
    }


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_items(client):
    """Return the list of raw SharePoint item dicts (single API GET)."""
    resp = client.get(ITEMS_URL)
    resp.raise_for_status()
    return odata_results(resp.json())


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
