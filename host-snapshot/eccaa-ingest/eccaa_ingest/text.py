# eccaa_ingest/text.py
import html
import re

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONSLUG = re.compile(r"[^a-z0-9]+")


def strip_html(s):
    if not s:
        return ""
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def slugify(s):
    if not s:
        return ""
    return _NONSLUG.sub("-", s.lower()).strip("-")


def make_site_slug(aircraft, registration, location):
    parts = [p for p in (aircraft, registration, location) if p]
    base = slugify(" ".join(parts))
    return f"crash-{base}" if base else "crash-eccaa"


# ── Multi-country derivation ───────────────────────────────────────────────────
# The Eastern Caribbean CAA is shared by 6 OECS states. The country of the
# *aircraft state of registry* is derived from the registration prefix. The 6
# OECS member registries:
#   V2-  Antigua & Barbuda           -> AG
#   J3-  Grenada                     -> GD
#   J6-  Saint Lucia                 -> LC
#   J7-  Dominica                    -> DM
#   J8-  Saint Vincent & Grenadines  -> VC
#   V4-  Saint Kitts & Nevis         -> KN
# Foreign-registered aircraft (an accident in EC airspace investigated by ECCAA)
# carry their own registry country (e.g. N- US, G- GB, VP-M Montserrat,
# YV Venezuela). Default fallback when nothing matches: 'AG' (ECCAA HQ state).

# Longest prefixes first so V2-/V4- win over a bare 'V', J3.. over 'J'.
_OECS_PREFIXES = [
    ("V2-", "AG"),  # Antigua & Barbuda
    ("V4-", "KN"),  # Saint Kitts & Nevis
    ("J3-", "GD"),  # Grenada
    ("J6-", "LC"),  # Saint Lucia
    ("J7-", "DM"),  # Dominica
    ("J8-", "VC"),  # Saint Vincent & the Grenadines
]

# Common foreign registries observed on ECCAA final reports. Checked only after
# OECS prefixes. Ordered longest-first.
_FOREIGN_PREFIXES = [
    ("VP-M", "MS"),  # Montserrat
    ("VQ-", "GB"),   # UK overseas territories (generic)
    ("VP-", "GB"),   # UK overseas territories (generic)
    ("N", "US"),     # United States
    ("G-", "GB"),    # United Kingdom
    ("YV", "VE"),    # Venezuela
    ("C-", "CA"),    # Canada
    ("PJ-", "NL"),   # Sint Maarten / Curacao (Dutch)
    ("8P-", "BB"),   # Barbados
    ("9Y-", "TT"),   # Trinidad & Tobago
    ("6Y-", "JM"),   # Jamaica
    ("HK-", "CO"),   # Colombia
]

DEFAULT_COUNTRY = "AG"


def country_from_registration(registration):
    """Map an aircraft registration to an ISO-2 country code.

    OECS member-state prefixes resolve to the six ECCAA states; common foreign
    prefixes resolve to their state of registry; anything unrecognised (or a
    missing registration) returns the ECCAA-HQ default 'AG'.
    """
    if not registration:
        return DEFAULT_COUNTRY
    reg = registration.strip().upper()
    for prefix, cc in _OECS_PREFIXES:
        if reg.startswith(prefix):
            return cc
    for prefix, cc in _FOREIGN_PREFIXES:
        if reg.startswith(prefix):
            return cc
    return DEFAULT_COUNTRY
