# tests/test_header.py
"""
14 real BEA header samples – tests for parse_header().
Minimum bar: date_iso correct for ≥13/14, registration correct for ≥11/14.
"""
import pytest
from bea_ingest.header import parse_header

# Each tuple: (label, header_text, exp_date, exp_reg, aircraft_substr, loc_substr_or_None)
SAMPLES = [
    (
        "s1 English registered",
        "RAPPORT ACCIDENT www.bea.aero Accident to the Robin DR300 - 140 registered F-BSPK on 24 September 2023 at Calais-Marck (Pas-de-Calais)",
        "2023-09-24",
        "F-BSPK",
        "Robin DR300",
        "Calais-Marck",
    ),
    (
        "s2 English identified ultralight",
        "SAFETY INVESTIGATION REPORT Accident to the Skyranger 95B identified 63ASS on 19 June 2022 at Égletons (Corrèze)",
        "2022-06-19",
        "63ASS",
        "Skyranger",
        "Égletons",
    ),
    (
        "s3 English identified paramotor",
        "SAFETY INVESTIGATION REPORT Accident to the paramotor identified 68AKY on 18 June 2022 at Saint Rémy (Deux-Sèvres)",
        "2022-06-18",
        "68AKY",
        "paramotor",
        "Saint Rémy",
    ),
    (
        "s4 English en-dash in type name",
        "SAFETY INVESTIGATION REPORT Accident to the FLIGHT DESIGN – CTLS-ELA registered F-HVAT on 18 June 2021 at Col des Prés, in the commune of Thoiry (Savoie)",
        "2021-06-18",
        "F-HVAT",
        "CTLS",
        "Col des Prés",
    ),
    (
        "s5 French flowing ULM planeur",
        "RAPPORT D'ENQUÊTE www.bea.aero Accident de l'ULM planeur Multiaxe TeST TST-8 (1) DM Alpin identifié 83ALU survenu le 25 août 2019 à Romilly-sur-Seine (10)",
        "2019-08-25",
        "83ALU",
        "TST-8",
        "Romilly-sur-Seine",
    ),
    (
        "s6 English unidentified no reg",
        "SAFETY INVESTIGATION REPORT Accident to the unidentified paramotor equipped with an ITV Awak 2 wing on 26 May 2019 in Aroz (Haute-Saône)",
        "2019-05-26",
        None,
        "paramotor",
        "Aroz",
    ),
    (
        "s7 French flowing à bord du",
        "RAPPORT www.bea.aero Événement survenu à bord du Stearman PT-17 (1) immatriculé F-HIZI survenu le 7 avril 2018 à La Ferté-Alais (91)",
        "2018-04-07",
        "F-HIZI",
        "Stearman",
        "La Ferté-Alais",
    ),
    (
        "s8 French flowing Accident du",
        "RAPPORT www.bea.aero Accident du LAK17A immatriculé F-CJJH survenu le 29 mars 2017 à Chambéry Challes-les-Eaux (73)",
        "2017-03-29",
        "F-CJJH",
        "LAK17A",
        "Chambéry",
    ),
    (
        "s9 French tabular Planeur GROB",
        "RAPPORT ACCIDENT www.bea.aero Planeur GROB G103 TWIN ASTIR II immatriculé F-CFKJ Date et heure 16 avril 2016 à 16 h 45 Exploitant Club Lieu La Motte-Chalancon (26)",
        "2016-04-16",
        "F-CFKJ",
        "GROB G103",
        "Motte-Chalancon",
    ),
    (
        "s10 French tabular Ballon OCR space in reg",
        "RAPPORT ACCIDENT www.bea.aero Ballon Ultramagic M-120 immatriculé F-HEXT 5 janvier 2016 à 11 h 40 Lieu Aurel (26)",
        "2016-01-05",
        "F-HEXT",
        "Ultramagic",
        "Aurel",
    ),
    (
        "s11 French tabular Avion MS880",
        "RAPPORT ACCIDENT www.bea.aero Avion MS880 Rallye immatriculé F-BNSX 28 septembre 2015 à 16 h 25 Lieu Aérodrome de Bourges (18)",
        "2015-09-28",
        "F-BNSX",
        "MS880",
        "Bourges",
    ),
    (
        "s12 French tabular Planeur Schempp Hirth with Date et heure",
        "RAPPORT ACCIDENT www.bea.aero Planeur Schempp Hirth Janus immatriculé F-CEPP, finesse maximum environ 40 Date et heure 26 septembre 2015 à 15 h 30 Exploitant Club Lieu Lépaud (23)",
        "2015-09-26",
        "F-CEPP",
        "Schempp Hirth Janus",
        "Lépaud",
    ),
    (
        "s13 French tabular Jodel no lieu",
        "RAPPORT ACCIDENT www.bea.aero Avion Jodel D127 immatriculé F-BJJX 26 septembre 2015 vers 10 h 50",
        "2015-09-26",
        "F-BJJX",
        "Jodel D127",
        None,  # no location required
    ),
    (
        "s14 French tabular Robin DR 400 no lieu",
        "RAPPORT ACCIDENT www.bea.aero Avion Robin DR 400 immatriculé F-GFXZ 19 septembre 2015 vers 12 h 00",
        "2015-09-19",
        "F-GFXZ",
        "Robin DR",
        None,  # no location required
    ),
]


@pytest.mark.parametrize("label,text,exp_date,exp_reg,ac_sub,loc_sub", SAMPLES, ids=[s[0] for s in SAMPLES])
def test_parse_header_sample(label, text, exp_date, exp_reg, ac_sub, loc_sub):
    h = parse_header(text)

    # date_iso: always asserted
    assert h["date_iso"] == exp_date, f"[{label}] date_iso mismatch: got {h['date_iso']!r}"

    # registration: assert when expected non-None
    if exp_reg is not None:
        assert h["registration"] == exp_reg, f"[{label}] registration mismatch: got {h['registration']!r}"
    else:
        # sample 6: no reg expected — assert None or empty
        assert not h["registration"], f"[{label}] expected no registration, got {h['registration']!r}"

    # aircraft: assert substring present when non-None
    if ac_sub is not None:
        assert h["aircraft"] is not None, f"[{label}] aircraft is None, expected to contain {ac_sub!r}"
        assert ac_sub.lower() in h["aircraft"].lower(), (
            f"[{label}] aircraft {h['aircraft']!r} doesn't contain {ac_sub!r}"
        )

    # location: assert substring when expected
    if loc_sub is not None:
        assert h["location"] is not None, f"[{label}] location is None, expected to contain {loc_sub!r}"
        assert loc_sub.lower() in h["location"].lower(), (
            f"[{label}] location {h['location']!r} doesn't contain {loc_sub!r}"
        )


def test_parse_header_returns_dict_keys():
    """parse_header always returns all four keys, even on empty input."""
    h = parse_header("")
    assert set(h.keys()) == {"aircraft", "registration", "date_iso", "location"}
    assert all(v is None for v in h.values())


def test_parse_header_none_input():
    h = parse_header(None)
    assert set(h.keys()) == {"aircraft", "registration", "date_iso", "location"}
    assert all(v is None for v in h.values())


def test_parse_header_minimum_hit_rates():
    """Aggregate bar: date_iso ≥13/14, registration ≥11/14."""
    date_hits = 0
    reg_hits = 0
    for label, text, exp_date, exp_reg, _, __ in SAMPLES:
        h = parse_header(text)
        if h["date_iso"] == exp_date:
            date_hits += 1
        if exp_reg is None:
            # sample 6: counts as hit when reg is None/empty
            if not h["registration"]:
                reg_hits += 1
        else:
            if h["registration"] == exp_reg:
                reg_hits += 1
    assert date_hits >= 13, f"date_iso hit rate too low: {date_hits}/14"
    assert reg_hits >= 11, f"registration hit rate too low: {reg_hits}/14"
