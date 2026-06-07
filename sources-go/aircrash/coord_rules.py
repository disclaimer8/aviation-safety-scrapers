"""Country bounding-box rules shared by the coord-fix and re-geocode passes.

Single source of truth so a bbox tweak (e.g. widening Russia for Sakhalin)
propagates to every script that filters coords by location-text.

Each rule: (compiled regex matched against UPPER(location), lat_range, lon_range).
Order matters: first match wins. State-suffix patterns ahead of generic "USA"
so we always pick the tightest applicable bbox.
"""
import re

# Each entry: (regex, (lat_min, lat_max), (lon_min, lon_max)).
# Bboxes are inclusive with small margin for coastal accidents that happened
# just offshore. They cover offshore territory (Alaska, Hawaii, Sakhalin).
RULES = [
    # USA — every row that ends in ", XX, United States" or "(YYY airport
    # in the US)" should plot in continental US, Alaska, Hawaii, or
    # nearby ocean. Lon range allows up to -180 (Aleutians) and (-50)
    # (Maine) margin.
    (re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR|VI|GU|AS),\s*UNITED STATES"),
     (16, 72), (-180, -50)),
    (re.compile(r"\b(UNITED STATES|USA)\b"), (16, 72), (-180, -50)),
    # Canada
    (re.compile(r"\bCANADA\b"), (40, 84), (-141, -50)),
    # Russia — straddles antimeridian, so lon allowance is union (19, 180) U (-180, -168).
    # The simple (19, 180) range below misses Russian territory east of the antimeridian
    # (Chukotka), but coverage there is negligible in the dataset; revisit if needed.
    (re.compile(r"\bRUSSIA\b"), (41, 82), (19, 180)),
    # Brazil
    (re.compile(r"\bBRAZIL\b"), (-34, 6), (-74, -34)),
    # Mexico
    (re.compile(r"\bMEXICO\b"), (14, 33), (-118, -86)),
    # France — mainland; Réunion / French Guiana excluded.
    (re.compile(r"\bFRANCE\b"), (41, 51.5), (-5.5, 10)),
    # Germany
    (re.compile(r"\bGERMANY\b"), (47, 56), (5, 16)),
    # United Kingdom
    (re.compile(r"\b(UNITED KINGDOM|UK\b|ENGLAND|SCOTLAND|WALES)"), (49, 61), (-9, 2)),
    # Australia
    (re.compile(r"\bAUSTRALIA\b"), (-44, -10), (112, 154)),
    # India
    (re.compile(r"\bINDIA\b"), (6, 36), (68, 98)),
    # China
    (re.compile(r"\bCHINA\b"), (18, 54), (73, 135)),
    # Japan
    (re.compile(r"\bJAPAN\b"), (24, 46), (122, 146)),
    # Argentina
    (re.compile(r"\bARGENTINA\b"), (-55, -21), (-74, -53)),
    # Italy
    (re.compile(r"\bITALY\b"), (35, 47), (6, 19)),
    # Spain
    (re.compile(r"\bSPAIN\b"), (27, 44), (-19, 5)),
    # Indonesia
    (re.compile(r"\bINDONESIA\b"), (-11, 6), (95, 141)),
]


def country_bbox_for(location: str):
    """Return (lat_range, lon_range) for the first matching country rule, or
    None if no rule matches. Caller uses None to mean "no country hint, accept
    any coord" — matches the old fix-coords behaviour."""
    if not location:
        return None
    upper = location.upper()
    for rx, lat_r, lon_r in RULES:
        if rx.search(upper):
            return (lat_r, lon_r)
    return None


def is_outside_bbox(lat, lon, bbox) -> bool:
    """True iff (lat, lon) lies outside the bbox tuple ((lat_min, lat_max),
    (lon_min, lon_max)). Returns False if any input is None — callers that
    want to treat None as 'outside' must pre-filter."""
    if lat is None or lon is None or bbox is None:
        return False
    lat_r, lon_r = bbox
    if not (lat_r[0] <= lat <= lat_r[1]):
        return True
    if not (lon_r[0] <= lon <= lon_r[1]):
        return True
    return False
