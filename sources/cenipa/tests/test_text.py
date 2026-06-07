from cenipa_ingest.text import strip_html, slugify, make_site_slug, parse_event_title, fr_date_to_iso

def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"

def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""

def test_slugify_basic():
    assert slugify("Leonardo AW139") == "leonardo-aw139"
    assert slugify("  G-CIMU!! ") == "g-cimu"

def test_make_site_slug_combines_parts():
    assert make_site_slug("Leonardo AW139", "G-CIMU", "Norwich Airport") == "crash-leonardo-aw139-g-cimu-norwich-airport"

def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "G-ABCD", None) == "crash-g-abcd"
    assert make_site_slug(None, None, None) == "crash-cenipa"

def test_parse_event_title_full():
    r = parse_event_title("Accident to the Cessna 208 registered F-HFDZ on 24/05/2026 at Frétoy-le-Château AD")
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "Cessna 208"
    assert r["registration"] == "F-HFDZ"
    assert r["date_iso"] == "2026-05-24"
    assert "Frétoy-le-Château" in r["location"]
    assert r["operator"] is None

def test_parse_event_title_serious_incident():
    r = parse_event_title("Serious incident to the Airbus A320 registered F-XXXX on 03/02/2019 at Paris")
    assert r["event_class"] == "Serious incident"
    assert r["aircraft_type"] == "Airbus A320"
    assert r["registration"] == "F-XXXX"
    assert r["date_iso"] == "2019-02-03"
    assert r["operator"] is None

def test_parse_event_title_unparseable():
    r = parse_event_title("Some weird title")
    assert r["registration"] is None and r["date_iso"] is None
    assert r["event_class"] is None
    assert r["operator"] is None

def test_fr_date_to_iso():
    assert fr_date_to_iso("24/05/2026") == "2026-05-24"
    assert fr_date_to_iso("") is None
    assert fr_date_to_iso("garbage") is None

def test_slugify_and_site_slug_preserved():
    assert slugify("Boeing 737!!") == "boeing-737"
    assert make_site_slug("Cessna 208", "F-HFDZ", "Frétoy").startswith("crash-")

# ── New cases: richer title patterns ──

def test_parse_event_title_with_operator():
    """Title with 'operated by <OPERATOR>' captures operator."""
    r = parse_event_title(
        "Serious incident to the Airbus A330 registered TC-LOL operated by Turkish Airlines "
        "on 31/12/2019 at Port Harcourt, Nigeria [Investigation led by BFU / Germany]"
    )
    assert r["event_class"] == "Serious incident"
    assert r["aircraft_type"] == "Airbus A330"
    assert r["registration"] == "TC-LOL"
    assert r["operator"] == "Turkish Airlines"
    assert r["date_iso"] == "2019-12-31"
    # location must not include the trailing bracket
    assert "[Investigation" not in r["location"]
    assert "Port Harcourt" in r["location"]

def test_parse_event_title_identified():
    """Microlight titles use 'identified' instead of 'registered'."""
    r = parse_event_title(
        "Accident to the Zlin Savage identified 05NJ on 29/11/2019 at Aroza "
        "[Investigation led by STSB / Switzerland]"
    )
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "Zlin Savage"
    assert r["registration"] == "05NJ"
    assert r["operator"] is None
    assert r["date_iso"] == "2019-11-29"
    assert "[Investigation" not in r["location"]
    assert "Aroza" in r["location"]

def test_parse_event_title_near_location():
    """Location can be prefixed with 'near'."""
    r = parse_event_title(
        "Serious incident to the Embraer ERJ190 registered B-3203 on 23/12/2019 "
        "near New Chitose [Investigation led by JTSB / Japan]"
    )
    assert r["event_class"] == "Serious incident"
    assert r["registration"] == "B-3203"
    assert "New Chitose" in r["location"]
    assert "[Investigation" not in r["location"]

def test_parse_event_title_off_location():
    """Location can be prefixed with 'off'."""
    r = parse_event_title(
        "Accident to the Boeing 737 registered G-ABCD on 15/03/2020 off the coast of France"
    )
    assert r["event_class"] == "Accident"
    assert r["registration"] == "G-ABCD"
    assert "coast of France" in r["location"]

def test_parse_event_title_close_to():
    """Location prefixed with 'close to'."""
    r = parse_event_title(
        "Incident to the Airbus A318 - 100 registered F-GUGD operated by Air France "
        "on 20/12/2019 close to Hyères-Le Palyvestre airport (Le Var)"
    )
    assert r["event_class"] == "Incident"
    assert r["registration"] == "F-GUGD"
    assert r["operator"] == "Air France"
    assert "Hyères-Le Palyvestre" in r["location"]

def test_parse_event_title_en_route():
    """Location 'en route' without a specific place."""
    r = parse_event_title(
        "Serious incident to the Airbus A320 registered XA-VAR operated by Aeroenlances Nacionales "
        "on 29/12/2019 en route [Investigation led by AIB / Mexico]"
    )
    assert r["event_class"] == "Serious incident"
    assert r["registration"] == "XA-VAR"
    assert r["operator"] == "Aeroenlances Nacionales"
    assert "en route" in r["location"].lower()
    assert "[Investigation" not in r["location"]

def test_parse_event_title_investigation_stripped_from_location():
    """[Investigation led by ...] trailer is stripped; location is clean."""
    r = parse_event_title(
        "Accident to the helicopter Airbus AS350 registered N985SA "
        "on 26/12/2019 at Kauai Island, Hawai [Investigation led by NTSB / United States]"
    )
    assert r["location"] == "Kauai Island, Hawai"
    assert r["operator"] is None

def test_parse_event_title_operator_no_bracket():
    """operator captured, no bracket in location when no investigation trailer."""
    r = parse_event_title(
        "Accident to the Airbus A320 registered B-50001 operated by Tigerair Taiwan "
        "on 25/12/2019 en route [Investigation led by JTSB / Japan]"
    )
    assert r["operator"] == "Tigerair Taiwan"
    assert "[Investigation" not in r["location"]


# ── New failing examples from the full backfill (must all parse after fix) ───

def test_parse_event_title_jodel_on_the_location():
    """'on the <location>' prefix — previously fell through to null."""
    r = parse_event_title(
        "Accident to the Jodel D140 registered F-BMFV on 28/02/2025 "
        "on the Vallée Blanche glacier"
    )
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "Jodel D140"
    assert r["registration"] == "F-BMFV"
    assert r["date_iso"] == "2025-02-28"
    assert "Vallée Blanche" in r["location"]
    assert r["operator"] is None


def test_parse_event_title_en_route_no_place():
    """'en route' as entire location (no following place name)."""
    r = parse_event_title(
        "Incident to the Airbus A321 registered PH-YHA operated by Transavia Airlines "
        "on 13/01/2025, en route"
    )
    assert r["event_class"] == "Incident"
    assert r["aircraft_type"] == "Airbus A321"
    assert r["registration"] == "PH-YHA"
    assert r["operator"] == "Transavia Airlines"
    assert r["date_iso"] == "2025-01-13"
    # location is "en route" or empty — either is acceptable
    assert r["location"] is not None
    assert "investigation" not in (r["location"] or "").lower()


def test_parse_event_title_multi_aircraft_takes_first():
    """Multi-aircraft title: first aircraft+reg extracted, date+location correct."""
    r = parse_event_title(
        "Accident to the Robin DR400 registered F-GLDN and the fixed-wing microlight "
        "ATEC 122 Zéphyr identified 44APT on 29/11/2024 on Lunéville - Croismare AD"
    )
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "Robin DR400"
    assert r["registration"] == "F-GLDN"
    assert r["date_iso"] == "2024-11-29"
    assert "Lunéville" in r["location"]


def test_parse_event_title_2digit_year():
    """2-digit year (YY) is normalised to 2000+YY when plausible."""
    r = parse_event_title(
        "Accident to the Cessna 210K registered N5767J on 25/07/24 at Beblenheim"
    )
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "Cessna 210K"
    assert r["registration"] == "N5767J"
    assert r["date_iso"] == "2024-07-25"
    assert r["location"] == "Beblenheim"


def test_parse_event_title_2digit_year_with_operator():
    """2-digit year + operator captured correctly."""
    r = parse_event_title(
        "Incident to the Embraer ERJ170 registered F-HBXI operated by Hop! "
        "on 11/05/24 at Toulouse"
    )
    assert r["event_class"] == "Incident"
    assert r["aircraft_type"] == "Embraer ERJ170"
    assert r["registration"] == "F-HBXI"
    assert r["operator"] == "Hop!"
    assert r["date_iso"] == "2024-05-11"
    assert r["location"] == "Toulouse"


def test_parse_event_title_identified_microlight_2digit_year():
    """Microlight with 'identified' keyword and 2-digit year."""
    r = parse_event_title(
        "Accident to the fixed-wing microlight WT9 DYNAMIC CLUB identified 67BVN "
        "on 12/04/24 at Peynier"
    )
    assert r["event_class"] == "Accident"
    assert r["aircraft_type"] == "fixed-wing microlight WT9 DYNAMIC CLUB"
    assert r["registration"] == "67BVN"
    assert r["date_iso"] == "2024-04-12"
    assert r["location"] == "Peynier"


def test_parse_event_title_british_airways_operator_clean_location():
    """Operator extracted; location clean (no bracket, no country)."""
    r = parse_event_title(
        "Serious incident to the Airbus A320 registered G-EUUW and operated by "
        "British Airways on 30/12/2017 near Geneva [Investigation led by SESA - Switzerland]"
    )
    assert r["event_class"] == "Serious incident"
    assert r["registration"] == "G-EUUW"
    assert r["operator"] == "British Airways"
    assert r["date_iso"] == "2017-12-30"
    assert "Geneva" in r["location"]
    assert "[Investigation" not in r["location"]
