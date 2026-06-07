# bfu_ingest/header.py
"""
PDF-header metadata parser for BFU (Bundesstelle für Flugunfalluntersuchung) accident reports.

Every BFU report's narrative_text (pdftotext output) starts with a standardised
"Identifikation" block of LABELLED German fields:

    Identifikation
    Art des Ereignisses: Unfall          ← inline value (most reports)
    Datum:                               ← or value on next non-empty line
                                           (2-column PDF layout artefact)
    16.01.2023

    Ort:

    Rendsburg

    Luftfahrzeug:                        ← or "Luftfahrzeug 1:" for multi-a/c reports

    Flugzeug

    Hersteller:

    Learjet Corporation

    Muster:

    Learjet 35 A

    Aktenzeichen:

    BFU23-0022-1X

Notably absent in all examined real BFU reports: a "Kennzeichen" (registration) field.
BFU anonymises aircraft identities; no registration number appears in the
Identifikation block.  parse_header() will return registration=None for all BFU reports.

parse_header(narrative_text) -> dict with keys:
    event_class   – "Accident" / "Serious incident" / "Incident" / raw German value / None
    aircraft      – "<Hersteller> <Muster>" (first aircraft if multi-a/c report) / None
    registration  – always None (field absent from BFU Identifikation blocks)
    date_iso      – "YYYY-MM-DD" / None
    location      – raw value of Ort field / None
    case_id       – Aktenzeichen value e.g. "BFU23-0022-1X" / None
"""
import re

_HEADER_WINDOW = 1500  # chars; the Identifikation block is always near the top

# ── German month names (for dates like "15. April 2018") ─────────────────────
_DE_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "marz": 3,
    "april": 4, "mai": 5, "juni": 6, "juli": 7,
    "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}

# ── Event-class mapping ───────────────────────────────────────────────────────
_EVENT_MAP = [
    # Most-specific patterns first (order matters)
    (re.compile(r"schwere\s+st[öo]rung", re.IGNORECASE), "Serious incident"),
    (re.compile(r"st[öo]rung", re.IGNORECASE), "Incident"),
    (re.compile(r"unfall", re.IGNORECASE), "Accident"),
]


def _map_event_class(raw: str) -> str:
    """Map German event-class string to English; return raw value if no match."""
    for pattern, english in _EVENT_MAP:
        if pattern.search(raw):
            return english
    return raw.strip()


# ── Core label extractor ──────────────────────────────────────────────────────

def _field(text: str, label: str) -> str | None:
    """
    Extract the value for `label` from the BFU Identifikation block.

    Tries two layouts:
      (A) inline:   "Label: value"
      (B) next-line: "Label:\n\nvalue"   (value on next non-empty line)

    Returns the first non-empty value found, stripped of whitespace.
    Returns None if the label is not present or has no value.
    """
    # Build a regex that matches "<label>:\s*(.*)$" (multiline, after the colon).
    # The label may contain spaces, periods, etc. — escape it.
    escaped = re.escape(label)
    pattern = re.compile(
        r"^" + escaped + r"(?:\s+\d+)?:\s*(.*?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return None

    inline = m.group(1).strip()
    if inline:
        return inline

    # Inline is empty: find next non-empty line after the label line
    pos = m.end()
    remainder = text[pos:]
    for line in remainder.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped

    return None


# ── Date parser ───────────────────────────────────────────────────────────────

# Numeric: "16.01.2023" or "16. 01. 2023"
_NUMERIC_DATE_RE = re.compile(
    r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})"
)

# German month name: "15. April 2018" or "18. Januar 2011" or "03. März 2022"
# Note: ä in März may survive as UTF-8 or be normalised; match both ä and ae variants.
_DE_MONTH_DATE_RE = re.compile(
    r"(\d{1,2})\.\s*"
    r"(Januar|Februar|M(?:ä|ae|a)rz|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)


def _parse_date(raw: str) -> str | None:
    """Convert a German date string to YYYY-MM-DD ISO format."""
    if not raw:
        return None

    # Try numeric format first: DD.MM.YYYY
    m = _NUMERIC_DATE_RE.search(raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"

    # Try German month name: "15. April 2018" / "03. März 2022"
    m = _DE_MONTH_DATE_RE.search(raw)
    if m:
        d = int(m.group(1))
        # Normalise: lowercase, replace ä→a, ae→a so "März"/"Maerz" both → "marz"
        month_name = m.group(2).lower().replace("ä", "a").replace("ae", "a")
        y = int(m.group(3))
        mo = _DE_MONTHS.get(month_name)
        if mo and 1 <= d <= 31:
            return f"{y}-{mo:02d}-{d:02d}"

    return None


# ── Main function ─────────────────────────────────────────────────────────────

def parse_header(narrative_text) -> dict:
    """
    Parse metadata from the standardised Identifikation block at the top of a BFU report.

    Returns dict with keys:
        event_class   – str | None
        aircraft      – str | None  (Hersteller + Muster of first aircraft)
        registration  – always None (BFU does not include Kennzeichen in Identifikation)
        date_iso      – str | None  (YYYY-MM-DD)
        location      – str | None
        case_id       – str | None  (Aktenzeichen)
    """
    out = {
        "event_class": None,
        "aircraft": None,
        "registration": None,  # BFU does not publish registration in Identifikation
        "date_iso": None,
        "location": None,
        "case_id": None,
    }

    if not narrative_text:
        return out

    # Work within the first 1500 chars only
    window = narrative_text[:_HEADER_WINDOW]

    # ── Art des Ereignisses ───────────────────────────────────────────────────
    raw_event = _field(window, "Art des Ereignisses")
    if raw_event:
        out["event_class"] = _map_event_class(raw_event)

    # ── Datum ─────────────────────────────────────────────────────────────────
    raw_date = _field(window, "Datum")
    out["date_iso"] = _parse_date(raw_date)

    # ── Ort ───────────────────────────────────────────────────────────────────
    raw_ort = _field(window, "Ort")
    out["location"] = raw_ort if raw_ort else None

    # ── Aircraft: Hersteller + Muster ─────────────────────────────────────────
    # Multi-aircraft reports use "Luftfahrzeug 1" and "Luftfahrzeug 2".
    # We always take the FIRST aircraft (Hersteller / Muster after the first label).
    # Older reports use "Hersteller / Muster" as a combined label.
    hersteller = _field(window, "Hersteller")
    muster = _field(window, "Muster")

    if not hersteller:
        # Older combined label: "Hersteller / Muster: Piper / PA-34-220T Seneca V"
        combined = _field(window, "Hersteller / Muster")
        if combined:
            # Split on " / " to get manufacturer and type
            parts = combined.split(" / ", 1)
            hersteller = parts[0].strip() if parts else combined
            muster = parts[1].strip() if len(parts) > 1 else None

    parts = [p for p in [hersteller, muster] if p]
    if parts:
        out["aircraft"] = " ".join(parts)

    # ── Aktenzeichen ─────────────────────────────────────────────────────────
    raw_az = _field(window, "Aktenzeichen")
    if raw_az:
        # Strip any trailing whitespace; keep the case ID as-is (e.g. "BFU23-0022-1X")
        out["case_id"] = raw_az.strip()

    return out
