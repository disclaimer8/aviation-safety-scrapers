# knkt_ingest/knkt.py
"""
KNKT / NTSC Indonesia (Komite Nasional Keselamatan Transportasi) aviation
investigation listing and metadata parser — knkt.go.id.

The listing is ONE JSON endpoint (DataTables backend):
    GET /Investigasi/Get_Investigasi_Penerbangan?row_count=20000&…
returning {"Error":false,"Message":[…858 rows…]}.  Send a Referer and follow
the 301 (it lowercases the path).  ~254 rows carry a report file
(Final_Report / Preliminary_Report / Interim_Report); the rest are
occurrence stubs — skipped.

Report PDFs are ENGLISH born-digital, at
    /Repo/Files/Laporan/Penerbangan/{YEAR}/{filename}
⚠️ TRAP (verified): the folder YEAR is NOT always the occurrence year —
for late-published reports it follows the CASE-NUMBER year (the site's own
JS links 404 on those).  candidate_pdf_urls() yields occurrence-year first,
then the case-number year.

Metadata lives in the Keterangan field:
    "{occurrence type}, {operator} ({aircraft type}/{registration});
     {location} / {KNKT case number}"
Case number formats drift: new "KNKT.YY.MM.DD.NN", old "KNKT/07.01/08.01.36"
→ canonicalized to dot-form.
"""
import re
from urllib.parse import quote

BASE = "https://knkt.go.id"
LISTING_URL = (
    BASE + "/Investigasi/Get_Investigasi_Penerbangan"
)
LISTING_PARAMS = {
    "row_count": "20000",
    "Tahun_Kejadian": "", "Tanggal_Kejadian_Start": "",
    "Tanggal_Kejadian_End": "", "Operator": "", "Reg": "",
    "Aircraft_Type": "", "Lokasi": "",
}
PDF_BASE = BASE + "/Repo/Files/Laporan/Penerbangan"
DELAY = 1.5

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en,id;q=0.9",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Referer": BASE + "/investigasi",
}

# "KNKT.22.07.11.04" (new) or "KNKT/07.01/08.01.36" (old) or "KNKT 18.02.06.04"
_CASE_RE = re.compile(r"KNKT[\s./]+([\d./\s]+\d)")
# "(Boeing 737-400/PK-KKW)" — type/reg parenthetical
_TYPE_REG_RE = re.compile(r"\(([^()/]+(?:\([^()]*\))?[^()/]*)/([A-Z0-9-]+)\)\s*;?")
_REG_RE = re.compile(r"\b((?:PK|[A-Z0-9]{1,2})-[A-Z0-9]{2,4})\b")


def canonical_case_id(raw):
    """Normalize any KNKT case-number spelling to 'KNKT.xx.xx.xx.xx'."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]+", ".", raw).strip(".")
    return f"KNKT.{digits}" if digits else None


def parse_keterangan(text):
    """
    Best-effort parse of the Keterangan blob →
        occurrence_type, operator, aircraft, registration, location, case_id
    Pattern: "{type}, {operator} ({aircraft}/{reg}); {location} / {case#}"
    Any part may be absent; returns None for missing fields.
    """
    out = {"occurrence_type": None, "operator": None, "aircraft": None,
           "registration": None, "location": None, "case_id": None}
    if not text:
        return out
    t = re.sub(r"\s+", " ", text).strip()

    case_m = _CASE_RE.search(t)
    if case_m:
        out["case_id"] = canonical_case_id(case_m.group(0))
        t = t[: case_m.start()].rstrip(" /")

    tr_m = _TYPE_REG_RE.search(t)
    if tr_m:
        out["aircraft"] = tr_m.group(1).strip()
        out["registration"] = tr_m.group(2).strip()
        before = t[: tr_m.start()].strip(" ,")
        after = t[tr_m.end():].strip(" ;,")
        # before = "{type}, {operator}" — operator is the LAST comma part
        if "," in before:
            head, op = before.rsplit(",", 1)
            out["occurrence_type"] = head.strip() or None
            out["operator"] = op.strip() or None
        else:
            out["operator"] = before or None
        out["location"] = after or None
    else:
        # no parenthetical — try registration anywhere, keep type as head
        reg_m = _REG_RE.search(t)
        if reg_m:
            out["registration"] = reg_m.group(1)
        parts = t.split(";", 1)
        head = parts[0]
        if "," in head:
            out["occurrence_type"] = head.split(",", 1)[0].strip() or None
        else:
            out["occurrence_type"] = head.strip() or None
        if len(parts) > 1:
            out["location"] = parts[1].strip(" /") or None
    return out


def pick_report(row):
    """(filename, kind) preferring Final > Interim > Preliminary, else None."""
    for field, kind in (("Final_Report", "Final"),
                        ("Final_Report_Interim", "Interim"),
                        ("Interim_Report", "Interim"),
                        ("Preliminary_Report", "Preliminary")):
        f = row.get(field)
        if f:
            return f.strip(), kind
    return None, None


def candidate_years(occurrence_date, filename, case_id):
    """
    Candidate folder years, occurrence-year first, then case-number year
    (the verified trap: late-published reports live under the case year).
    """
    years = []
    if occurrence_date and len(occurrence_date) >= 4 and occurrence_date[:4].isdigit():
        years.append(occurrence_date[:4])
    for src in (filename or "", case_id or ""):
        m = re.search(r"KNKT[./](\d{2})[./]", src)
        if m:
            y = f"20{m.group(1)}"
            if y not in years:
                years.append(y)
    return years


def candidate_pdf_urls(occurrence_date, filename, case_id):
    return [
        f"{PDF_BASE}/{y}/{quote(filename)}"
        for y in candidate_years(occurrence_date, filename, case_id)
    ]


def make_case_id(parsed_case, registration, occurrence_date, taken=None):
    """KNKT case number when present; else '{reg}-{date}'; suffix on clash."""
    if parsed_case:
        base = parsed_case
    elif registration and occurrence_date:
        base = f"{registration}-{occurrence_date}"
    elif occurrence_date:
        base = f"KNKT-{occurrence_date}"
    else:
        base = "KNKT-unknown"
    if taken is None:
        return base
    cand = base
    n = 2
    while cand in taken:
        cand = f"{base}-{n}"
        n += 1
    return cand


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers (live network; not exercised in offline tests)
# ──────────────────────────────────────────────────────────────────────────────


def fetch_listing(client):
    resp = client.get(LISTING_URL, params=LISTING_PARAMS)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Error"):
        raise RuntimeError(f"KNKT listing API error: {data}")
    return data["Message"]


def download_pdf(client, url, dest_path):
    resp = client.get(url)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
