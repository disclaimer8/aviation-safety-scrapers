from eccaa_ingest.text import (
    strip_html, slugify, make_site_slug, country_from_registration, DEFAULT_COUNTRY,
)


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Cessna 402-C") == "cessna-402-c"
    assert slugify("  J8-SXY!! ") == "j8-sxy"


def test_make_site_slug_lowercased_and_combined():
    s = make_site_slug("Cessna 402-C", "J8-SXY", "St Vincent")
    assert s == "crash-cessna-402-c-j8-sxy-st-vincent"
    # site_slug must be lowercased [^a-z0-9]+ -> '-'
    assert s == s.lower()


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "J8-VAX", None) == "crash-j8-vax"
    assert make_site_slug(None, None, None) == "crash-eccaa"


# ── Multi-country derivation (the defining ECCAA feature) ────────────────────

def test_country_oecs_prefixes():
    assert country_from_registration("V2-ABC") == "AG"   # Antigua & Barbuda
    assert country_from_registration("J3-XYZ") == "GD"   # Grenada
    assert country_from_registration("J6-ABC") == "LC"   # Saint Lucia
    assert country_from_registration("J7-ABC") == "DM"   # Dominica
    assert country_from_registration("J8-SXY") == "VC"   # St Vincent
    assert country_from_registration("J8-VAX") == "VC"   # St Vincent
    assert country_from_registration("V4-ABC") == "KN"   # St Kitts & Nevis


def test_country_foreign_prefixes_kept():
    assert country_from_registration("N8862F") == "US"   # USA
    assert country_from_registration("N590DR") == "US"
    assert country_from_registration("G-VNVC") == "GB"   # UK
    assert country_from_registration("YV196T") == "VE"   # Venezuela
    assert country_from_registration("VP-MON") == "MS"   # Montserrat


def test_country_fallback_default():
    assert country_from_registration(None) == DEFAULT_COUNTRY
    assert country_from_registration("") == DEFAULT_COUNTRY
    assert country_from_registration("ZZZ-???") == DEFAULT_COUNTRY
    assert DEFAULT_COUNTRY == "AG"


def test_country_not_a_single_fixed_value():
    """The 7 real ECCAA finals must derive at least 3 distinct countries."""
    regs = ["G-VNVC", "VP-MON", "YV196T", "J8-SXY", "J8-VAX", "N590DR", "N8862F"]
    countries = {country_from_registration(r) for r in regs}
    assert len(countries) >= 3, f"expected multi-country, got {countries}"
    assert "VC" in countries  # St Vincent (the two J8- finals)
    assert "US" in countries  # US-registered finals
