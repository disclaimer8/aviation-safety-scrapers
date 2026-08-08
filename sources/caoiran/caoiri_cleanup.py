#!/usr/bin/env python3
"""
caoiri_cleanup.py — dedup + date-fill + rebuild for caoiri.db
Run: python3 caoiri_cleanup.py [--selftest] [--dry-run]
"""

import sys, re, sqlite3, os

DB = os.path.expanduser("~/caoiran-ingest/caoiri.db")
NARRATIVE_FLOOR = 300
CORPUS_START = 2003
CORPUS_END = 2022

# ─── Jalali → Gregorian converter (pure Python, no pip deps) ─────────────────
# Standard algorithm: Borkowski / Roozbeh Pournader
# Jalali months: 6 months of 31 days, 5 months of 30 days, last month 29 or 30

_JM_DAYS = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]  # non-leap
_JM_DAYS_LEAP = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 30]

def _jalali_is_leap(jy):
    """Leap year check for Jalali calendar (algorithmic approximation)."""
    # Cycle of 2820 years; common approximation adequate for 1390-1401
    leaps = [1, 5, 9, 13, 17, 22, 26, 30]
    return (jy - 474) % 2820 % 128 % 29 < 4 or (jy % 4 == 0 and jy % 100 != 0) or jy % 400 == 0
    # Better: use the exact 2820-year cycle
    # Correct implementation below:

def _jalali_is_leap_exact(jy):
    """Exact Jalali leap year via 2820-cycle."""
    breaks = [
        -61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181,
        1210, 1635, 2060, 2097, 2192, 2263, 2324, 2394, 2456, 3178
    ]
    jy1 = jy - 474
    year = jy1 % 2820 + 474 + 38
    jp = (year + 30) * 682 % 2816
    if jp < 682:
        return True
    # Simpler alternative: standard single-formula
    return ((jy - 474) % 2820 + 474 + 38) * 682 % 2816 < 682

# Use a well-known simple approach valid for 1375-1410 (our corpus):
# Based on the algorithm from https://en.wikipedia.org/wiki/Solar_Hijri_calendar
def jalali_to_gregorian(jy, jm, jd):
    """Convert Jalali (Solar Hijri) date to Gregorian. Returns (year, month, day).

    Valid for modern era (1300-1410 Jalali = ~1921-2031 Gregorian).
    Algorithm: convert via Julian Day Number.
    """
    # From https://www.fourmilab.ch/documents/calendar/
    # Jalali epoch = JD 1948320.5 (but we use the common formula)

    # Step 1: Compute days since Jalali epoch
    # Each 4 years: 365*4+1 = 1461 days for first 6 cycles of 4 within 8-year block
    # Simpler: use the epoch-based algorithm

    # Jalali New Year (Nowruz) corresponds to:
    # 1 Farvardin 1 SH = JD 1948438.5
    # Use the algorithm:

    jy0 = jy - 979
    jm0 = jm - 1
    jd0 = jd - 1

    j_day_no = 365 * jy0 + (jy0 // 33) * 8 + (jy0 % 33 + 3) // 4
    for i in range(jm0):
        j_day_no += [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29][i]
    j_day_no += jd0

    # Gregorian
    g_day_no = j_day_no + 79

    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097

    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False

    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461

    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365

    g_days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    for i, days in enumerate(g_days_in_month):
        if g_day_no < days:
            gm = i + 1
            gd = g_day_no + 1
            break
        g_day_no -= days

    return gy, gm, gd


def jalali_to_gregorian_str(jy, jm, jd):
    """Return 'YYYY-MM-DD' string or None if conversion fails plausibility check."""
    try:
        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        if CORPUS_START <= gy <= CORPUS_END:
            return f"{gy}-{gm:02d}-{gd:02d}"
        return None
    except Exception:
        return None


# ─── Persian digit normalization ─────────────────────────────────────────────
_PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')

def normalize_persian(s):
    return s.translate(_PERSIAN_DIGITS)


# ─── Date extraction ─────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Jalali in narrative: 1394/07/23 or ۱۳۹۴/۰۷/۲۳ or 1394-07-23
# Use \d{1,2} for greedy day matching to avoid regex partial-match (e.g. matching '2' from '25')
_JALALI_SLASH_RE = re.compile(
    r'(1[34]\d{2})[/\-](\d{1,2})[/\-](\d{1,2})\b'
)
# case_id pattern: caoiri-aYYYYMMDD (Jalali YYYYMMDD where YYYY is 13xx)
_CASEID_JALALI_RE = re.compile(r'a(13\d{2})(\d{2})(\d{2})', re.IGNORECASE)

# EN date patterns
_EN_DATE_PATTERNS = [
    # YYYY-MM-DD  or YYYY/MM/DD  or YYYY.MM.DD
    re.compile(r'\b(20[0-2]\d)[.\-/](0?[1-9]|1[012])[.\-/](0?[1-9]|[12]\d|3[01])\b'),
    # DD Month YYYY  e.g. "18 Feb, 2018" or "18 February 2018"
    re.compile(
        r'\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December'
        r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[,.\s]+(\d{4})\b',
        re.IGNORECASE
    ),
    # Month DD, YYYY  e.g. "February 18, 2018" or "Nov. 25, 2018"
    re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December'
        r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?[,.\s]+(\d{4})\b',
        re.IGNORECASE
    ),
    # DD.Mon.YYYY  e.g. "24.Jul.2009"
    re.compile(
        r'\b(\d{1,2})\.(January|February|March|April|May|June|July|August|September|October|November|December'
        r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.(\d{4})\b',
        re.IGNORECASE
    ),
    # Oct. 15th 2015 style
    re.compile(
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December'
        r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?\.?\s+(\d{4})\b',
        re.IGNORECASE
    ),
]


def _parse_en_date(text):
    """Try all EN date patterns on text. Returns 'YYYY-MM-DD' or None."""
    for pat in _EN_DATE_PATTERNS:
        for m in pat.finditer(text):
            g = m.groups()
            try:
                if len(g) == 3:
                    # Pattern 1: YYYY, MM, DD (numeric)
                    if g[0].isdigit() and g[1].isdigit() and g[2].isdigit():
                        y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    # Pattern 2: DD, MonthName, YYYY
                    elif g[0].isdigit() and not g[1].isdigit() and g[2].isdigit():
                        d, mo_s, y = int(g[0]), g[1].lower()[:3], int(g[2])
                        mo = _MONTH_MAP.get(mo_s)
                        if mo is None:
                            continue
                    # Pattern 3: MonthName, DD, YYYY
                    elif not g[0].isdigit() and g[1].isdigit() and g[2].isdigit():
                        mo_s, d, y = g[0].lower()[:3], int(g[1]), int(g[2])
                        mo = _MONTH_MAP.get(mo_s)
                        if mo is None:
                            continue
                    else:
                        continue
                    if CORPUS_START <= y <= CORPUS_END and 1 <= mo <= 12 and 1 <= d <= 31:
                        return f"{y}-{mo:02d}-{d:02d}"
            except (ValueError, TypeError):
                continue
    return None


def _parse_jalali_in_text(text):
    """Look for Jalali dates in text (both ASCII and Persian digits). Returns Gregorian 'YYYY-MM-DD' or None."""
    normed = normalize_persian(text)
    for m in _JALALI_SLASH_RE.finditer(normed):
        jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1380 <= jy <= 1402 and 1 <= jm <= 12 and 1 <= jd <= 31:
            result = jalali_to_gregorian_str(jy, jm, jd)
            if result:
                return result
    return None


def extract_date(case_id, narrative, lang):
    """Multi-strategy date extraction. Returns 'YYYY-MM-DD' or None."""
    # Strategy A0: known-date overrides for rows where auto-extraction is impossible
    _KNOWN_DATES = {
        # EX-009: Itek Air B737-200 Manas 2008-08-24; narrative has "24.08.08" (YY format, undetectable)
        "caoiri-ex-009": "2008-08-24",
    }
    if case_id in _KNOWN_DATES:
        return _KNOWN_DATES[case_id]

    # Strategy A: case_id Jalali pattern (most reliable for caoiri-aYYYYMMDD)
    m = _CASEID_JALALI_RE.search(case_id)
    if m:
        jy, jm, jd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= jm <= 12 and 1 <= jd <= 31:
            result = jalali_to_gregorian_str(jy, jm, jd)
            if result:
                return result

    if not narrative:
        return None

    # Strategy B: EN date patterns in text
    # Try near "Date of Occurrence" label first
    doc_match = re.search(
        r'(?:Date\s+of\s+(?:Occurrence|occurrence)|Date\s+of\s+Accident|Date\s+and\s+Time)\s*[:\-]?\s*\n?\s*([^\n\r]{3,60})',
        narrative
    )
    if doc_match:
        context = doc_match.group(1).strip()
        d = _parse_en_date(context)
        if d:
            return d

    # Strategy C: EN dates anywhere in first 3000 chars of narrative
    d = _parse_en_date(narrative[:3000])
    if d:
        return d

    # Strategy D: Jalali in text (for FA reports)
    d = _parse_jalali_in_text(narrative[:3000])
    if d:
        return d

    return None


# ─── Self-test ────────────────────────────────────────────────────────────────

def selftest():
    print("=== Jalali converter self-test ===")
    cases = [
        ((1394, 7, 23), "2015-10-15"),   # EP-MNE
        ((1398, 10, 18), "2020-01-08"),  # PS752
        ((1396, 11, 29), "2018-02-18"),  # EP-ATS
        ((1397, 8, 25), "2018-11-16"),   # EP-TBC (approx)
        ((1380, 1, 1), None),             # Nowruz 2001 -- before corpus (2003), correctly rejected
        ((1399, 4, 16), "2020-07-06"),   # PS752 progress report date
    ]
    all_ok = True
    for (jy, jm, jd), expected in cases:
        result = jalali_to_gregorian_str(jy, jm, jd)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  {jy}/{jm:02d}/{jd:02d} -> {result} (expected {expected}) [{status}]")

    print("\n=== EN date regex self-test ===")
    en_cases = [
        ("18 Feb, 2018", "2018-02-18"),
        ("19 March 2019", "2019-03-19"),
        ("March 19, 2019", "2019-03-19"),
        ("16 May 2019", "2019-05-16"),
        ("Nov. 25, 2018", "2018-11-25"),
        ("24 Aug 2009", "2009-08-24"),
        ("24.Jul.2009", "2009-07-24"),
        ("Oct. 15th 2015", "2015-10-15"),
        ("2020-01-08", "2020-01-08"),
        ("June 6, 2019", "2019-06-06"),
    ]
    for text, expected in en_cases:
        result = _parse_en_date(text)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  '{text}' -> {result} (expected {expected}) [{status}]")

    print("\n=== case_id Jalali self-test ===")
    id_cases = [
        ("caoiri-a13940723epmne", "2015-10-15"),
        ("caoiri-a13961129epats", "2018-02-18"),
        ("caoiri-ex-009", "2008-08-24"),  # known-date override
    ]
    for cid, expected in id_cases:
        result = extract_date(cid, "", "en")
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  '{cid}' -> {result} (expected {expected}) [{status}]")

    # Also test Jalali in text with greedy day match
    print("\n=== Jalali-in-text self-test ===")
    jalali_text_cases = [
        ("حادثه مورخ 1397/08/25 اقدام", "2018-11-16"),  # EP-TBC
        (" 1394/07/23 ", "2015-10-15"),
        (" ۱۳۹۸/۱۰/۱۸ ", "2020-01-08"),  # Persian digits
    ]
    for text, expected in jalali_text_cases:
        result = _parse_jalali_in_text(text)
        status = "OK" if result == expected else "FAIL"
        if status == "FAIL":
            all_ok = False
        print(f"  '{text[:30]}' -> {result} (expected {expected}) [{status}]")

    return all_ok


# ─── Main cleanup logic ───────────────────────────────────────────────────────

# Dedup decisions (based on manual audit):
# Each entry: (keep_id, [delete_ids], fix_registration)
# fix_registration: if not None, update registration on the kept row

DEDUP_PLAN = [
    # EP-ATS cluster: 3 rows all same 2018-02-18 accident
    # Keep: caoiri-ep-ats (EN final report, 212828ch, 2021 Wayback, registration-based stable id)
    # Delete: caoiri-a13961129epats (EN interim 2019, 212617ch), caoiri-ep-ats-17a3 (FA preliminary)
    {
        "keep": "caoiri-ep-ats",
        "delete": ["caoiri-a13961129epats", "caoiri-ep-ats-17a3"],
        "fix_registration": None,
        "reason": "EP-ATS 2018-02-18 Yasouj: keep longest EN final (2021), delete EN interim + FA preliminary",
    },
    # UR-PSR / PS752 cluster: 6 rows all same 2020-01-08 accident
    # Keep: caoiri-ur-psr-fdc0 (EN final report, 404250ch, has date)
    # Delete: 30c3 (identical final, slightly shorter), ur-psr (EN 2nd prelim),
    #         6f08 (FA 2nd prelim), 9020 (EN 1st prelim), 6239e35f (progress report, no reg),
    #         ps752 (copy-protected, no text)
    {
        "keep": "caoiri-ur-psr-fdc0",
        "delete": [
            "caoiri-ur-psr-30c3",
            "caoiri-ur-psr",
            "caoiri-ur-psr-6f08",
            "caoiri-ur-psr-9020",
            "caoiri-6239e35f",
            "caoiri-ps752",
        ],
        "fix_registration": "UR-PSR",  # already set but confirm
        "reason": "PS752 UR-PSR 2020-01-08: keep largest EN final (fdc0 404250ch), delete 5 prelim/progress + copy-protected",
    },
    # IL-76TD/EP-PUS: same 2019-05-16 Yerevan runway excursion
    # Keep: caoiri-il-76td (EN, has date, 15884ch) but fix registration: IL-76TD -> EP-PUS
    # Delete: caoiri-ep-pus (FA, no date, 4512ch)
    {
        "keep": "caoiri-il-76td",
        "delete": ["caoiri-ep-pus"],
        "fix_registration": "EP-PUS",  # was wrongly set to model name IL-76TD
        "reason": "IL-76TD EP-PUS 2019-05-16 Yerevan: keep EN dated (fix reg to EP-PUS), delete FA undated",
    },
]

# Also: caoiri-3d3da018 has registration=None (RA-02772 Russian BAe125 at Mehrabad)
# The scraper failed to extract the registration from the FA text
# Fix: set registration = 'RA-02772'
REG_FIXES = {
    "caoiri-3d3da018": "RA-02772",
    # caoiri-il-62m has registration='IL-62M' (model), actual reg is UP-I6208
    "caoiri-il-62m": "UP-I6208",
}


def run_cleanup(dry_run=False):
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    print(f"=== caoiri cleanup {'[DRY RUN]' if dry_run else '[LIVE]'} ===\n")

    # --- Step 1: Dedup ---
    print("Step 1: Dedup clusters")
    total_deleted = 0
    for plan in DEDUP_PLAN:
        keep = plan["keep"]
        to_delete = plan["delete"]
        fix_reg = plan["fix_registration"]
        reason = plan["reason"]

        print(f"\n  Cluster: {reason}")

        # Verify keep row exists
        row = c.execute("SELECT case_id, registration, event_date, lang, length(narrative_text) as nlen FROM caoiri_reports WHERE case_id=?", (keep,)).fetchone()
        if not row:
            print(f"  [ERROR] Keep row '{keep}' NOT FOUND in DB!")
            continue
        print(f"  KEEP: {keep} [{row['lang']}] date={row['event_date']} nlen={row['nlen']} reg={row['registration']}")

        # Fix registration on kept row if needed
        if fix_reg and row['registration'] != fix_reg:
            print(f"  FIX registration: {row['registration']} -> {fix_reg}")
            if not dry_run:
                c.execute("UPDATE caoiri_reports SET registration=? WHERE case_id=?", (fix_reg, keep))

        # Delete the others
        for del_id in to_delete:
            del_row = c.execute("SELECT case_id, lang, length(narrative_text) as nlen FROM caoiri_reports WHERE case_id=?", (del_id,)).fetchone()
            if del_row:
                print(f"  DELETE: {del_id} [{del_row['lang']}] nlen={del_row['nlen']}")
                if not dry_run:
                    c.execute("DELETE FROM caoiri_reports WHERE case_id=?", (del_id,))
                    c.execute("DELETE FROM caoiri_accidents WHERE case_id=?", (del_id,))
                total_deleted += 1
            else:
                print(f"  SKIP DELETE: {del_id} (not found - already deleted?)")

    print(f"\n  Total deleted: {total_deleted}")

    # --- Step 2: Registration fixes ---
    print("\nStep 2: Registration fixes")
    for cid, new_reg in REG_FIXES.items():
        row = c.execute("SELECT registration FROM caoiri_reports WHERE case_id=?", (cid,)).fetchone()
        if row:
            old_reg = row['registration']
            print(f"  {cid}: {old_reg} -> {new_reg}")
            if not dry_run:
                c.execute("UPDATE caoiri_reports SET registration=? WHERE case_id=?", (new_reg, cid))
        else:
            print(f"  [SKIP] {cid} not found")

    # --- Step 3: Date extraction ---
    print("\nStep 3: Date extraction")
    rows = c.execute("""
        SELECT case_id, event_date, lang, narrative_text
        FROM caoiri_reports
        WHERE status IN ('ok', 'ok_no_text')
        ORDER BY case_id
    """).fetchall()

    date_filled = 0
    date_already = 0
    date_failed = 0

    for row in rows:
        cid = row['case_id']
        existing_date = row['event_date']
        lang = row['lang'] or 'en'
        narr = row['narrative_text'] or ''

        extracted = extract_date(cid, narr, lang)

        if existing_date:
            date_already += 1
            # Verify consistency if we also extracted
            if extracted and extracted != existing_date:
                print(f"  [MISMATCH] {cid}: existing={existing_date} extracted={extracted} (keeping existing)")
            else:
                print(f"  OK  {cid}: {existing_date}")
        elif extracted:
            date_filled += 1
            print(f"  FILL {cid}: {extracted} (from text/case_id)")
            if not dry_run:
                c.execute("UPDATE caoiri_reports SET event_date=? WHERE case_id=?", (extracted, cid))
        else:
            date_failed += 1
            print(f"  MISS {cid}: no date extracted [{lang}]")

    print(f"\n  Already dated: {date_already}")
    print(f"  Newly filled:  {date_filled}")
    print(f"  Still missing: {date_failed}")

    if not dry_run:
        c.commit()

    # --- Step 4: Rebuild caoiri_accidents ---
    print("\nStep 4: Rebuild caoiri_accidents")
    if not dry_run:
        c.execute("DELETE FROM caoiri_accidents")
        rows = c.execute("""
            SELECT * FROM caoiri_reports
            WHERE status IN ('ok', 'ok_no_text')
              AND (
                (narrative_text IS NOT NULL AND length(narrative_text) >= ?)
                OR status = 'ok_no_text'
              )
            ORDER BY event_date
        """, (NARRATIVE_FLOOR,)).fetchall()

        built = 0
        import time
        now_ms = int(time.time() * 1000)
        for row in rows:
            if not row['narrative_text'] and row['status'] != 'ok_no_text':
                continue
            reg = (row['registration'] or 'unknown').lower().replace('/', '-')
            date = (row['event_date'] or 'undated').replace('-', '')[:8]
            site_slug = f"caoiri-{reg}-{date}"

            c.execute("""
                INSERT OR REPLACE INTO caoiri_accidents
                (case_id, event_date, aircraft, registration, operator, location,
                 country, narrative_text, probable_cause, source_url, report_type,
                 site_slug, lang, built_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                row['case_id'], row['event_date'], row['aircraft'],
                row['registration'], row['operator'], row['location'],
                'IR', row['narrative_text'], None,
                row['source_url'], row['report_type'],
                site_slug, row['lang'], now_ms,
            ))
            built += 1
        c.commit()
        print(f"  Built {built} rows in caoiri_accidents")

    # --- Step 5: Verification ---
    print("\nStep 5: Verification")

    total = c.execute("SELECT count(*) FROM caoiri_reports").fetchone()[0]
    ok = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='ok'").fetchone()[0]
    ok_no_text = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='ok_no_text'").fetchone()[0]
    skip = c.execute("SELECT count(*) FROM caoiri_reports WHERE status='skip'").fetchone()[0]
    dated = c.execute("SELECT count(*) FROM caoiri_reports WHERE status IN ('ok','ok_no_text') AND event_date IS NOT NULL").fetchone()[0]
    undated = c.execute("SELECT count(*) FROM caoiri_reports WHERE status IN ('ok','ok_no_text') AND event_date IS NULL").fetchone()[0]
    en_cnt = c.execute("SELECT count(*) FROM caoiri_reports WHERE lang='en' AND status='ok'").fetchone()[0]
    fa_cnt = c.execute("SELECT count(*) FROM caoiri_reports WHERE lang='fa' AND status='ok'").fetchone()[0]
    above_floor = c.execute(f"SELECT count(*) FROM caoiri_reports WHERE status='ok' AND length(narrative_text) >= {NARRATIVE_FLOOR}").fetchone()[0]
    built = c.execute("SELECT count(*) FROM caoiri_accidents").fetchone()[0]

    print(f"\n  reports total:    {total}")
    print(f"  ok (with text):   {ok}")
    print(f"  ok (no text):     {ok_no_text}")
    print(f"  skipped:          {skip}")
    print(f"  EN narratives:    {en_cnt}")
    print(f"  FA narratives:    {fa_cnt}")
    print(f"  >= {NARRATIVE_FLOOR}ch:          {above_floor}")
    print(f"  dated:            {dated}")
    print(f"  undated:          {undated}")
    print(f"  built accidents:  {built}")

    print("\n  All rows (sorted by date):")
    rows = c.execute("""
        SELECT case_id, registration, event_date, lang, status, length(narrative_text) as nlen
        FROM caoiri_reports WHERE status IN ('ok','ok_no_text','skip')
        ORDER BY event_date, case_id
    """).fetchall()
    for r in rows:
        nlen = r['nlen'] or 0
        above = ">=" if nlen >= NARRATIVE_FLOOR else "< "
        print(f"    {r['case_id']:40s} {r['registration'] or '?':12s} {r['event_date'] or '?':12s} [{r['lang'] or '?'}] {r['status']:12s} {above}{NARRATIVE_FLOOR}ch ({nlen})")

    # CRITICAL: Registration+date dup check — must be empty
    print("\n  === Dup check (registration+date GROUP BY HAVING count>1) ===")
    dups = c.execute("""
        SELECT registration, event_date, count(*) as cnt
        FROM caoiri_reports
        WHERE status IN ('ok','ok_no_text')
          AND registration IS NOT NULL
          AND event_date IS NOT NULL
        GROUP BY registration, event_date
        HAVING cnt > 1
    """).fetchall()
    if dups:
        print(f"  [FAIL] Found {len(dups)} dup clusters!")
        for d in dups:
            print(f"    reg={d['registration']} date={d['event_date']} count={d['cnt']}")
    else:
        print("  PASS: No registration+date duplicates found")

    print("\nDone.")


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--selftest" in args:
        ok = selftest()
        sys.exit(0 if ok else 1)

    dry_run = "--dry-run" in args
    run_cleanup(dry_run=dry_run)
