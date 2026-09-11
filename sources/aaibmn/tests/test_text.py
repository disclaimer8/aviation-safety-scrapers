from aaibmn_ingest.text import (
    strip_html, slugify, make_site_slug,
    parse_event_date, parse_registration, parse_event_class,
)


def test_strip_html():
    assert strip_html("<p>Tail rotor &amp; gear</p> <b>fail</b>") == "Tail rotor & gear fail"
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify():
    assert slugify("Leonardo AW139") == "leonardo-aw139"
    assert slugify("  JU-1088!! ") == "ju-1088"


def test_make_site_slug():
    assert make_site_slug("B737", "JU-1088", "x") == "crash-b737-ju-1088-x"
    assert make_site_slug(None, None, None) == "crash-aaibmn"


def test_parse_event_date_prefix_ymd():
    assert parse_event_date("(2024.02.27)B737-800,JU-1088 INCIDENT") == "2024-02-27"


def test_parse_event_date_suffix_ymd():
    assert parse_event_date('B737-800, JU-1015 incident "TIRE" Japan, 2016.09.09') == "2016-09-09"


def test_parse_event_date_dmon_y():
    assert parse_event_date("F27 Mk050, JU-8258 incident 04 Aug.2016") == "2016-08-04"
    assert parse_event_date("PC-6, JU-1911 incident 20 May. 2013") == "2013-05-20"
    assert parse_event_date("Aircraft accident report Mi 8T JU-5566 18.Oct.2013") == "2013-10-18"


def test_parse_event_date_dmy_numeric():
    assert parse_event_date("(02.06.2017 /IAC/) Final report TVS-2MS RA-2099G") == "2017-06-02"


def test_parse_event_date_none():
    assert parse_event_date("No date here") is None
    assert parse_event_date("") is None


def test_parse_registration_ju_dash():
    assert parse_registration("(2024.02.27)B737-800,JU-1088 INCIDENT") == "JU-1088"


def test_parse_registration_ju_nodash_normalised():
    assert parse_registration("(2022.09.25)H125(AS350 B3), JU6888 ACCIDENT") == "JU-6888"


def test_parse_registration_foreign():
    assert parse_registration("(2020.07.02) B737-800, EI-CXV serious incident") == "EI-CXV"
    assert parse_registration("Final report aircraft TVS-2MS RA-2099G Mongolia") == "RA-2099G"


def test_parse_registration_none():
    assert parse_registration("Incident, ADS-B system failure") is None


def test_parse_event_class():
    assert parse_event_class("JU-1088 INCIDENT warning") == "Incident"
    assert parse_event_class("JU-6888 ACCIDENT forced landing") == "Accident"
    assert parse_event_class("EI-CXV serious incident shutdown") == "Serious incident"
    assert parse_event_class("nothing relevant") is None
