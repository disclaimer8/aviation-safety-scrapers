# bea_ingest/header.py
"""
PDF-header metadata parser for BEA accident reports.

Every BEA report's narrative_text (pdftotext output) starts with a
standardised header in one of three families:

  (A) English "SAFETY/INVESTIGATION REPORT" courtesy translations:
      ... Accident to the <TYPE> registered|identified <REG>
          on <D Month YYYY> at|in <LOCATION> (dept)

  (B) French flowing:
      ... Accident de l'<...> immatriculé|identifié <REG>
          survenu le <D mois YYYY> à <LOCATION> (dept)

  (C) French TABULAR (older "RAPPORT ACCIDENT"):
      <Avion|Planeur|…> <TYPE> immatriculé <REG>
      Date et heure <D mois YYYY> à <time>
      Lieu <LOCATION>

parse_header(narrative_text) -> dict(aircraft, registration, date_iso, location)
All values are str | None.  Only the first ~800 chars are searched.
"""
import re

_HEADER_WINDOW = 800

# ── month tables ──────────────────────────────────────────────────────────────
_FR_MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2,
    "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
}
_EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# ── compiled regexes ──────────────────────────────────────────────────────────

# French date: "25 août 2019", "7 avril 2018", "1er janvier 2015"
_FR_DATE_RE = re.compile(
    r"(\d{1,2})(?:er)?\s+"
    r"(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|"
    r"septembre|octobre|novembre|d[eé]cembre)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)

# English date: "24 September 2023", "19 June 2022"
_EN_DATE_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)

# Registration after immatriculé/identifié/registered/identified.
# Stops at whitespace+keyword, whitespace+digit (French tabular date follows directly),
# double-space, comma, or end.
_REG_RE = re.compile(
    r"(?:immatricul[eé]e?|identifi[eé]e?|registered|identified)"
    r"\s+([A-Z0-9][A-Z0-9\-]+)"
    r"(?=\s+(?:\d|survenu|on\b|le\b|Date\b|$|,)|\s{2,}|,|\.|$)",
    re.IGNORECASE,
)

# English aircraft: between "to the " and " registered|identified"
_EN_AC_RE = re.compile(
    r"\bto the\s+(.+?)\s+(?:registered|identified)\b",
    re.IGNORECASE,
)

# French flowing aircraft: the noun phrase right before "immatriculé|identifié"
# Anchored after common French event/preposition keywords.
_FR_FLOWING_AC_RE = re.compile(
    r"(?:"
    r"(?:Accident|[ÉEe]v[eé]nement|Incident)\s+(?:de l'|du |de la |de |à bord du |à bord de l')"
    r"|à bord du |à bord de l'"
    r")"
    r"(.+?)\s+(?:immatricul[eé]e?|identifi[eé]e?)",
    re.IGNORECASE,
)

# French tabular category words that may prefix the type
_TABULAR_CATS = r"(?:Avion|Planeur|Ballon|ULM|H[eé]licopt[eè]re|Hydravion|Autogire|Paramoteur|Multiaxe)\s+"

# French tabular aircraft: optional category + type name right before immatriculé
_FR_TAB_AC_RE = re.compile(
    r"(?:" + _TABULAR_CATS + r")?"
    r"([A-Z][A-Za-z0-9 \-\.]+?)\s+immatricul[eé]e?",
    re.IGNORECASE,
)

# English location: after the date, "at|in <loc>"
_EN_LOC_RE = re.compile(
    r"(?:at|in)\s+([^()\n]+?)(?:\s*\([^)]*\)|\s{2,}|$)",
    re.IGNORECASE,
)

# French flowing location: after "survenu le ... à <loc>"
_FR_FL_LOC_RE = re.compile(
    r"survenu le\s+.+?\s+à\s+(.+?)(?:\s*\(\d+\)|\s{2,}|$)",
    re.IGNORECASE,
)

# French tabular location: after "Lieu "
_FR_TAB_LOC_RE = re.compile(
    r"Lieu\s+(.+?)(?:,|\(|altitude|\s{2,}|Nature|$)",
    re.IGNORECASE,
)

# ── helpers ───────────────────────────────────────────────────────────────────

def _clean_reg(raw: str) -> str:
    """Collapse internal spaces (OCR artefact) and uppercase."""
    # Remove spaces between letters/digits that look like a single token.
    # E.g. "F- M120 HEXT" should stay as "F-M120HEXT" is too aggressive;
    # just strip leading/trailing spaces and collapse inner spaces cautiously:
    # keep the raw value but strip surrounding whitespace.
    return raw.strip()


def _fr_date(m) -> str:
    d, mon, y = m.group(1), m.group(2).lower(), m.group(3)
    # normalise accented variants
    mon = mon.replace("é", "e").replace("è", "e").replace("û", "u").replace("ô", "o")
    mo = _FR_MONTHS.get(mon) or _FR_MONTHS.get(m.group(2).lower())
    if mo is None:
        return None
    return f"{y}-{mo:02d}-{int(d):02d}"


def _en_date(m) -> str:
    d, mon, y = m.group(1), m.group(2).lower(), m.group(3)
    mo = _EN_MONTHS.get(mon)
    if mo is None:
        return None
    return f"{y}-{mo:02d}-{int(d):02d}"


def _strip_dept(s: str) -> str:
    """Strip trailing (dept-number) like (26) or (Corrèze)."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()


# ── main function ─────────────────────────────────────────────────────────────

def parse_header(narrative_text) -> dict:
    """
    Parse metadata from the standardised header at the start of a BEA narrative.

    Returns dict with keys: aircraft, registration, date_iso, location.
    All values are str | None.
    """
    out = {"aircraft": None, "registration": None, "date_iso": None, "location": None}

    if not narrative_text:
        return out

    # Work within the first 800 chars only
    window = narrative_text[:_HEADER_WINDOW]

    # ── determine header family ───────────────────────────────────────────────
    # English if it contains "to the" + "registered|identified", OR English month names
    is_english = bool(
        re.search(r"\bto the\b.{0,80}(?:registered|identified)\b", window, re.IGNORECASE)
        or re.search(
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\b",
            window,
        )
    )

    # ── registration ─────────────────────────────────────────────────────────
    rm = _REG_RE.search(window)
    if rm:
        raw_reg = _clean_reg(rm.group(1))
        # Strip any trailing noise: take the longest prefix that looks like a reg
        # (letters, digits, dash, no space-then-word that's a keyword)
        raw_reg = re.split(r"\s+(?:survenu|on\b|le\b|Date\b)", raw_reg, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        # Strip trailing comma (s12: "F-CEPP,")
        raw_reg = raw_reg.rstrip(",").strip()
        # Collapse internal spaces from OCR (e.g. "F- HEXT" → "F-HEXT")
        raw_reg = re.sub(r"(?<=[A-Z0-9])\s+(?=[A-Z0-9])", "", raw_reg)
        out["registration"] = raw_reg.upper() if raw_reg else None

    # ── date_iso ──────────────────────────────────────────────────────────────
    if is_english:
        # English date
        em = _EN_DATE_RE.search(window)
        if em:
            out["date_iso"] = _en_date(em)
        # fallback: French date (bilingual reports sometimes have both)
        if not out["date_iso"]:
            fm = _FR_DATE_RE.search(window)
            if fm:
                out["date_iso"] = _fr_date(fm)
    else:
        # French date — prefer the one near "survenu le" or "Date et heure"
        # Search all occurrences and take the one closest after an anchor
        best_date = None
        best_pos = len(window) + 1

        for anchor_re in (
            re.compile(r"survenu le\s+", re.IGNORECASE),
            re.compile(r"Date et heure\s+", re.IGNORECASE),
        ):
            am = anchor_re.search(window)
            if am:
                # look for a French date starting from anchor position
                fm = _FR_DATE_RE.search(window, am.end())
                if fm and fm.start() < best_pos:
                    best_pos = fm.start()
                    best_date = _fr_date(fm)

        if best_date is None:
            # Fallback: first French date anywhere in the window
            fm = _FR_DATE_RE.search(window)
            if fm:
                best_date = _fr_date(fm)

        out["date_iso"] = best_date

    # ── aircraft ─────────────────────────────────────────────────────────────
    if is_english:
        acm = _EN_AC_RE.search(window)
        if acm:
            ac = acm.group(1).strip()
            # Remove "(1)" footnote markers
            ac = re.sub(r"\s*\(\d+\)\s*", " ", ac).strip()
            out["aircraft"] = ac if ac else None
        else:
            # Fallback for "unidentified" aircraft: "to the <type> on <date>"
            fb = re.search(r"\bto the\s+(.+?)\s+on\s+\d", window, re.IGNORECASE)
            if fb:
                ac = fb.group(1).strip()
                # Strip leading "unidentified " qualifier
                ac = re.sub(r"^unidentified\s+", "", ac, flags=re.IGNORECASE)
                # Strip trailing equipment descriptions ("equipped with ...")
                ac = re.sub(r"\s+equipped\s+.*$", "", ac, flags=re.IGNORECASE).strip()
                out["aircraft"] = ac if ac else None
    else:
        # Try French flowing first
        acm = _FR_FLOWING_AC_RE.search(window)
        if acm:
            ac = acm.group(1).strip()
            ac = re.sub(r"\s*\(\d+\)\s*", " ", ac).strip()
            out["aircraft"] = ac if ac else None
        else:
            # French tabular: find the word(s) between category/start and "immatriculé"
            # Strategy: find "immatriculé" and look backwards for the type name
            imm = re.search(r"\bimmatricul[eé]e?\b", window, re.IGNORECASE)
            if imm:
                before = window[:imm.start()].rstrip()
                # Try to match a tabular aircraft line: optional category + type
                # The type runs from just after the last newline/category word
                # Find last category word occurrence before imm
                cat_m = list(re.finditer(_TABULAR_CATS, before, re.IGNORECASE))
                if cat_m:
                    # text after the last category word = type
                    last_cat = cat_m[-1]
                    ac = before[last_cat.end():].strip()
                    # Remove footnote refs and trailing commas
                    ac = re.sub(r"\s*\(\d+\).*$", "", ac).strip().rstrip(",").strip()
                    out["aircraft"] = ac if ac else None
                else:
                    # No category word; take the last token-run before immatriculé
                    # after any newline or report header noise
                    # Split on whitespace/newlines and take the last meaningful word(s)
                    before_clean = re.sub(r"\s+", " ", before).strip()
                    # Remove report-header prefixes
                    before_clean = re.sub(
                        r"^.*?(?:RAPPORT ACCIDENT|RAPPORT D'ENQUÊTE|RAPPORT|www\.bea\.aero)\s*",
                        "",
                        before_clean,
                        flags=re.IGNORECASE,
                    ).strip()
                    # Strip trailing footnote ref
                    before_clean = re.sub(r"\s*\(\d+\).*$", "", before_clean).strip()
                    out["aircraft"] = before_clean if before_clean else None

    # ── location ─────────────────────────────────────────────────────────────
    if is_english:
        # Find date position first, then search for "at|in" after it
        em = _EN_DATE_RE.search(window)
        if em:
            after_date = window[em.end():]
            lm = _EN_LOC_RE.search(after_date)
            if lm:
                loc = lm.group(1).strip()
                loc = _strip_dept(loc)
                # Strip trailing ", in the commune of X" etc.
                loc = re.sub(r",\s*in the commune of.*$", "", loc, flags=re.IGNORECASE).strip()
                out["location"] = loc if loc else None
    else:
        # Try French flowing "survenu le ... à <loc>"
        lm = _FR_FL_LOC_RE.search(window)
        if lm:
            loc = lm.group(1).strip()
            loc = _strip_dept(loc)
            out["location"] = loc if loc else None
        else:
            # French tabular "Lieu <loc>"
            lm = _FR_TAB_LOC_RE.search(window)
            if lm:
                loc = lm.group(1).strip()
                loc = _strip_dept(loc)
                out["location"] = loc if loc else None

    return out
