from aaicnp_ingest.text import strip_html, slugify, make_site_slug


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Tail rotor &amp; gear</p>  <b>fail</b>") == "Tail rotor & gear fail"


def test_strip_html_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_slugify_basic():
    assert slugify("Airbus AS350 B3e") == "airbus-as350-b3e"
    assert slugify("  9N-AMI!! ") == "9n-ami"


def test_slugify_lowercase_nonalnum_to_hyphen():
    assert slugify("Lobuche, Solukhumbu") == "lobuche-solukhumbu"


def test_make_site_slug_combines_parts():
    assert make_site_slug("H125", "9N-AMI", "Pathivara") == "crash-h125-9n-ami-pathivara"


def test_make_site_slug_skips_missing_and_has_fallback():
    assert make_site_slug("", "9N-AMS", None) == "crash-9n-ams"
    assert make_site_slug(None, None, None) == "crash-aaicnp"
